import atexit
import base64
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from Crypto.Cipher import AES
from fastapi.testclient import TestClient

# 在导入 app.* 之前设置隔离的临时配置与数据目录，避免触碰本地真实 config.yaml 与 data/
_TMP_DIR = Path(tempfile.mkdtemp(prefix="reminder_test_"))
atexit.register(lambda: shutil.rmtree(_TMP_DIR, ignore_errors=True))
_CFG_FILE = _TMP_DIR / "config.yaml"
_DATA_DIR = _TMP_DIR / "data"

_AES_KEY_RAW = b"0123456789abcdef0123456789abcdef"  # 32 字节
_AES_KEY_B64 = base64.b64encode(_AES_KEY_RAW).decode().rstrip("=")  # 43 位 EncodingAESKey
_CORP_ID = "ww_test_corp"
_CB_TOKEN = "cb_test_token"
_SERVER_TOKEN = "srv_test_token_123456"

_BASE_CFG = {
    "server_token": _SERVER_TOKEN,
    "public_base_url": "https://todo.example.com",
    "timezone": "Asia/Shanghai",
    "wecom": {
        "corpid": _CORP_ID,
        "secret": "test_secret",
        "agentid": 1000002,
        "touser": "@all",
        "callback_token": _CB_TOKEN,
        "encoding_aes_key": _AES_KEY_B64,
    },
    "push": {
        "quiet_enabled": False,
        "quiet_start": 22,
        "quiet_end": 8,
        "max_nags": 48,
    },
    "heartbeat": {
        "enabled": True,
        "time": "08:00",
    },
    "ai": {
        "enabled": True,
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-test-key",
        "model": "deepseek-chat",
        "timeout": 10,
        "max_steps": 5,
        "allowed_users": [],
    },
}

_CFG_FILE.write_text(yaml.safe_dump(_BASE_CFG, allow_unicode=True), encoding="utf-8")
os.environ["REMINDER_CONFIG"] = str(_CFG_FILE)
os.environ["REMINDER_DATA"] = str(_DATA_DIR)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import ai, callback, config, core, db, main, service, wecom  # noqa: E402


def _wecom_encrypt(plain_text: str, corpid: str = _CORP_ID) -> str:
    """构造符合企业微信标准（32 字节 PKCS#7 填充）的回调密文。"""
    raw = plain_text.encode("utf-8")
    body = b"0123456789abcdef" + struct.pack(">I", len(raw)) + raw + corpid.encode("utf-8")
    pad = 32 - (len(body) % 32)
    padded = body + bytes([pad]) * pad
    enc = AES.new(_AES_KEY_RAW, AES.MODE_CBC, _AES_KEY_RAW[:16]).encrypt(padded)
    return base64.b64encode(enc).decode()


def _wecom_sig(timestamp: str, nonce: str, encrypt: str) -> str:
    return hashlib.sha1("".join(sorted([_CB_TOKEN, timestamp, nonce, encrypt])).encode()).hexdigest()


@pytest.fixture(autouse=True)
def reset_db_and_cfg(monkeypatch):
    """每个测试重置数据库内容和配置字典，确保完全隔离。"""
    config.cfg.clear()
    config.cfg.update(yaml.safe_load(yaml.safe_dump(_BASE_CFG)))
    db.init()
    with db._conn() as c:
        c.execute("DELETE FROM tasks")
        c.execute("DELETE FROM meta")
        c.execute("DELETE FROM callback_dedup")
    yield


# -------------------- 1. 调度与业务层测试 --------------------

def test_db_migration_adds_snooze_until(tmp_path, monkeypatch):
    old_db = tmp_path / "legacy.db"
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", old_db)
    import sqlite3
    with sqlite3.connect(old_db) as c:
        c.executescript(
            """
            CREATE TABLE tasks(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              content TEXT NOT NULL,
              remind_at TEXT NOT NULL,
              nag_interval INTEGER,
              nag_count INTEGER NOT NULL DEFAULT 0,
              repeat TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'pending',
              created_at TEXT,
              last_sent_at TEXT,
              last_attempt_at TEXT,
              last_error TEXT
            );
            INSERT INTO tasks(content, remind_at) VALUES('旧数据', '2026-11-27 09:00');
            """
        )
    db.init()
    rows = db.list_active()
    assert len(rows) == 1
    assert rows[0]["content"] == "旧数据"
    assert rows[0]["snooze_until"] is None


def test_repeat_nag_preserves_anchor_on_push_and_defer(monkeypatch):
    t_base = datetime(2026, 11, 26, 9, 0, tzinfo=config.TZ)
    monkeypatch.setattr(service, "now", lambda: t_base - timedelta(minutes=10))
    monkeypatch.setattr(db, "now", lambda: t_base - timedelta(minutes=10))

    res = service.create_reminder("每日晨会", t_base, nag_interval=30, repeat="daily", notify=False)
    tid = res["id"]

    pushed = []
    monkeypatch.setattr(core, "send_text", lambda txt: pushed.append(txt))

    # 1) 到点提醒：重复且带催办时，remind_at 应保持今天 09:00，不提前推到明天
    monkeypatch.setattr(core, "now", lambda: t_base + timedelta(seconds=10))
    core._handle_task(db.get(tid), t_base + timedelta(seconds=10))
    assert len(pushed) == 1
    assert "【待办提醒】每日晨会" in pushed[0]
    row = db.get(tid)
    assert row["remind_at"] == "2026-11-26 09:00"
    assert row["nag_count"] == 0

    # 2) 30 分钟后催办一次
    t_nag = t_base + timedelta(minutes=30, seconds=15)
    core._handle_task(db.get(tid), t_nag)
    assert len(pushed) == 2
    assert "【催办 1/48】每日晨会" in pushed[1]
    assert db.get(tid)["nag_count"] == 1

    # 3) 延后 1 小时：使用 snooze_until，保留原日程锚点 2026-11-26 09:00
    t_defer = t_base + timedelta(minutes=35)
    monkeypatch.setattr(service, "now", lambda: t_defer)
    monkeypatch.setattr(core, "now", lambda: t_defer)
    defer_res = service.defer_reminder(tid, minutes=60)
    assert defer_res["task"]["remind_at"] == "2026-11-26 09:00"
    assert defer_res["task"]["snooze_until"] == "2026-11-26 10:35"

    # 未到 10:35 不应触发
    core._handle_task(db.get(tid), t_base + timedelta(minutes=90))
    assert len(pushed) == 2

    # 到 10:35 触发，同时清空 snooze_until
    t_resumed = datetime(2026, 11, 26, 10, 35, 5, tzinfo=config.TZ)
    core._handle_task(db.get(tid), t_resumed)
    assert len(pushed) == 3
    assert db.get(tid)["snooze_until"] is None
    assert db.get(tid)["remind_at"] == "2026-11-26 09:00"

    # 4) 点击完成：按锚点 09:00 推到明天 09:00；再次误点不跳过明天
    monkeypatch.setattr(core, "now", lambda: t_resumed + timedelta(minutes=5))
    msg, nxt = core.complete(db.get(tid))
    assert "已完成本次" in msg
    assert nxt == "下次提醒：2026-11-27 09:00"
    assert db.get(tid)["remind_at"] == "2026-11-27 09:00"

    _, nxt2 = core.complete(db.get(tid))
    assert nxt2 == "下次提醒：2026-11-27 09:00"
    assert db.get(tid)["remind_at"] == "2026-11-27 09:00"


def test_repeat_nag_cross_day_rollover(monkeypatch):
    t_day1 = datetime(2026, 11, 26, 9, 0, tzinfo=config.TZ)
    monkeypatch.setattr(service, "now", lambda: t_day1 - timedelta(minutes=5))
    tid = service.create_reminder("吃药", t_day1, nag_interval=30, repeat="daily", notify=False)["id"]

    pushed = []
    monkeypatch.setattr(core, "send_text", lambda txt: pushed.append(txt))

    # 第一天推送后未确认，且假设后来静默
    core._handle_task(db.get(tid), t_day1 + timedelta(seconds=5))
    db.update(tid, status="silenced", nag_count=48)

    # 第二天 09:02 到达，自动滚入新周期并重新激活推送
    t_day2 = datetime(2026, 11, 27, 9, 2, tzinfo=config.TZ)
    core._handle_task(db.get(tid), t_day2)
    assert len(pushed) == 2
    row = db.get(tid)
    assert row["remind_at"] == "2026-11-27 09:00"
    assert row["status"] == "pending"
    assert row["nag_count"] == 0


def test_wecom_utf8_chunk_splitting():
    long_text = "\n".join(f"{i}. 这是第 {i} 条中文待办事项内容测试" for i in range(1, 150))
    chunks = wecom.split_utf8_chunks(long_text, max_bytes=1900)
    assert len(chunks) >= 2
    for ch in chunks:
        assert len(ch.encode("utf-8")) <= 1900
        ch.encode("utf-8").decode("utf-8")


# -------------------- 2. 企微回调与 /ai 检测测试 --------------------

def test_callback_verify_and_large_decrypt():
    client = TestClient(main.app)
    plain = "echo_ok_" + ("x" * 2500)  # 超过旧版 2048 字节上限
    enc = _wecom_encrypt(plain)
    ts, nonce = "1700000000", "n123"
    sig = _wecom_sig(ts, nonce, enc)
    resp = client.get("/wecom/callback", params={
        "msg_signature": sig, "timestamp": ts, "nonce": nonce, "echostr": enc,
    })
    assert resp.status_code == 200
    assert resp.text == plain


def test_callback_ai_dispatch_dedup_and_acl(monkeypatch):
    sent = []
    monkeypatch.setattr(callback, "send_text", lambda txt, touser=None: sent.append((touser, txt)))
    monkeypatch.setattr(ai, "handle_command", lambda prompt: f"已收到要求：{prompt}")

    client = TestClient(main.app)

    def post_text(content: str, msg_id: str, from_user: str = "zhangsan", corpid: str = _CORP_ID):
        inner_xml = (
            f"<xml>"
            f"<ToUserName><![CDATA[{_CORP_ID}]]></ToUserName>"
            f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
            f"<CreateTime>1700000001</CreateTime>"
            f"<MsgType><![CDATA[text]]></MsgType>"
            f"<Content><![CDATA[{content}]]></Content>"
            f"<MsgId>{msg_id}</MsgId>"
            f"<AgentID>1000002</AgentID>"
            f"</xml>"
        )
        enc = _wecom_encrypt(inner_xml, corpid=corpid)
        outer = f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>"
        ts, nonce = "1700000001", "n456"
        sig = _wecom_sig(ts, nonce, enc)
        return client.post(
            "/wecom/callback",
            params={"msg_signature": sig, "timestamp": ts, "nonce": nonce},
            content=outer.encode("utf-8"),
        )

    # 非 /ai 消息应被忽略
    r0 = post_text("普通聊天消息", "m_ignore")
    assert r0.status_code == 200 and r0.text == "success"
    assert sent == []

    # 命中 /ai 消息：立刻返回 success 并在后台回推给发送者
    r1 = post_text("/ai 明天9点提醒我开会", "m_1001", from_user="zhangsan")
    assert r1.status_code == 200 and r1.text == "success"
    assert sent == [("zhangsan", "已收到要求：明天9点提醒我开会")]

    # 同一 MsgId 重试：幂等去重，不再重复执行
    r2 = post_text("/ai 明天9点提醒我开会", "m_1001", from_user="zhangsan")
    assert r2.status_code == 200 and r2.text == "success"
    assert len(sent) == 1

    # 白名单开启后，未授权用户被拦截
    config.cfg["ai"]["allowed_users"] = ["zhangsan"]
    r3 = post_text("/ai 帮我删任务", "m_1002", from_user="outsider")
    assert r3.status_code == 200 and r3.text == "success"
    assert len(sent) == 1

    # 错误 corpid 的密文被拒绝
    r4 = post_text("/ai 明天提醒", "m_1003", from_user="zhangsan", corpid="ww_wrong_corp")
    assert r4.status_code == 200 and r4.text == "success"
    assert len(sent) == 1


# -------------------- 3. AI 工具执行引擎测试 --------------------

def test_parse_ai_command():
    assert ai.parse_ai_command("/ai 明天提醒我") == (True, "明天提醒我")
    assert ai.parse_ai_command("  /AI\n查看待办 ") == (True, "查看待办")
    assert ai.parse_ai_command("/ai：延后一小时") == (True, "延后一小时")
    assert ai.parse_ai_command("/ai") == (True, "")
    assert ai.parse_ai_command("/airport hello") == (False, "")
    assert ai.parse_ai_command("今天记得 /ai") == (False, "")


def test_ai_handle_command_tool_calling_lifecycle(monkeypatch):
    fixed_now = datetime(2026, 11, 26, 9, 0, tzinfo=config.TZ)
    monkeypatch.setattr(ai, "now", lambda: fixed_now)
    monkeypatch.setattr(service, "now", lambda: fixed_now)
    monkeypatch.setattr(core, "now", lambda: fixed_now)
    monkeypatch.setattr(db, "now", lambda: fixed_now)

    # 模拟多轮 Tool Calling：创建 -> 延后 -> 最终总结
    responses = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "create_task",
                        "arguments": json.dumps({
                            "content": "交周报",
                            "remind_at": "2026-11-27 09:00",
                            "nag_interval": 15,
                            "repeat": "fri",
                        }, ensure_ascii=False),
                    },
                }
            ],
        },
        {
            "role": "assistant",
            "content": "已为你创建每周五 09:00 的提醒「交周报」，每 15 分钟催办一次。",
        },
    ]

    captured_prompts = []

    def fake_call_llm(messages, conf):
        captured_prompts.append(messages)
        return responses.pop(0)

    monkeypatch.setattr(ai, "_call_llm", fake_call_llm)

    reply = ai.handle_command("每周五早上9点提醒我交周报，每15分钟催一次")
    assert "交周报" in reply
    assert "https://todo.example.com/done/" in reply
    assert "https://todo.example.com/task/" in reply
    assert "未来 14 天日期对照表" in captured_prompts[0][0]["content"]

    active = db.list_active()
    assert len(active) == 1
    assert active[0]["content"] == "交周报"
    assert active[0]["nag_interval"] == 15
    assert active[0]["repeat"] == "fri"


def test_ai_delete_guard_and_error_handling(monkeypatch):
    fixed_now = datetime(2026, 11, 26, 9, 0, tzinfo=config.TZ)
    monkeypatch.setattr(ai, "now", lambda: fixed_now)
    monkeypatch.setattr(service, "now", lambda: fixed_now)
    monkeypatch.setattr(db, "now", lambda: fixed_now)

    ids = [
        service.create_reminder(f"任务{i}", fixed_now + timedelta(hours=i), notify=False)["id"]
        for i in range(1, 4)
    ]

    # 模拟模型尝试一次删除 3 条任务：第 3 条应被护栏拦截
    responses = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": f"del_{i}", "type": "function",
                 "function": {"name": "delete_task", "arguments": json.dumps({"task_id": tid})}}
                for i, tid in enumerate(ids)
            ],
        },
        {
            "role": "assistant",
            "content": "已删除前两条任务，第 3 条因单次最多删除 2 条的保护限制未删。",
        },
    ]
    monkeypatch.setattr(ai, "_call_llm", lambda msgs, conf: responses.pop(0))
    reply = ai.handle_command("删掉这三条任务")
    assert "最多删除 2 条" in reply
    assert len(db.list_active()) == 1

    # 模拟模型超时
    import requests as rq
    monkeypatch.setattr(ai, "_call_llm", lambda msgs, conf: (_ for _ in ()).throw(rq.Timeout("timeout")))
    assert "超时" in ai.handle_command("查一下待办")

    # 关闭 AI 开关时的提示
    config.cfg["ai"]["enabled"] = False
    assert "未开启" in ai.handle_command("明天提醒")


# -------------------- 4. Web 页面与路由测试 --------------------

def test_web_routes_edit_custom_nag_and_prefetch(monkeypatch):
    fixed_now = datetime(2026, 11, 26, 9, 0, tzinfo=config.TZ)
    monkeypatch.setattr(service, "now", lambda: fixed_now)
    monkeypatch.setattr(core, "now", lambda: fixed_now)
    monkeypatch.setattr(main, "now", lambda: fixed_now)
    monkeypatch.setattr(db, "now", lambda: fixed_now)

    client = TestClient(main.app)

    # 未携带 token 访问首页返回 401
    assert client.get("/").status_code == 401

    # AI 创建了自定义 15 分钟催办任务
    tid = service.create_reminder("自定义催办", fixed_now + timedelta(hours=2), nag_interval=15, notify=False)["id"]

    # 从微信直接点击编辑链接（无初始 cookie）：应渲染成功并种下 cookie，保存修改成功
    edit_page = client.get(f"/task/{tid}/edit", params={"k": _SERVER_TOKEN})
    assert edit_page.status_code == 200
    assert "每 15 分钟催" in edit_page.text

    save_resp = client.post(
        f"/task/{tid}/edit",
        data={
            "content": "修改后的内容",
            "remind_at": "2026-11-26T12:00",
            "nag": "15",
            "repeat": "none",
        },
        follow_redirects=False,
    )
    assert save_resp.status_code == 303
    assert db.get(tid)["content"] == "修改后的内容"

    # 预加载头访问 /done 不应误完成任务
    pre = client.get(
        f"/done/{tid}",
        params={"k": _SERVER_TOKEN},
        headers={"Purpose": "prefetch"},
    )
    assert pre.status_code == 204
    assert db.get(tid)["status"] == "pending"

    # 正常点击完成
    done_resp = client.get(f"/done/{tid}", params={"k": _SERVER_TOKEN})
    assert done_resp.status_code == 200
    assert db.get(tid)["status"] == "done"

    # 清空已完成
    clr = client.post("/tasks/clear-done", follow_redirects=False)
    assert clr.status_code == 303
    assert db.list_done() == []


def test_index_render_snooze_badge_and_static_picker(monkeypatch):
    fixed_now = datetime(2026, 11, 26, 9, 0, tzinfo=config.TZ)
    monkeypatch.setattr(service, "now", lambda: fixed_now)
    monkeypatch.setattr(core, "now", lambda: fixed_now)
    monkeypatch.setattr(main, "now", lambda: fixed_now)
    monkeypatch.setattr(db, "now", lambda: fixed_now)

    tid = service.create_reminder("重复打卡", fixed_now + timedelta(minutes=30), nag_interval=10, repeat="daily", notify=False)["id"]
    service.defer_reminder(tid, minutes=60)

    client = TestClient(main.app)
    idx = client.get("/", params={"k": _SERVER_TOKEN})
    assert idx.status_code == 200
    assert "重复打卡" in idx.text
    assert "已延后 · 原日程 09:30" in idx.text
    assert "/static/picker.js?v=" in idx.text

    js = client.get("/static/picker.js")
    assert js.status_code == 200
    assert "initReminderPicker" in js.text


def test_ai_all_tools_update_defer_complete_list_and_past_time(monkeypatch):
    fixed_now = datetime(2026, 11, 26, 9, 0, tzinfo=config.TZ)
    monkeypatch.setattr(ai, "now", lambda: fixed_now)
    monkeypatch.setattr(service, "now", lambda: fixed_now)
    monkeypatch.setattr(core, "now", lambda: fixed_now)
    monkeypatch.setattr(db, "now", lambda: fixed_now)

    tid = service.create_reminder("原任务", fixed_now + timedelta(hours=1), nag_interval=30, notify=False)["id"]

    # 直接验证工具执行器：list_tasks / update_task / defer_task / past time / complete_task
    state = {"touched_ids": [], "deleted_count": 0, "actions": []}

    listed = ai._run_tool("list_tasks", json.dumps({"status": "all"}), state)
    assert listed["ok"] and len(listed["active"]) == 1

    # 过去时间应返回结构化校验错误
    bad = ai._run_tool("create_task", json.dumps({
        "content": "过期任务",
        "remind_at": "2026-11-26 08:00",
    }), state)
    assert bad["ok"] is False and "晚于现在" in bad["error"]

    # 修改内容并取消催办（nag_interval=0）
    upd = ai._run_tool("update_task", json.dumps({
        "task_id": tid,
        "content": "新任务",
        "nag_interval": 0,
    }), state)
    assert upd["ok"] and upd["task"]["content"] == "新任务"
    assert upd["task"]["nag_interval"] is None

    # 延后 120 分钟
    dfr = ai._run_tool("defer_task", json.dumps({
        "task_id": tid,
        "minutes": 120,
    }), state)
    assert dfr["ok"] and dfr["task"]["effective_remind_at"] == "2026-11-26 12:00"

    # 完成任务
    cmp_res = ai._run_tool("complete_task", json.dumps({"task_id": tid}), state)
    assert cmp_res["ok"] and db.get(tid)["status"] == "done"
    assert tid not in state["touched_ids"]

