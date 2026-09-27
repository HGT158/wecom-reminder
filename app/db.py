import sqlite3
from contextlib import contextmanager

from .config import DATA_DIR, DB_PATH, now

# remind_at 存 "%Y-%m-%d %H:%M"（本地时区，分钟精度，与表单 datetime-local 一致）
# last_sent_at / last_attempt_at 存 "%Y-%m-%d %H:%M:%S"
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
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
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


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
    with _conn() as c:
        c.executescript(SCHEMA)


def create_task(content: str, remind_at: str, nag_interval, repeat: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO tasks(content, remind_at, nag_interval, repeat, status, created_at)"
            " VALUES(?,?,?,?, 'pending', ?)",
            (content, remind_at, nag_interval, repeat, now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        return int(cur.lastrowid)


def get(task_id: int):
    with _conn() as c:
        return c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def list_active():
    with _conn() as c:
        return c.execute(
            "SELECT * FROM tasks WHERE status IN ('pending','silenced') ORDER BY remind_at"
        ).fetchall()


def list_done(limit: int = 20):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM tasks WHERE status='done' ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def today_pending(today: str):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM tasks WHERE status='pending' AND remind_at LIKE ? ORDER BY remind_at",
            (today + " %",),
        ).fetchall()


def update(task_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))


def delete(task_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM tasks WHERE id=?", (task_id,))


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
