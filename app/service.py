"""待办核心业务服务层：供 Web 表单路由与 AI 助手工具统一复用。"""
import logging
from datetime import datetime, timedelta

from . import core, db
from .config import TZ, now

log = logging.getLogger("reminder.service")

NAG_MAP = {"none": None, "5": 5, "10": 10, "30": 30, "60": 60}
REPEAT_MAP = {
    "none": "", "daily": "daily",
    "mon": "mon", "tue": "tue", "wed": "wed",
    "thu": "thu", "fri": "fri", "sat": "sat", "sun": "sun",
}
VALID_REPEATS = set(REPEAT_MAP.values())
_UNSET = object()


class TaskValidationError(ValueError):
    """输入校验失败或目标任务状态不合法时抛出的业务异常。"""


def _ensure_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _validate_content(content: str) -> str:
    text = (content or "").strip()
    if not text:
        raise TaskValidationError("内容不能为空")
    if len(text) > 500:
        raise TaskValidationError("待办内容过长（最多 500 字）")
    return text


def _validate_future(dt: datetime) -> datetime:
    dt = _ensure_tz(dt)
    # 表单与口头命令是分钟精度，允许选"当前分钟"（视为立即提醒）
    if dt < now() - timedelta(minutes=1):
        raise TaskValidationError("提醒时间必须晚于现在，请重新设置")
    return dt


def _validate_nag(nag_interval) -> int | None:
    if nag_interval is None or nag_interval == "" or nag_interval == "none":
        return None
    try:
        val = int(nag_interval)
    except (TypeError, ValueError):
        raise TaskValidationError("催办间隔必须是整数分钟")
    if val == 0:
        return None
    if val < 1 or val > 1440:
        raise TaskValidationError("催办间隔需在 1 到 1440 分钟之间")
    return val


def _validate_repeat(repeat: str | None) -> str:
    rep = (repeat or "").strip().lower()
    if rep == "none":
        rep = ""
    if rep not in VALID_REPEATS:
        raise TaskValidationError("不支持的重复周期，仅支持不重复、每天或每周一至每周日")
    return rep


def serialize_task(t) -> dict:
    """将数据库任务行转为结构化字典，供视图和 AI 查询使用。"""
    snooze = t["snooze_until"] if "snooze_until" in t.keys() else None
    eff = core.effective_remind(t)
    return {
        "id": t["id"],
        "content": t["content"],
        "remind_at": t["remind_at"],
        "snooze_until": snooze,
        "effective_remind_at": eff.strftime(core.FMT_MIN),
        "nag_interval": t["nag_interval"],
        "nag_count": t["nag_count"],
        "repeat": t["repeat"] or "",
        "repeat_label": core.REPEAT_LABEL.get(t["repeat"] or "", ""),
        "status": t["status"],
        "last_error": t["last_error"],
    }


def list_reminders(include_done: bool = False, done_limit: int = 20) -> dict:
    """查询进行中（含已静默）及可选已完成待办。"""
    active = [serialize_task(t) for t in db.list_active()]
    done = [serialize_task(t) for t in db.list_done(limit=done_limit)] if include_done else []
    return {"active": active, "done": done}


def create_reminder(
    content: str,
    remind_at: datetime,
    nag_interval=None,
    repeat: str = "",
    notify: bool = True,
) -> dict:
    """创建新待办；notify=True 时立即发送企微确认推送。"""
    clean_content = _validate_content(content)
    dt = _validate_future(remind_at)
    nag_min = _validate_nag(nag_interval)
    rep = _validate_repeat(repeat)

    task_id = db.create_task(clean_content, dt.strftime(core.FMT_MIN), nag_min, rep)
    log.info("建任务 id=%s %s @ %s nag=%s repeat=%s", task_id, clean_content, dt, nag_min, rep)

    push_ok = True
    push_error = None
    if notify:
        try:
            core.push_created(task_id, clean_content, dt, nag_min, rep)
        except Exception as e:
            push_ok = False
            push_error = str(e)
            log.warning("建任务确认推送失败 id=%s: %s", task_id, e)

    return {
        "id": task_id,
        "task": serialize_task(db.get(task_id)),
        "push_ok": push_ok,
        "push_error": push_error,
    }


def update_reminder(
    task_id: int,
    content: str | None = None,
    remind_at: datetime | None = None,
    nag_interval=_UNSET,
    repeat: str | None = None,
    require_future_if_set: bool = True,
) -> dict:
    """修改进行中待办；仅传入的字段会被更新。"""
    t = db.get(task_id)
    if not t:
        raise TaskValidationError(f"任务 #{task_id} 不存在")
    if t["status"] == "done":
        raise TaskValidationError(f"任务 #{task_id} 已完成，无法修改")

    updates = {}
    if content is not None:
        updates["content"] = _validate_content(content)
    if remind_at is not None:
        dt = _validate_future(remind_at) if require_future_if_set else _ensure_tz(remind_at)
        updates["remind_at"] = dt.strftime(core.FMT_MIN)
        updates["snooze_until"] = None
        updates["nag_count"] = 0
        updates["last_sent_at"] = None
        updates["last_attempt_at"] = None
        updates["last_error"] = None
        updates["status"] = "pending"
    if nag_interval is not _UNSET:
        updates["nag_interval"] = _validate_nag(nag_interval)
    if repeat is not None:
        updates["repeat"] = _validate_repeat(repeat)

    if updates:
        db.update(task_id, **updates)
        log.info("编辑任务 id=%s updates=%s", task_id, updates)

    return serialize_task(db.get(task_id))


def defer_reminder(
    task_id: int,
    minutes: int = 60,
    target_dt: datetime | None = None,
) -> dict:
    """延后待办：重复任务写入 snooze_until 保留原周期锚点，单次任务直接推进 remind_at。"""
    t = db.get(task_id)
    if not t:
        raise TaskValidationError(f"任务 #{task_id} 不存在")
    if t["status"] == "done":
        return {"already_done": True, "task": serialize_task(t), "new_at": None}

    if target_dt is not None:
        new_at = _validate_future(target_dt)
    else:
        try:
            mins = int(minutes)
        except (TypeError, ValueError):
            raise TaskValidationError("延后分钟数必须是整数")
        if mins < 1 or mins > 43200:
            raise TaskValidationError("延后时间需在 1 分钟到 30 天之间")
        new_at = max(core.effective_remind(t), now()) + timedelta(minutes=mins)

    new_str = new_at.strftime(core.FMT_MIN)
    if t["repeat"]:
        db.update(
            task_id,
            snooze_until=new_str,
            nag_count=0,
            last_sent_at=None,
            last_attempt_at=None,
            last_error=None,
            status="pending",
        )
    else:
        db.update(
            task_id,
            remind_at=new_str,
            snooze_until=None,
            nag_count=0,
            last_sent_at=None,
            last_attempt_at=None,
            last_error=None,
            status="pending",
        )
    log.info("延后任务 id=%s -> %s", task_id, new_str)
    return {"already_done": False, "task": serialize_task(db.get(task_id)), "new_at": new_at}


def complete_reminder(task_id: int) -> tuple[str, str | None]:
    """完成待办或推进重复任务至下一周期。"""
    t = db.get(task_id)
    if not t:
        raise TaskValidationError(f"任务 #{task_id} 不存在")
    return core.complete(t)


def delete_reminder(task_id: int) -> dict:
    """删除待办并返回被删除任务的摘要。"""
    t = db.get(task_id)
    if not t:
        raise TaskValidationError(f"任务 #{task_id} 不存在")
    info = serialize_task(t)
    db.delete(task_id)
    log.info("删除任务 id=%s content=%s", task_id, info["content"])
    return info
