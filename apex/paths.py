"""Shared filesystem paths."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DASHBOARD_DIR = ROOT / "dashboard"

DB_PATH = DATA_DIR / "apex.db"
STATE_JSON = DATA_DIR / "state.json"
PENDING_JUDGMENTS = DATA_DIR / "pending_judgments.json"
JUDGMENTS = DATA_DIR / "judgments.json"

DATA_DIR.mkdir(exist_ok=True)
