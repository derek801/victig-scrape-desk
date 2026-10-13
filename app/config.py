"""Environment-driven settings. Secrets live in the Render dashboard (or .env.local when run on a PC)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_env_local():
    p = ROOT / ".env.local"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env_local()

DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data" / "local"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "desk.db"
SEED_DIR = ROOT / "data"

TZ = os.environ.get("APP_TZ", "America/New_York")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")

PB_API_KEY = os.environ.get("PB_API_KEY", "")
PB_ORG_ID = os.environ.get("PB_ORG_ID", "3152587389729387")  # "VICTIG Workspace"

BISON_KEY_VICTIG = os.environ.get("BISON_KEY_VICTIG", "")
SLACK_ALERT_WEBHOOK = os.environ.get("SLACK_ALERT_WEBHOOK", "")

SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
ALLOWED_DOMAIN = os.environ.get("ALLOWED_DOMAIN", "victig.com")
# Fallback when Google sign-in isn't configured yet: one shared password. Empty = no auth (local only).
DESK_PASSWORD = os.environ.get("DESK_PASSWORD", "")

# Background loop off switch (tests, one-off scripts).
RUN_SCHEDULER = os.environ.get("RUN_SCHEDULER", "1") == "1"
