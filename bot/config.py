"""Configuration for the ChannelGuard admin bot.

All values come from environment variables (see ``bot/.env.example``). The
module loads a ``.env`` sitting next to it, then falls back to the repo-root
``.env`` so the bot can share credentials with the userbot if you want.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BOT_DIR = Path(__file__).resolve().parent
ROOT_DIR = BOT_DIR.parent

# Load bot/.env first (bot-specific), then the repo root .env as a fallback.
# override=False means the more specific file wins.
load_dotenv(BOT_DIR / ".env")
load_dotenv(ROOT_DIR / ".env", override=False)


def _int(name: str, default: int = 0) -> int:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off", "")


# --- Credentials ----------------------------------------------------------
BOT_TOKEN = (os.getenv("BOT_TOKEN", "") or "").strip()
# The single account that controls the bot and receives every log/notification.
OWNER_ID = _int("OWNER_ID", 0)

# --- Behaviour -------------------------------------------------------------
# Delete "X joined" / "X left" / other member service messages in groups.
CLEAN_SERVICE = _bool("CLEAN_SERVICE", True)
# When an approved user consumes a join-request link, revoke it and mint a
# fresh one so a leaked link can't be reused.
ROTATE_ON_JOIN = _bool("ROTATE_ON_JOIN", True)
# Optional friendly title for the invite links the bot creates.
LINK_TITLE = (os.getenv("LINK_TITLE", "ChannelGuard") or "ChannelGuard").strip()

# --- Storage ---------------------------------------------------------------
DATA_DIR = BOT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.getenv("BOT_DB_PATH", str(DATA_DIR / "channelguard.db")))

# Default template used by the owner link-distribution flow when no custom
# template is registered for a keyword. Supports {link} {title} {short}
# {amount} {name} {keyword}.
DEFAULT_TEMPLATE = (
    "<b>{title}</b>\n"
    "<blockquote>Join link (approval required)</blockquote>\n"
    "{link}"
)


def require() -> None:
    """Abort early with a clear message if the essentials are missing."""
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not OWNER_ID:
        missing.append("OWNER_ID")
    if missing:
        raise SystemExit(
            "Missing config: "
            + ", ".join(missing)
            + "\nCopy bot/.env.example to bot/.env and fill it in."
        )
