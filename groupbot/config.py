"""Configuration for the group-logger bot. Reads .env (see .env.example)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")

DB_PATH = os.getenv("DB_PATH", "data/bot.db").strip()
if not os.path.isabs(DB_PATH):
    DB_PATH = str(BASE_DIR / DB_PATH)

SHORT_FORM_WORDS = max(1, int(os.getenv("SHORT_FORM_WORDS", "1") or "1"))
CLEAN_SERVICE = os.getenv("CLEAN_SERVICE", "1").strip().lower() not in ("0", "false", "no", "")
REVOKE_AFTER_JOIN = os.getenv("REVOKE_AFTER_JOIN", "1").strip().lower() not in ("0", "false", "no", "")

DEFAULT_ADD_TEMPLATE = (
    "<b>Payment confirmed</b>\n"
    "<blockquote>Order: {order}\n"
    "Amount: {amount}\n"
    "Account: {account}\n"
    "Group: {group} ({short})</blockquote>\n"
    "Your invite link (admin approval): {link}"
)
ADD_TEMPLATE = os.getenv("ADD_TEMPLATE", DEFAULT_ADD_TEMPLATE)

# {orderid}-style id for /add, matching the wider project convention.
ORDER_PREFIX = os.getenv("ORDER_PREFIX", "ANI")
ORDER_ID_LENGTH = max(3, int(os.getenv("ORDER_ID_LENGTH", "6") or "6"))


def require() -> None:
    missing = [n for n in ("BOT_TOKEN", "OWNER_ID") if not globals().get(n)]
    if missing:
        raise SystemExit(
            "Missing config: " + ", ".join(missing) + "\n"
            "Copy .env.example to .env and fill BOT_TOKEN and OWNER_ID."
        )
