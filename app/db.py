import sqlite3
from contextlib import contextmanager
from datetime import timedelta

from .config import DATA_DIR, DB_PATH, now

# remind_at / snooze_until 存 "%Y-%m-%d %H:%M"（本地时区，分钟精度）
# last_sent_at / last_attempt_at 存 "%Y-%m-%d %H:%M:%S"
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content TEXT NOT NULL,
  remind_at TEXT NOT NULL,
  snooze_until TEXT,
  nag_interval INTEGER,
  nag_count INTEGER NOT NULL DEFAULT 0,
  repeat TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT,
  last_sent_at TEXT,
  last_attempt_at TEXT,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS callback_dedup(
  msg_key TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);
"""

TASK_COLUMNS = {
    "content",
    "remind_at",
    "snooze_until",
    "nag_interval",
    "nag_count",
    "repeat",
    "status",
    "created_at",
    "last_sent_at",
    "last_attempt_at",
    "last_error",
}


@contextmanager
def _conn():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    """初始化表结构并自动补齐历史库缺失字段。"""
    with _conn() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(tasks)").fetchall()}
        if "snooze_until" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN snooze_until TEXT")


def create_task(content: str, remind_at: str, nag_interval, repeat: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO tasks(content, remind_at, snooze_until, nag_interval, repeat, status, created_at)"
            " VALUES(?,?, NULL, ?,?, 'pending', ?)",
            (content, remind_at, nag_interval, repeat, now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        return int(cur.lastrowid)


def get(task_id: int):
    with _conn() as c:
        return c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def list_active():
    with _conn() as c:
        return c.execute(
            "SELECT * FROM tasks WHERE status IN ('pending','silenced')"
            " ORDER BY COALESCE(snooze_until, remind_at), id"
        ).fetchall()


def list_done(limit: int = 20):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM tasks WHERE status='done' ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def today_pending(today: str):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM tasks WHERE status='pending'"
            " AND COALESCE(snooze_until, remind_at) LIKE ?"
            " ORDER BY COALESCE(snooze_until, remind_at), id",
            (today + " %",),
        ).fetchall()


def update(task_id: int, **fields) -> None:
    if not fields:
        return
    invalid = set(fields) - TASK_COLUMNS
    if invalid:
        raise ValueError(f"非法任务字段: {invalid}")
    cols = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))


def delete(task_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM tasks WHERE id=?", (task_id,))


def clear_done() -> int:
    """清理所有已完成任务，返回清理条数。"""
    with _conn() as c:
        cur = c.execute("DELETE FROM tasks WHERE status='done'")
        return int(cur.rowcount)


def get_meta(key: str):
    with _conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO meta(key,value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def claim_callback_msg(msg_key: str, max_age_hours: int = 24) -> bool:
    """原子认领企微回调消息标识；首次收到返回 True，重复重试返回 False。"""
    cur_dt = now()
    cur_str = cur_dt.strftime("%Y-%m-%d %H:%M:%S")
    cutoff = (cur_dt - timedelta(hours=max_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        c.execute("DELETE FROM callback_dedup WHERE created_at < ?", (cutoff,))
        cur = c.execute(
            "INSERT OR IGNORE INTO callback_dedup(msg_key, created_at) VALUES(?, ?)",
            (msg_key, cur_str),
        )
        return cur.rowcount == 1
