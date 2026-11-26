"""企微/微信聊天框斜杠快捷命令路由器与内置指令实现。"""
import logging
import re
from datetime import timedelta

from . import ai, core, db, service
from .config import APP_VERSION, ai_cfg, base_url, cfg, now, push_cfg

log = logging.getLogger("reminder.commands")

CMD_RE = re.compile(r"^\s*/([a-zA-Z?]+)(?:$|([\s:：,，][\s\S]*))")
DURATION_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(d|天|h|hr|hrs|小时|m|min|mins|分|分钟)?(?:\s+([\s\S]*))?$",
    re.IGNORECASE,
)
MAX_BATCH_COMPLETE = 10
MAX_BATCH_DELETE = 2


def is_slash_command(text: str) -> bool:
    """判断文本是否以合法的 /命令 开头。"""
    return bool(CMD_RE.match(text or ""))


def parse_command(text: str) -> tuple[str, str]:
    """解析斜杠命令，返回 (小写命令名, 去除前缀分隔符后的参数正文)。未命中返回 ("", "")。"""
    m = CMD_RE.match(text or "")
    if not m:
        return "", ""
    cmd = m.group(1).lower()
    arg = (m.group(2) or "").lstrip(" :：,，\t\r\n").strip()
    return cmd, arg


def _parse_duration_token(token: str) -> int:
    """解析时长字符串（如 30 / 30m / 2h / 1.5h / 1d / 30分 / 2小时）为整数分钟。"""
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(d|天|h|hr|hrs|小时|m|min|mins|分|分钟)?$", (token or "").strip(), re.IGNORECASE)
    if not m:
        raise ValueError(f"无法识别时长「{token}」，支持例如 30m、2h、1d 或纯数字分钟")
    num = float(m.group(1))
    unit = (m.group(2) or "m").lower()
    if unit in ("d", "天"):
        mins = round(num * 1440)
    elif unit in ("h", "hr", "hrs", "小时"):
        mins = round(num * 60)
    else:
        mins = round(num)
    if mins < 1 or mins > 43200:
        raise ValueError("时长需在 1 分钟到 30 天之间")
    return mins


def _parse_id_list(arg: str, max_count: int) -> list[int]:
    tokens = [t.strip().lstrip("#") for t in re.split(r"[\s,，、]+", arg.strip()) if t.strip()]
    if not tokens:
        raise ValueError("请提供任务 ID")
    if len(tokens) > max_count:
        raise ValueError(f"单次最多处理 {max_count} 个任务 ID")
    ids: list[int] = []
    for tok in tokens:
        if not tok.isdigit():
            raise ValueError(f"无效的任务 ID「{tok}」")
        tid = int(tok)
        if tid not in ids:
            ids.append(tid)
    return ids


def _format_task_brief(t: dict, short_time: bool = False) -> str:
    dt_str = t["effective_remind_at"]
    time_part = dt_str[11:16] if short_time else dt_str[5:16]
    tags = []
    if t["repeat_label"]:
        tags.append(t["repeat_label"])
    if t["nag_interval"]:
        tags.append(f"每{t['nag_interval']}分催")
    if t["snooze_until"]:
        tags.append("已延后")
    if t["status"] == "silenced":
        tags.append("已静默")
    if t["last_error"]:
        tags.append("推送异常")
    suffix = f"（{' · '.join(tags)}）" if tags else ""
    return f"• #{t['id']} {time_part} {t['content']}{suffix}"


def _cmd_help() -> str:
    return (
        "📖 可用斜杠命令：\n\n"
        "🤖 AI 智能助手\n"
        "• /ai <要求> — 自然语言管理待办\n"
        "  例：/ai 明天9点提醒我交周报，每30分催一次\n\n"
        "⚡ 快捷指令（零延迟 · 免 AI 额度）\n"
        "• /list [done|all]（或 /ls）— 查看待办列表与 ID\n"
        "• /today（或 /td）— 查看今日待办\n"
        "• /in <时长> <内容> — 快速定时（如 /in 30m 关火）\n"
        "• /done <ID...> — 标记任务完成（如 /done 12）\n"
        "• /defer <ID> [时长]（或 /snooze）— 延后任务（默认 1h，支持 30m / 2h / 1d）\n"
        "• /del <ID...>（或 /rm）— 删除任务（单次最多 2 条）\n"
        "• /status（或 /ping）— 查看服务与配置状态\n"
        "• /help（或 /h）— 显示本帮助"
    )


def _cmd_list(arg: str) -> str:
    scope = (arg or "active").strip().lower()
    if scope not in ("active", "done", "all"):
        return "用法：/list [done|all]\n示例：/list、/list done、/list all"

    snap = service.list_reminders(include_done=(scope in ("done", "all")), done_limit=15)
    active = snap["active"]
    done = snap["done"]
    lines: list[str] = []

    if scope in ("active", "all"):
        if active:
            lines.append(f"📋 进行中待办（{len(active)} 件）：")
            lines.extend(_format_task_brief(t) for t in active[:30])
            if len(active) > 30:
                lines.append(f"… 另有 {len(active) - 30} 件请前往网页查看")
        elif scope == "active":
            lines.append("🍃 当前没有进行中的待办。")

    if scope in ("done", "all"):
        if lines:
            lines.append("")
        if done:
            lines.append(f"✅ 最近已完成（{len(done)} 件）：")
            lines.extend(f"• #{t['id']} {t['content']}（{t['remind_at'][5:16]}）" for t in done)
        elif scope == "done":
            lines.append("暂无已完成记录。")

    base = base_url()
    if base:
        lines.append(f"\n🔗 管理列表：{base}/?k={cfg['server_token']}")
    return "\n".join(lines)


def _cmd_today() -> str:
    cur = now()
    today = cur.strftime("%Y-%m-%d")
    items = [service.serialize_task(t) for t in db.today_pending(today)]
    all_active = db.list_active()
    other_cnt = max(0, len(all_active) - len(items))

    if items:
        lines = [f"☀️ 今日待办（{today}）共 {len(items)} 件："]
        lines.extend(_format_task_brief(t, short_time=True) for t in items)
    else:
        lines = [f"☀️ 今日（{today}）暂无待办事项。"]

    if other_cnt:
        lines.append(f"\n另有 {other_cnt} 件其他日期/已静默待办，发 /list 查看")
    return "\n".join(lines)


def _cmd_in(arg: str) -> str:
    m = DURATION_RE.match(arg or "")
    if not m or not (m.group(3) or "").strip():
        return "用法：/in <时长> <内容>\n示例：/in 30m 关火  或  /in 2h 取快递"

    dur_tok = m.group(1) + (m.group(2) or "")
    content = m.group(3).strip()
    try:
        mins = _parse_duration_token(dur_tok)
        target = now() + timedelta(minutes=mins)
        res = service.create_reminder(content, target, nag_interval=None, repeat="", notify=False)
    except (ValueError, service.TaskValidationError) as e:
        return f"❌ 创建失败：{e}"

    tid = res["id"]
    t = res["task"]
    lines = [
        f"✅ 已创建快捷提醒 #{tid}：{t['content']}",
        f"时间：{t['effective_remind_at']}（{mins} 分钟后，只提醒一次）",
    ]
    if base_url():
        lines.append(f"✅ 办完了：{core.done_url(tid)}")
        lines.append(f"✏️ 修改：{core.edit_url(tid)}")
    return "\n".join(lines)


def _cmd_done(arg: str) -> str:
    if not arg:
        return "用法：/done <任务ID>\n示例：/done 12（可先发 /list 查看 ID）"
    try:
        ids = _parse_id_list(arg, MAX_BATCH_COMPLETE)
    except ValueError as e:
        return f"❌ {e}"

    results: list[str] = []
    for tid in ids:
        try:
            msg, nxt = service.complete_reminder(tid)
            line = f"#{tid} {msg}"
            if nxt:
                line += f"（{nxt}）"
            results.append(line)
        except service.TaskValidationError as e:
            results.append(f"❌ #{tid} {e}")
    return "\n".join(results)


def _cmd_defer(arg: str) -> str:
    parts = arg.split() if arg else []
    if not parts or len(parts) > 2:
        return "用法：/defer <任务ID> [时长]\n示例：/defer 12（默认延后1小时）或 /defer 12 30m / /defer 12 2h"

    raw_id = parts[0].lstrip("#")
    if not raw_id.isdigit():
        return f"❌ 无效的任务 ID「{parts[0]}」"
    tid = int(raw_id)

    try:
        mins = _parse_duration_token(parts[1]) if len(parts) == 2 else 60
        res = service.defer_reminder(tid, minutes=mins)
    except (ValueError, service.TaskValidationError) as e:
        return f"❌ 延后失败：{e}"

    t = res["task"]
    if res["already_done"]:
        return f"⏰ #{tid}「{t['content']}」已经完成过啦，无需延后。"

    lines = [
        f"⏰ 已延后 #{tid}「{t['content']}」{mins} 分钟",
        f"下次提醒：{t['effective_remind_at']}",
    ]
    if base_url():
        lines.append(f"✅ 办完了：{core.done_url(tid)}")
        lines.append(f"✏️ 修改：{core.edit_url(tid)}")
    return "\n".join(lines)


def _cmd_del(arg: str) -> str:
    if not arg:
        return "用法：/del <任务ID>\n示例：/del 12（单次最多删除 2 条）"
    try:
        ids = _parse_id_list(arg, MAX_BATCH_DELETE)
    except ValueError as e:
        return f"❌ {e}"

    results: list[str] = []
    for tid in ids:
        try:
            deleted = service.delete_reminder(tid)
            results.append(f"🗑️ 已删除 #{tid}「{deleted['content']}」")
        except service.TaskValidationError as e:
            results.append(f"❌ #{tid} {e}")
    return "\n".join(results)


def _cmd_status() -> str:
    cur = now()
    active = db.list_active()
    silenced_cnt = sum(1 for t in active if t["status"] == "silenced")
    today_cnt = len(db.today_pending(cur.strftime("%Y-%m-%d")))
    done_cnt = len(db.list_done(limit=100))

    pc = push_cfg()
    quiet_text = (
        f"开启（{pc['quiet_start']}:00–次日{pc['quiet_end']}:00）"
        if pc["quiet_enabled"]
        else "关闭"
    )
    hb = cfg.get("heartbeat") or {}
    hb_text = f"开启（每天 {hb.get('time', '08:00')}）" if hb.get("enabled") else "关闭"
    ac = ai_cfg()
    if not ac["enabled"]:
        ai_text = "关闭"
    elif not ac["api_key"]:
        ai_text = f"已开启但未配密钥（{ac['model']}）"
    else:
        ai_text = f"正常（{ac['model']}）"

    lines = [
        f"🩺 服务运行正常 · v{APP_VERSION}",
        f"当前时间：{cur.strftime('%Y-%m-%d %H:%M:%S')}",
        f"待办统计：进行中 {len(active)} 件（已静默 {silenced_cnt}） · 今日 {today_cnt} 件 · 最近已完成 {done_cnt} 件",
        f"免打扰：{quiet_text}",
        f"每日心跳：{hb_text}",
        f"AI 助手：{ai_text}",
    ]
    base = base_url()
    if base:
        lines.append(f"管理页：{base}/?k={cfg['server_token']}")
    return "\n".join(lines)


def dispatch(text: str) -> tuple[bool, str]:
    """检测并执行斜杠命令；返回 (是否命中斜杠命令, 回复内容)。"""
    cmd, arg = parse_command(text)
    if not cmd:
        return False, ""

    if cmd == "ai":
        return True, ai.handle_command(arg)
    if cmd in ("help", "h", "?"):
        return True, _cmd_help()
    if cmd in ("list", "ls"):
        return True, _cmd_list(arg)
    if cmd in ("today", "td"):
        return True, _cmd_today()
    if cmd == "in":
        return True, _cmd_in(arg)
    if cmd == "done":
        return True, _cmd_done(arg)
    if cmd in ("defer", "snooze"):
        return True, _cmd_defer(arg)
    if cmd in ("del", "rm", "delete"):
        return True, _cmd_del(arg)
    if cmd in ("status", "ping"):
        return True, _cmd_status()

    return True, f"❓ 未知命令 /{cmd}，发送 /help 查看全部可用命令。"
