"""调度与提醒逻辑：每 15 秒 tick 一次，全部状态以 SQLite 为准，重启即恢复。"""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import db
from .config import TZ, cfg, now
from .wecom import send_text

log = logging.getLogger("reminder.core")

FMT_MIN = "%Y-%m-%d %H:%M"
FMT_SEC = "%Y-%m-%d %H:%M:%S"
WEEKDAY_NUM = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
REPEAT_LABEL = {"daily": "每天", "mon": "每周一", "tue": "每周二", "wed": "每周三",
                "thu": "每周四", "fri": "每周五", "sat": "每周六", "sun": "每周日"}

_sched = None


def parse_min(s: str) -> datetime:
    return datetime.strptime(s, FMT_MIN).replace(tzinfo=TZ)


def parse_sec(s: str) -> datetime:
    return datetime.strptime(s, FMT_SEC).replace(tzinfo=TZ)


def done_url(task_id: int) -> str:
    base = (cfg.get("public_base_url") or "").rstrip("/")
    return f"{base}/done/{task_id}?k={cfg['server_token']}"


def in_quiet(t: datetime) -> bool:
    qs = int(cfg["push"]["quiet_start"])
    qe = int(cfg["push"]["quiet_end"])
    h = t.hour
    return (h >= qs or h < qe) if qs > qe else (qs <= h < qe)


def next_occurrence(repeat: str, dt: datetime, after: datetime):
    """repeat 任务的下次时间：从 dt 往后找第一个严格晚于 after 的匹配日。"""
    d = dt + timedelta(days=1)
    if repeat == "daily":
        while d <= after:
            d += timedelta(days=1)
        return d
    wd = WEEKDAY_NUM.get(repeat)
    if wd is None:
        return None
    while d <= after or d.weekday() != wd:
        d += timedelta(days=1)
    return d


def _attempt_allowed(t, n: datetime) -> bool:
    """上次尝试推送失败时限流：60 秒内不重试。"""
    la = t["last_attempt_at"]
    return not la or parse_sec(la) + timedelta(seconds=60) <= n


def _push(t, text: str, n: datetime) -> bool:
    try:
        send_text(text)
    except Exception as e:
        log.warning("推送失败 id=%s: %s", t["id"], e)
        db.update(t["id"], last_attempt_at=n.strftime(FMT_SEC), last_error=str(e))
        return False
    db.update(t["id"], last_sent_at=n.strftime(FMT_SEC),
              last_attempt_at=n.strftime(FMT_SEC), last_error=None)
    return True


def process_tick():
    n = now()
    try:
        _handle_heartbeat(n)
    except Exception:
        log.exception("心跳处理异常")
    for t in db.list_active():
        try:
            _handle_task(t, n)
        except Exception:
            log.exception("任务处理异常 id=%s", t["id"])


def _handle_task(t, n: datetime):
    remind = parse_min(t["remind_at"])
    last_sent = parse_sec(t["last_sent_at"]) if t["last_sent_at"] else None
    nag = t["nag_interval"]
    max_nags = int(cfg["push"]["max_nags"])

    # 1) 到点提醒：本次 occurrence 还没发过就发
    if remind <= n and (last_sent is None or last_sent < remind):
        if not _attempt_allowed(t, n):
            return
        late = n - remind > timedelta(minutes=10)
        head = "【待办·补发】" if late else "【待办提醒】"
        lines = f"{head}{t['content']}\n时间：{remind.strftime(FMT_MIN)}"
        if nag:
            lines += f"\n未确认将每 {nag} 分钟催一次（{cfg['push']['quiet_start']}:00–次日{cfg['push']['quiet_end']}:00 不打扰）"
        lines += f"\n办完了点这里停止催办：{done_url(t['id'])}"
        if _push(t, lines, n):
            if t["repeat"]:
                nxt = next_occurrence(t["repeat"], remind, n)
                if nxt:
                    db.update(t["id"], remind_at=nxt.strftime(FMT_MIN), nag_count=0)
            elif not nag:
                db.update(t["id"], status="done")  # 一次性、不催办：发完即完成
            else:
                db.update(t["id"], nag_count=0)
        return

    # 2) 催办：距上次推送超过设定间隔，且不在免打扰时段
    if nag and last_sent and last_sent + timedelta(minutes=nag) <= n:
        if t["nag_count"] >= max_nags:
            db.update(t["id"], status="silenced")
            return
        if not _attempt_allowed(t, n) or in_quiet(n):
            return
        if _push(t, f"【催办 {t['nag_count'] + 1}/{max_nags}】{t['content']}\n还没办？点此确认：{done_url(t['id'])}", n):
            db.update(t["id"], nag_count=t["nag_count"] + 1)


def _handle_heartbeat(n: datetime):
    hb = cfg.get("heartbeat") or {}
    if not hb.get("enabled"):
        return
    hh, mm = (int(x) for x in str(hb.get("time", "08:00")).split(":"))
    if (n.hour, n.minute) < (hh, mm):
        return
    today = n.strftime("%Y-%m-%d")
    if db.get_meta("last_heartbeat") == today:
        return
    hb_start = n.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if n > hb_start + timedelta(hours=3):
        # 错过窗口（如下午才启动）不打扰，只标记今天已处理
        db.set_meta("last_heartbeat", today)
        return
    items = db.today_pending(today)
    if items:
        body = "\n".join(f"{i + 1}. {t['remind_at'][11:16]} {t['content']}" for i, t in enumerate(items))
        text = f"☀️ 在岗打卡 | 今日待办 {len(items)} 件：\n{body}"
    else:
        text = "☀️ 在岗打卡 | 今日无待办，服务正常运行"
    try:
        send_text(text)
        db.set_meta("last_heartbeat", today)
    except Exception as e:
        log.warning("心跳推送失败: %s", e)


def push_created(task_id: int, content: str, dt: datetime, nag_min, rep: str):
    parts = [f"✅ 已建提醒：{content}", f"时间：{dt.strftime(FMT_MIN)}"]
    if REPEAT_LABEL.get(rep):
        parts.append(f"重复：{REPEAT_LABEL[rep]}")
    if nag_min:
        parts.append(f"未完成每 {nag_min} 分钟催一次")
    parts.append(f"办完点此：{done_url(task_id)}")
    send_text("\n".join(parts))


def complete(t) -> tuple[str, str | None]:
    """处理"完成"点击。返回 (提示, 下次时间提示)。"""
    n = now()
    if t["status"] == "done":
        return "该提醒已处理过，无需重复操作。", None
    if t["repeat"]:
        r = parse_min(t["remind_at"])
        nxt = r if r > n else next_occurrence(t["repeat"], r, n)
        db.update(t["id"], remind_at=nxt.strftime(FMT_MIN), nag_count=0,
                  last_sent_at=None, last_attempt_at=None, last_error=None)
        return f"✅ 已完成本次「{t['content']}」", f"下次提醒：{nxt.strftime(FMT_MIN)}"
    db.update(t["id"], status="done", last_error=None)
    return f"✅ 已完成「{t['content']}」，催办已停止。", None


def start():
    global _sched
    _sched = BackgroundScheduler(timezone=str(cfg.get("timezone", "Asia/Shanghai")))
    _sched.add_job(process_tick, "interval", seconds=15, id="tick",
                   max_instances=1, coalesce=True)
    _sched.add_job(process_tick, "date", run_date=now() + timedelta(seconds=1))
    _sched.start()
    log.info("调度器已启动，tick 间隔 15s")


def stop():
    if _sched:
        _sched.shutdown(wait=False)
