"""Config for the channel-guard userbot. Reads .env (see .env.example)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
CHANNEL_RAW = os.getenv("CHANNEL", "")
OWNER_RAW = os.getenv("OWNER", "")
ROTATE_MINUTES = float(os.getenv("ROTATE_MINUTES", "5"))

# --- Quick-reply updater (second userbot) ---------------------------------
# Account that SENDS the invite link to the quick-reply userbot (usually the
# guard account). Blank = accept links from any private chat.
LINK_SOURCE_RAW = os.getenv("LINK_SOURCE", "")
# The Business quick reply shortcut name to keep equal to the link.
SHORTCUT = os.getenv("SHORTCUT", "demo")
# Send the current Business AWAY message to anyone who DMs us the first time.
GREET_NEW = os.getenv("GREET_NEW", "1").strip().lower() not in ("0", "false", "no", "")

BASE_DIR = Path(__file__).resolve().parent
SESSION = str(BASE_DIR / "userbot")          # guard account session
QR_SESSION = str(BASE_DIR / "quickreply")    # quick-reply account session
ENV_PATH = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
GREETED_FILE = DATA_DIR / "greeted.json"     # user ids already auto-replied to


def coerce(s: str):
    """A channel/user string as an int id when numeric, else the raw string."""
    s = (s or "").strip()
    if not s:
        return s
    body = s[1:] if s.startswith("-") else s
    return int(s) if body.isdigit() else s


def channel():
    return coerce(CHANNEL_RAW)


def owner():
    return coerce(OWNER_RAW)


def link_source():
    return coerce(LINK_SOURCE_RAW)


def save_env(updates: dict) -> None:
    """Write/replace the given KEY=value lines in .env, preserving the rest."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    out, seen = [], set()
    for ln in lines:
        key = ln.split("=", 1)[0] if "=" in ln else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(ln)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(out) + "\n")


def require(*names: str) -> None:
    missing = [n for n in names if not globals().get(n)]
    if missing:
        raise SystemExit(
            "Missing config: " + ", ".join(missing) + "\nRun:  python setup.py"
        )
