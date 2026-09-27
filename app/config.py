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
    cfg = yaml.safe_load(f)

TZ = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))


def now() -> datetime:
    return datetime.now(TZ)
