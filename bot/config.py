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
# Rotate (revoke + remint) the general approval link after each approval.
# Default OFF: an approval-required link is safe to reuse forever (every joiner
# is gated), and rotating it invalidates a link you may have already shared.
# Single-use /add order links are always one-time regardless of this setting.
ROTATE_ON_JOIN = _bool("ROTATE_ON_JOIN", False)
# Optional friendly title for the invite links the bot creates.
LINK_TITLE = (os.getenv("LINK_TITLE", "ChannelGuard") or "ChannelGuard").strip()

# --- Orders ----------------------------------------------------------------
# Prefix for the order ids minted by /add, e.g. ANI0001, ANI0002, ...
ORDER_PREFIX = (os.getenv("ORDER_PREFIX", "ANI") or "ANI").strip()
# Optional chat to also post each order to (numeric id like -100123... or a
# public @username). Leave empty to disable channel posting.
PAYMENT_CHANNEL = (os.getenv("PAYMENT_CHANNEL", "") or "").strip()

# --- Storage ---------------------------------------------------------------
DATA_DIR = BOT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.getenv("BOT_DB_PATH", str(DATA_DIR / "channelguard.db")))

# Template for the quick bare-text link lookup (general approval link). Tokens:
# {link} {title} {short} {amount} {name} {keyword} {orderid}.
DEFAULT_TEMPLATE = (
    "<b>{title}</b>\n"
    "<blockquote>Approval required</blockquote>\n"
    "{link}"
)

# Template for a paid /add order post (single-use link). Used when no custom
# per-keyword template is registered. Same tokens as above.
ORDER_TEMPLATE = (
    "<b>{title}</b>\n"
    "<blockquote>Order <code>{orderid}</code>   "
    "Amount <code>{amount}</code>   {name}</blockquote>\n"
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
