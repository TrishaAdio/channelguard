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
# Security sweep: kick all non-admin members on startup and every N minutes.
# 0 disables the recurring sweep (the startup sweep still runs).
SWEEP_MINUTES = float(os.getenv("SWEEP_MINUTES", "5"))

# --- Quick-reply updater (second userbot) ---------------------------------
# Guard account that sends the rotating demo link. When blank, quickreply.py
# accepts only the exact link published by local guard.py in data/demo_link.json.
LINK_SOURCE_RAW = os.getenv("LINK_SOURCE", "")
# The Business quick reply shortcut name to keep equal to the link.
SHORTCUT = os.getenv("SHORTCUT", "demo")
# Receive guard.py's rotated link and rewrite it into the /SHORTCUT (demo)
# Business quick reply — the original behaviour. Default 1 = ON. Set 0 to freeze
# the demo link (e.g. if the BOT manages links instead).
USERBOT_RELAY_LINK = os.getenv("USERBOT_RELAY_LINK", "1").strip().lower() not in ("0", "false", "no", "")
# Also rewrite the /set greeting post's invite link on every rotation.
# Default 0 = FREEZE the greeting link (e.g. a fixed "Join for demo" group) so
# only the /SHORTCUT quick reply follows the live rotating link. Set to 1 to
# restore the old behaviour where the greeting link is swapped too.
SWAP_GREETING = os.getenv("SWAP_GREETING", "0").strip().lower() not in ("0", "false", "no", "")
# Send the current greeting/Business away message to first-time DMs.
# This can also be changed live with /away on|off in Saved Messages.
GREET_NEW = os.getenv("GREET_NEW", "1").strip().lower() not in ("0", "false", "no", "")
# A manual outgoing message means the owner is active for this long. Telegram's
# live UserStatusOnline is checked too; this window covers delayed status updates.
ONLINE_MINUTES = max(0.0, float(os.getenv("ONLINE_MINUTES", "2")))

# --- Payment logger (quickreply.py extension) -----------------------------
# Revenue split for the {rioshare}/{marco} caption parameters.
RIO_PCT = float(os.getenv("RIO_PCT", "55"))
MARCO_PCT = float(os.getenv("MARCO_PCT", "45"))
# What {rioshare}/{marco} are computed from:
#   today       -> that % of the TOTAL amount received so far today
#   transaction -> that % of THIS single payment's amount
SHARE_BASE = os.getenv("SHARE_BASE", "today").strip().lower()
# Caption parse mode for the auto-post: html or none.
PAY_PARSE_MODE = os.getenv("PAY_PARSE_MODE", "html").strip().lower()
# Timezone used to decide what counts as "today" (INR default = India).
TZ_NAME = os.getenv("TZ", "Asia/Kolkata")
# {orderid} template parameter: a prefix followed by a random suffix, generated
# once per payment (e.g. ANI7F3K9Q). Configure the prefix and suffix length.
ORDER_PREFIX = os.getenv("ORDER_PREFIX", "ANI")
ORDER_ID_LENGTH = max(3, int(os.getenv("ORDER_ID_LENGTH", "6")))

BASE_DIR = Path(__file__).resolve().parent
SESSION = str(BASE_DIR / "userbot")          # guard account session
QR_SESSION = str(BASE_DIR / "quickreply")    # quick-reply account session
ENV_PATH = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
GREETED_FILE = DATA_DIR / "greeted.json"     # user ids already auto-replied to
GREETING_FILE = DATA_DIR / "greeting.json"   # the /set greeting post reference
PAY_FILE = DATA_DIR / "pay.json"             # post channel + caption templates + payments log


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
