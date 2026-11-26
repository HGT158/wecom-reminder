"""AI 命令解析与待办工具执行引擎：兼容 OpenAI Chat Completions / Tool Calling 接口。"""
import json
import logging
import re
from datetime import datetime, timedelta

import requests

from . import core, db, service
from .config import TZ, ai_cfg, base_url, cfg, now

log = logging.getLogger("reminder.ai")

# 匹配消息开头的 /ai 命令（支持空格、换行或中英文冒号/逗号分隔）
AI_CMD_RE = re.compile(r"^\s*/ai(?=$|[\s:：,，])([\s\S]*)$", re.IGNORECASE)
WEEKDAYS_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
MAX_DELETE_PER_CALL = 2

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "查询当前待办任务列表（包含任务 ID、内容、提醒时间、催办间隔、重复规则和状态）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "done", "all"],
                        "description": "查询范围：active 为进行中与已静默任务（默认），done 为最近已完成任务，all 为全部。",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "创建一条新的待办提醒任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "待办事项内容，简洁明确。",
                    },
                    "remind_at": {
                        "type": "string",
                        "description": "本地提醒时间，必须是 24 小时制格式 YYYY-MM-DD HH:MM（且不早于当前时间）。",
                    },
                    "nag_interval": {
                        "type": "integer",
                        "description": "未确认时的催办间隔（分钟，如 5/10/15/30/60）。用户未要求反复催办时传 0 或省略。",
                    },
                    "repeat": {
                        "type": "string",
                        "enum": ["", "daily", "mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                        "description": "重复规则：空字符串表示不重复，daily 表示每天，mon..sun 表示每周一至每周日。",
                    },
                },
                "required": ["content", "remind_at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": "修改已有待办任务的内容、提醒时间、催办间隔或重复规则。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "要修改的任务 ID。",
                    },
                    "content": {
                        "type": "string",
                        "description": "新的任务内容（不改则省略）。",
                    },
                    "remind_at": {
                        "type": "string",
                        "description": "新的提醒时间 YYYY-MM-DD HH:MM（不改则省略）。",
                    },
                    "nag_interval": {
                        "type": "integer",
                        "description": "新的催办间隔分钟数；传 0 表示取消催办（不改则省略）。",
                    },
                    "repeat": {
                        "type": "string",
                        "enum": ["", "daily", "mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                        "description": "新的重复规则（不改则省略）。",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "defer_task",
            "description": "延后一条进行中的待办任务。可以指定延后分钟数或明确的目标时间。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "要延后的任务 ID。",
                    },
                    "minutes": {
                        "type": "integer",
                        "description": "从原定提醒时间或当前时间起延后的分钟数（例如延后2小时传 120）。",
                    },
                    "remind_at": {
                        "type": "string",
                        "description": "明确延后到的目标时间 YYYY-MM-DD HH:MM（与 minutes 二选一，优先此字段）。",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "将指定任务标记为已完成（若为重复任务则自动排入下一周期）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "要标记完成的任务 ID。",
                    }
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_task",
            "description": "永久删除指定待办任务。单次指令最多删除 2 条明确目标。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "要删除的任务 ID。",
                    }
                },
                "required": ["task_id"],
            },
        },
    },
]


def parse_ai_command(text: str) -> tuple[bool, str]:
    """检测消息是否以 /ai 命令开头，返回 (是否命中, 要求正文)。"""
    m = AI_CMD_RE.match(text or "")
    if not m:
        return False, ""
    body = (m.group(1) or "").lstrip(" :：,，\t\r\n").strip()
    return True, body


def _chat_endpoint(url: str) -> str:
    b = (url or "https://api.deepseek.com/v1").strip().rstrip("/")
    if b.endswith("/chat/completions"):
        return b
    return f"{b}/chat/completions"


def _parse_dt(value: str) -> datetime:
    s = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    raise service.TaskValidationError(f"时间格式无效「{value}」，请使用 YYYY-MM-DD HH:MM")


def _format_task_line(t: dict) -> str:
    rep = t["repeat_label"] or "不重复"
    nag = f"每{t['nag_interval']}分钟催" if t["nag_interval"] else "不催办"
    snooze_note = f"（已延后，原日程 {t['remind_at']}）" if t["snooze_until"] else ""
    return (
        f"- id={t['id']} | {t['content']} | 下次提醒={t['effective_remind_at']}{snooze_note}"
        f" | {rep} | {nag} | 状态={t['status']}"
    )


def _build_system_prompt(cur: datetime) -> str:
    wd = WEEKDAYS_ZH[cur.weekday()]
    cal_lines = []
    for i in range(14):
        d = cur.date() + timedelta(days=i)
        tag = {0: "（今天）", 1: "（明天）", 2: "（后天）"}.get(i, "")
        cal_lines.append(f"  {d.strftime('%Y-%m-%d')} {WEEKDAYS_ZH[d.weekday()]}{tag}")

    snap = service.list_reminders(include_done=True, done_limit=10)
    active = snap["active"]
    done = snap["done"]
    active_text = "\n".join(_format_task_line(t) for t in active[:50]) or "（暂无进行中待办）"
    if len(active) > 50:
        active_text += f"\n（共 {len(active)} 条进行中待办，其余可调用 list_tasks 查看）"
    done_text = "\n".join(_format_task_line(t) for t in done[:10]) or "（无）"

    return (
        "你是企业微信待办提醒系统的 AI 助手，只负责通过提供的工具管理用户的待办提醒。\n\n"
        f"当前北京时间：{cur.strftime('%Y-%m-%d %H:%M:%S')} {wd}\n"
        "未来 14 天日期对照表（涉及相对日期、星期几时务必严格查表，不可凭空心算）：\n"
        + "\n".join(cal_lines)
        + "\n\n当前进行中待办：\n"
        + active_text
        + "\n\n最近已完成待办：\n"
        + done_text
        + "\n\n执行规则：\n"
        "1. 只能调用工具对现有待办进行查询、创建、修改、延后、完成、删除与总结，不得执行任何系统命令或编造未执行的操作结果。\n"
        "2. 时间必须使用 24 小时制 YYYY-MM-DD HH:MM。若用户只说了时间点（如“9点”），结合当前时刻合理推断并明确告知；若用户明确指了一个已经过去的时刻（如当前 15:00 却说“今天上午9点”），请不要擅自乱建，礼貌指出时间已过并询问确认。\n"
        "3. 催办间隔：只有当用户提到“催/反复提醒/每隔X分钟”等意思时才设置 nag_interval；仅说“提醒我…”时默认传 0（只提醒一次）。\n"
        "4. 重复周期：仅支持不重复（\"\"）、每天（\"daily\"）、每周一至周日（\"mon\"..\"sun\"）。不支持每月/间隔几天等周期，若遇到请如实说明暂不支持并给出替代建议。\n"
        "5. 删除安全：单次指令最多删除 2 条明确目标。如果用户要求清空全部或批量删掉多条未逐一确认的任务，请先列出目标并提示前往网页端处理或分批确认，不要直接执行删除。\n"
        "6. 回复风格：用于微信纯文本气泡展示，简洁清晰，不要使用 Markdown 标题、表格或 ** 加粗语法；不要自己编造 URL 链接（系统会自动附上真实可点击的操作链接）。"
    )


def _usage_text() -> str:
    return (
        "🤖 AI 待办助手用法：/ai <你的要求>\n"
        "示例：\n"
        "• /ai 明天上午9点提醒我交周报，每30分钟催一次\n"
        "• /ai 今天和明天还有哪些待办？\n"
        "• /ai 把交周报的提醒延后2小时\n"
        "• /ai 我已经完成交周报了\n"
        "• /ai 把测试提醒删掉"
    )


def _attach_action_links(reply: str, touched_ids: list[int]) -> str:
    base = base_url()
    if not base:
        return reply

    active_ids: list[int] = []
    for tid in touched_ids:
        if tid in active_ids:
            continue
        t = db.get(tid)
        if t and t["status"] != "done":
            active_ids.append(tid)

    if not active_ids:
        return reply

    if len(active_ids) == 1:
        tid = active_ids[0]
        links = (
            f"\n\n✅ 办完了：{core.done_url(tid)}"
            f"\n✏️ 修改：{core.edit_url(tid)}"
        )
        return reply + links

    lines = ["\n\n🔗 快捷操作："]
    for tid in active_ids[:4]:
        lines.append(f"• #{tid} 改：{core.edit_url(tid)} | 完：{core.done_url(tid)}")
    lines.append(f"📋 全部待办：{base}/?k={cfg['server_token']}")
    return reply + "\n".join(lines)


def _run_tool(name: str, raw_args: str, state: dict) -> dict:
    try:
        args = json.loads(raw_args) if raw_args else {}
        if not isinstance(args, dict):
            return {"ok": False, "error": "工具参数必须是 JSON 对象"}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"工具参数 JSON 解析失败: {e}"}

    try:
        if name == "list_tasks":
            status = str(args.get("status") or "active").lower()
            snap = service.list_reminders(include_done=(status in ("done", "all")), done_limit=20)
            if status == "done":
                return {"ok": True, "done": snap["done"]}
            if status == "all":
                return {"ok": True, "active": snap["active"], "done": snap["done"]}
            return {"ok": True, "active": snap["active"]}

        if name == "create_task":
            dt = _parse_dt(str(args.get("remind_at") or ""))
            nag = args.get("nag_interval")
            if nag == 0:
                nag = None
            res = service.create_reminder(
                content=str(args.get("content") or ""),
                remind_at=dt,
                nag_interval=nag,
                repeat=str(args.get("repeat") or ""),
                notify=False,
            )
            state["touched_ids"].append(res["id"])
            state["actions"].append(f"创建 #{res['id']} {res['task']['content']}")
            return {"ok": True, "task": res["task"]}

        if name == "update_task":
            tid = int(args["task_id"])
            remind_dt = _parse_dt(args["remind_at"]) if args.get("remind_at") else None
            nag = args["nag_interval"] if "nag_interval" in args else service._UNSET
            updated = service.update_reminder(
                task_id=tid,
                content=args.get("content"),
                remind_at=remind_dt,
                nag_interval=nag,
                repeat=args.get("repeat"),
            )
            state["touched_ids"].append(tid)
            state["actions"].append(f"修改 #{tid} {updated['content']}")
            return {"ok": True, "task": updated}

        if name == "defer_task":
            tid = int(args["task_id"])
            target_dt = _parse_dt(args["remind_at"]) if args.get("remind_at") else None
            mins = args.get("minutes")
            res = service.defer_reminder(
                task_id=tid,
                minutes=int(mins) if mins is not None else 60,
                target_dt=target_dt,
            )
            if not res["already_done"]:
                state["touched_ids"].append(tid)
                state["actions"].append(f"延后 #{tid} -> {res['task']['effective_remind_at']}")
            return {
                "ok": True,
                "already_done": res["already_done"],
                "task": res["task"],
            }

        if name == "complete_task":
            tid = int(args["task_id"])
            msg, nxt = service.complete_reminder(tid)
            t = db.get(tid)
            if t and t["status"] != "done":
                state["touched_ids"].append(tid)
            else:
                state["touched_ids"] = [x for x in state["touched_ids"] if x != tid]
            state["actions"].append(f"完成 #{tid}")
            return {"ok": True, "message": msg, "next": nxt}

        if name == "delete_task":
            tid = int(args["task_id"])
            if state["deleted_count"] >= MAX_DELETE_PER_CALL:
                return {
                    "ok": False,
                    "error": f"为防误删，单次 /ai 指令最多删除 {MAX_DELETE_PER_CALL} 条任务，其余请分批确认或前往网页端操作。",
                }
            deleted = service.delete_reminder(tid)
            state["deleted_count"] += 1
            state["touched_ids"] = [x for x in state["touched_ids"] if x != tid]
            state["actions"].append(f"删除 #{tid} {deleted['content']}")
            return {"ok": True, "deleted": deleted}

        return {"ok": False, "error": f"未知工具: {name}"}

    except (service.TaskValidationError, KeyError, TypeError, ValueError) as e:
        return {"ok": False, "error": str(e)}


def _call_llm(messages: list[dict], conf: dict) -> dict:
    url = _chat_endpoint(conf["base_url"])
    headers = {
        "Authorization": f"Bearer {conf['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": conf["model"],
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.1,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=conf["timeout"])
    if resp.status_code >= 400:
        snippet = resp.text[:300].replace("\n", " ")
        raise RuntimeError(f"LLM HTTP {resp.status_code}: {snippet}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise RuntimeError(f"LLM 返回缺少 choices: {str(data)[:200]}")
    msg = choices[0].get("message")
    if not isinstance(msg, dict):
        raise RuntimeError("LLM 返回缺少 message 对象")
    return msg


def handle_command(prompt: str) -> str:
    """执行 /ai 后面的用户要求，返回适合直接回推微信的纯文本结果。"""
    conf = ai_cfg()
    if not conf["enabled"]:
        return "AI 功能未开启，请在 config.yaml 中将 ai.enabled 设为 true 后重启服务。"

    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        return _usage_text()

    if not conf["api_key"]:
        return "AI 尚未配置 api_key，请在 config.yaml 的 ai.api_key 中填写后重启服务。"

    messages = [
        {"role": "system", "content": _build_system_prompt(now())},
        {"role": "user", "content": clean_prompt},
    ]
    state = {"touched_ids": [], "deleted_count": 0, "actions": []}

    for step in range(conf["max_steps"]):
        try:
            assistant_msg = _call_llm(messages, conf)
        except requests.Timeout:
            log.warning("AI 请求超时 timeout=%ss", conf["timeout"])
            return "AI 响应超时，请稍后重试或使用网页端操作。"
        except Exception as e:
            log.warning("AI 调用失败: %s", e)
            return "AI 调用失败（请检查接口配置或网络状态），暂时无法执行该指令。"

        tool_calls = assistant_msg.get("tool_calls") or []
        if not tool_calls:
            text = (assistant_msg.get("content") or "").strip()
            if not text:
                text = "已处理完成。" if not state["actions"] else "已完成：" + "；".join(state["actions"])
            return _attach_action_links(text, state["touched_ids"])

        messages.append({
            "role": "assistant",
            "content": assistant_msg.get("content") or "",
            "tool_calls": tool_calls,
        })

        for tc in tool_calls:
            tc_id = tc.get("id") or f"call_{step}"
            fn = tc.get("function") or {}
            fn_name = fn.get("name") or ""
            fn_args = fn.get("arguments") or "{}"
            result = _run_tool(fn_name, fn_args, state)
            log.info("AI 工具调用 %s(%s) -> ok=%s", fn_name, fn_args, result.get("ok"))
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    summary = "；".join(state["actions"]) if state["actions"] else "已达到最大推理步数，请换个更明确的说法再试。"
    return _attach_action_links(summary, state["touched_ids"])
