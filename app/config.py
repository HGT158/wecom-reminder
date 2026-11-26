import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("REMINDER_CONFIG", ROOT / "config.yaml"))
DATA_DIR = Path(os.environ.get("REMINDER_DATA", ROOT / "data"))
DB_PATH = DATA_DIR / "reminder.db"

with open(CONFIG_PATH, encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}

TZ = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))

APP_VERSION = "1.3"


def now() -> datetime:
    """返回当前配置的本地时区时间。"""
    return datetime.now(TZ)


def base_url() -> str:
    """返回配置中的对外访问基地址（去除末尾斜杠）。"""
    return (cfg.get("public_base_url") or "").rstrip("/")


def push_cfg() -> dict:
    """返回推送相关配置（带默认值兜底）。"""
    raw = cfg.get("push") or {}
    return {
        "quiet_enabled": bool(raw.get("quiet_enabled", False)),
        "quiet_start": int(raw.get("quiet_start", 22)),
        "quiet_end": int(raw.get("quiet_end", 8)),
        "max_nags": int(raw.get("max_nags", 48)),
    }


def ai_cfg() -> dict:
    """返回 AI 接入配置（兼容未配置 ai 段的老版 config.yaml）。"""
    raw = cfg.get("ai") or {}
    allowed = raw.get("allowed_users") or []
    if isinstance(allowed, str):
        allowed = [allowed]
    return {
        "enabled": bool(raw.get("enabled", False)),
        "provider": str(raw.get("provider") or "openai_compatible").strip(),
        "base_url": str(raw.get("base_url") or "https://api.deepseek.com/v1").strip(),
        "api_key": str(raw.get("api_key") or os.environ.get("AI_API_KEY") or "").strip(),
        "model": str(raw.get("model") or "deepseek-chat").strip(),
        "timeout": float(raw.get("timeout") or 30),
        "max_steps": max(1, int(raw.get("max_steps") or 5)),
        "allowed_users": [str(u).strip() for u in allowed if str(u).strip()],
    }
