"""Quick-reply userbot (second account, typically the OWNER).

Two jobs:
  1. When it receives a t.me invite link from LINK_SOURCE (the guard account),
     it swaps ONLY the link inside your "/SHORTCUT" post AND inside the greeting
     post — text, markdown, and premium (custom) emoji stay exactly as they were.
  2. When someone DMs this account for the FIRST time while the owner is offline,
     it sends the greeting post (when away replies are enabled). Set the greeting
     from your own Saved Messages: reply to any post with /set. It falls back to
     the Business away message if no greeting is set.
  3. Sending exactly uppercase L in a private chat clears that conversation for
     both sides and blocks the other user.

Saved Messages commands (send to yourself):
  /set          reply to a post -> use it as the greeting
  /unset        clear the greeting
  /show         show greeting + away status
  /away on      enable first-contact away replies
  /away off     disable first-contact away replies
  /away status  show whether away replies are enabled
  /broadcast TIME DATE [YEAR] KEYWORD
                reply to a post -> copy it to every chat where you sent the
                keyword between that IST start time and now

Example:
  reply to a post with /broadcast 9:30 AM 18 JUL THANKS FOR

Payment logger (send these yourself in ANY chat):
  /add <amt> [name]              reply to an image -> log it, message the user,
                                 and auto-post the image + caption to the channel
  /setdone <template>            message sent to the user in the private chat
  /setchannelpostofpayment <t>   caption for the channel post
  .setchannel                    type it in a channel -> post media there
  /stats                         today's total (INR) + count + split
  /cancel                        in the upload channel, reply to a payment post
                                 -> mark it fake and remove it from today's stats
  /clear                         reset today's stats to zero
  .ping                          verify the quick-reply userbot is running
  .help                          show every command and template parameter

Business quick replies require Telegram Premium (the payment logger does not).

Run:  python quickreply.py    (Ctrl+C to stop)
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import random
import re
import time
import traceback
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.functions.contacts import BlockRequest
from telethon.tl.functions.messages import (
    DeleteHistoryRequest,
    EditMessageRequest,
    GetQuickRepliesRequest,
    GetQuickReplyMessagesRequest,
    SendMessageRequest,
    SendQuickReplyMessagesRequest,
)
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    InputPeerSelf,
    InputQuickReplyShortcut,
    InputUserSelf,
    MessageEntityBold,
    MessageMediaWebPage,
    UserStatusOnline,
)

import config
import ui

# The link the guard sends us (full https invite link).
LINK_RE = re.compile(r"https?://t\.me/(?:joinchat/|\+)[\w-]+", re.IGNORECASE)
# The link inside the saved post (may or may not include the scheme).
FIND_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)[\w-]+", re.IGNORECASE)

# Owner-only Saved Messages broadcast:
#   /broadcast 9:30 AM 18 JUL THANKS FOR
# Reply to the post to copy. Every chat containing an owner-authored message
# with the keyword between that IST start time and the command time is targeted.
BROADCAST_RE = re.compile(
    r"^/broadcast\s+(\d{1,2}):(\d{2})\s*(AM|PM)\s+"
    r"(\d{1,2})\s+([A-Za-z]{3,9})(?:\s+(\d{4}))?\s+(.+?)\s*$",
    re.IGNORECASE,
)
MONTHS = {
    "JAN": 1, "JANUARY": 1,
    "FEB": 2, "FEBRUARY": 2,
    "MAR": 3, "MARCH": 3,
    "APR": 4, "APRIL": 4,
    "MAY": 5,
    "JUN": 6, "JUNE": 6,
    "JUL": 7, "JULY": 7,
    "AUG": 8, "AUGUST": 8,
    "SEP": 9, "SEPT": 9, "SEPTEMBER": 9,
    "OCT": 10, "OCTOBER": 10,
    "NOV": 11, "NOVEMBER": 11,
    "DEC": 12, "DECEMBER": 12,
}
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    try:
        _TZ = ZoneInfo(config.TZ_NAME)   # payment logger "today" boundary
    except Exception:  # noqa: BLE001
        _TZ = IST
except (ImportError, KeyError):
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")
    _TZ = IST

# Payment logger defaults (overridable via /setdone and /setchannelpostofpayment).
DEFAULT_DONE = "Payment of {amount} received - thank you, {name}!"
DEFAULT_CHANNEL = (
    "New payment received\n"
    "{name} paid {amount}\n\n"
    "Rio {rioshare} - Marco {marco}\n"
    "Payments today: {total}\n"
    "Collected today: {todaytotal}"
)

client: TelegramClient | None = None
_state = {
    "source_id": None,
    "last": None,
    "self_id": 0,
    "away_enabled": config.GREET_NEW,
    "owner_active_until": 0.0,
    "activity_generation": 0,
}
_greeted: set[int] = set()
_automatic_outgoing: dict[int, dict] = {}
_automatic_message_ids: set[tuple[int, int]] = set()
_broadcast_lock = asyncio.Lock()
_payment_lock = asyncio.Lock()

# Payment logger state (loaded in main): post_channel, done/channel templates
# (+ optional *_ref message references), and the payments log.
_pay: dict = {}

# Kept open for the whole process so a second copy can't grab the same lock.
_instance_lock = None


def _acquire_single_instance_lock() -> bool:
    """Take an exclusive lock so only ONE quickreply.py runs per machine.

    Running two copies on the same account makes them fight over every command:
    one edits the message to its result while the other overwrites it with
    'Command failed', and each error lands in a different terminal. Returns
    True if this process owns the lock, False if another instance already does.
    """
    global _instance_lock
    try:
        import fcntl
    except ImportError:
        return True  # non-POSIX platform: skip the guard rather than block
    try:
        config.PAY_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle = open(config.PAY_FILE.parent / "quickreply.lock", "w")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        return False
    _instance_lock = handle  # hold the fd open; the OS frees it when we exit
    try:
        handle.write(str(os.getpid()))
        handle.flush()
    except OSError:
        pass
    return True


def _load_greeted() -> set[int]:
    if config.GREETED_FILE.exists():
        try:
            return set(json.loads(config.GREETED_FILE.read_text()))
        except (ValueError, OSError):
            return set()
    return set()


def _save_greeted() -> None:
    config.GREETED_FILE.write_text(json.dumps(sorted(_greeted)))


def _load_greeting():
    """The /set greeting reference {chat_id, message_id}, or None."""
    if config.GREETING_FILE.exists():
        try:
            data = json.loads(config.GREETING_FILE.read_text())
            if "chat_id" in data and "message_id" in data:
                return data
        except (ValueError, OSError):
            return None
    return None


def _save_greeting(chat_id: int, message_id: int) -> None:
    config.GREETING_FILE.write_text(
        json.dumps({"chat_id": int(chat_id), "message_id": int(message_id)})
    )


def _clear_greeting() -> bool:
    if config.GREETING_FILE.exists():
        config.GREETING_FILE.unlink()
        return True
    return False


def _mark_owner_active() -> None:
    """Suppress away replies after a manual outgoing message from any session."""
    _state["owner_active_until"] = (
        time.monotonic() + (config.ONLINE_MINUTES * 60)
    )
    _state["activity_generation"] += 1


def _expect_automatic_outgoing(user_id: int, texts: list[str]) -> dict:
    """Create an identity marker before the userbot sends to one peer."""
    marker = {
        "until": time.monotonic() + 30,
        "texts": set(texts),
        "ready": asyncio.Event(),
    }
    _automatic_outgoing[user_id] = marker
    return marker


def _sent_message_ids(result) -> set[int]:
    """Extract message ids from Telethon Message/list/Updates send results."""
    ids = set()
    if isinstance(result, (list, tuple)):
        for item in result:
            ids.update(_sent_message_ids(item))
        return ids
    message_id = getattr(result, "id", None)
    if isinstance(message_id, int):
        ids.add(message_id)
    for update in getattr(result, "updates", []) or []:
        message = getattr(update, "message", None)
        message_id = getattr(message, "id", None)
        if isinstance(message_id, int):
            ids.add(message_id)
    return ids


def _finish_automatic_outgoing(user_id: int, marker: dict, result) -> None:
    ids = _sent_message_ids(result)
    _automatic_message_ids.update((user_id, message_id) for message_id in ids)
    marker["ready"].set()
    if _automatic_outgoing.get(user_id) is marker:
        _automatic_outgoing.pop(user_id, None)


def _cancel_automatic_outgoing(user_id: int, marker: dict) -> None:
    marker["ready"].set()
    if _automatic_outgoing.get(user_id) is marker:
        _automatic_outgoing.pop(user_id, None)


async def _consume_automatic_outgoing(event) -> bool:
    """Match generated outgoing updates by peer and Telegram message id."""
    user_id = event.chat_id
    key = (user_id, event.id)
    if key in _automatic_message_ids:
        _automatic_message_ids.discard(key)
        return True

    marker = _automatic_outgoing.get(user_id)
    if marker is None or (event.raw_text or "") not in marker["texts"]:
        return False
    if marker["until"] <= time.monotonic():
        _cancel_automatic_outgoing(user_id, marker)
        return False
    try:
        await asyncio.wait_for(marker["ready"].wait(), timeout=2)
    except asyncio.TimeoutError:
        return False
    if key not in _automatic_message_ids:
        return False
    _automatic_message_ids.discard(key)
    return True


async def _owner_is_online() -> bool:
    """Report the owner online ONLY on strong evidence: a recent manual send
    from any session, or Telegram explicitly reporting `UserStatusOnline`.

    This fails OPEN (returns False = offline) for every approximate/hidden
    status — `UserStatusRecently`, `LastWeek`, `LastMonth`, `Empty`, or a
    hidden last-seen (`status is None`). The previous fail-closed logic
    permanently suppressed away replies for the very common case of a hidden
    last-seen, because that status is not `UserStatusOffline`.
    """
    if time.monotonic() < _state["owner_active_until"]:
        return True
    try:
        me = await client.get_me()
        status = getattr(me, "status", None)
        if isinstance(status, UserStatusOnline):
            return status.expires.timestamp() > time.time()
        # Anything that is not an explicit, live "online" is treated as offline
        # so greetings actually fire.
        return False
    except Exception as e:  # noqa: BLE001
        ui.warn(f"Couldn't verify owner presence; treating as offline: {type(e).__name__}")
        return False


async def _away_still_allowed(generation: int) -> bool:
    """Re-authorize an away send after its message/template lookups finish."""
    if not _state["away_enabled"] or generation != _state["activity_generation"]:
        return False
    if await _owner_is_online():
        return False
    return (
        _state["away_enabled"]
        and generation == _state["activity_generation"]
        and time.monotonic() >= _state["owner_active_until"]
    )


def _set_away_enabled(enabled: bool) -> None:
    config.save_env({"GREET_NEW": "1" if enabled else "0"})
    _state["away_enabled"] = enabled
    _state["activity_generation"] += 1


def _u16(s: str) -> int:
    """Length in UTF-16 code units (how Telegram counts entity offsets)."""
    return len(s.encode("utf-16-le")) // 2


def _swap_link(text: str, entities, new_link: str):
    """Point the invite link at `new_link`, covering BOTH forms:
      (a) a hyperlink where the URL lives in a text_url entity's `.url`, and
      (b) a raw link in the visible text (offsets/lengths are re-aligned so
          formatting + custom emoji stay attached).

    Returns (new_text, entities) or None if nothing needed changing.
    """
    entities = list(entities or [])
    changed = False

    # (a) hyperlinked invite links -> just repoint the entity's url
    for e in entities:
        url = getattr(e, "url", None)
        if url and FIND_LINK_RE.search(url) and url != new_link:
            e.url = new_link
            changed = True

    # (b) a raw invite link in the visible text -> swap + shift offsets
    new_text = text or ""
    m = FIND_LINK_RE.search(new_text)
    if m and m.group(0) != new_link:
        old = m.group(0)
        pi = m.start()
        o = _u16(new_text[:pi])
        old_len = _u16(old)
        new_len = _u16(new_link)
        delta = new_len - old_len
        r_start, r_end = o, o + old_len
        for e in entities:
            start, end = e.offset, e.offset + e.length
            if end <= r_start:
                pass                              # before the link
            elif start >= r_end:
                e.offset = e.offset + delta       # after -> shift
            else:
                e.length = max(0, e.length + delta)  # covers/equals -> resize
        new_text = new_text[:pi] + new_link + new_text[pi + len(old):]
        changed = True

    return (new_text, entities) if changed else None


async def _swap_in_shortcut(shortcut_id: int, new_link: str) -> int:
    """Swap the link inside every message of a quick-reply shortcut. Returns
    how many messages were changed."""
    msgs = await client(GetQuickReplyMessagesRequest(
        shortcut_id=shortcut_id, hash=0, id=None,
    ))
    updated = 0
    for msg in getattr(msgs, "messages", []) or []:
        text = getattr(msg, "message", "") or ""
        swapped = _swap_link(text, getattr(msg, "entities", None), new_link)
        if swapped is None:
            continue
        new_text, new_entities = swapped
        had_preview = isinstance(getattr(msg, "media", None), MessageMediaWebPage)
        try:
            await client(EditMessageRequest(
                peer=InputPeerSelf(), id=msg.id, message=new_text,
                entities=new_entities or None,
                quick_reply_shortcut_id=shortcut_id,
                no_webpage=not had_preview,
            ))
            updated += 1
        except MessageNotModifiedError:
            pass
    return updated


async def update_link(name: str, new_link: str) -> str:
    res = await client(GetQuickRepliesRequest(hash=0))
    shortcuts = getattr(res, "quick_replies", []) or []
    target = next((q for q in shortcuts if getattr(q, "shortcut", None) == name), None)

    # No saved post yet -> create a minimal one (just the link) so it works.
    if target is None:
        await client(SendMessageRequest(
            peer=InputPeerSelf(), message=new_link,
            quick_reply_shortcut=InputQuickReplyShortcut(shortcut=name),
            random_id=random.randrange(-(2 ** 63), 2 ** 63),
        ))
        return "created (no existing post to preserve)"

    updated = await _swap_in_shortcut(target.shortcut_id, new_link)
    return f"updated link in {updated} message(s)" if updated else "no link in the post"


async def _away_shortcut_id():
    """The shortcut id backing the Business away message, or None if unset."""
    full = await client(GetFullUserRequest(InputUserSelf()))
    away = getattr(full.full_user, "business_away_message", None)
    return getattr(away, "shortcut_id", None) if away else None


async def _copy_to(peer, src):
    """Send a copy of `src` (text/media + entities, incl. premium emoji)."""
    text = src.message or ""
    ents = src.entities or None
    media = getattr(src, "media", None)
    if media and not isinstance(media, MessageMediaWebPage):
        return await client.send_file(
            peer,
            file=media,
            caption=text,
            formatting_entities=ents,
        )
    return await client.send_message(
        peer,
        text,
        formatting_entities=ents,
        link_preview=isinstance(media, MessageMediaWebPage),
    )


# --------------------------------------------------------------------------
# Payment logger
# --------------------------------------------------------------------------
def _default_pay() -> dict:
    return {
        "post_channel": None,
        "done_template": DEFAULT_DONE,
        "channel_template": DEFAULT_CHANNEL,
        "payments": [],
    }


def _optional_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_pay_data(value) -> tuple[dict, list[str]]:
    """Return a safe payment-store shape and a list of repairs performed.

    Manual JSON edits commonly turn numbers into strings or accidentally change
    `payments` from a list into an object. Payment state must never prevent the
    Telethon handlers from starting, so salvage valid records and skip only
    irrecoverable entries.
    """
    repairs = []
    if isinstance(value, list):
        repairs.append("top-level payment list wrapped into an object")
        value = {"payments": value}
    elif not isinstance(value, dict):
        repairs.append("top-level value replaced (expected an object)")
        value = {}

    data = dict(value)
    post_channel = _optional_int(data.get("post_channel"))
    if data.get("post_channel") not in (None, "") and post_channel is None:
        repairs.append("invalid post_channel removed")
    data["post_channel"] = post_channel

    for key, default in (
        ("done_template", DEFAULT_DONE),
        ("channel_template", DEFAULT_CHANNEL),
    ):
        if not isinstance(data.get(key), str):
            if key in data:
                repairs.append(f"invalid {key} replaced")
            data[key] = default

    raw_payments = data.get("payments", [])
    if isinstance(raw_payments, dict):
        if "amount" in raw_payments and "ts" in raw_payments:
            repairs.append("single payment object wrapped into a list")
            raw_payments = [raw_payments]
        else:
            repairs.append("payments object converted to a list")
            raw_payments = list(raw_payments.values())
    elif not isinstance(raw_payments, list):
        repairs.append("invalid payments value replaced with an empty list")
        raw_payments = []

    payments = []
    allowed_statuses = {
        "valid", "pending", "failed", "untracked", "cancel_pending",
        "fake", "cleared", "quarantined",
    }
    for index, raw in enumerate(raw_payments):
        if not isinstance(raw, dict):
            repairs.append(f"payment #{index + 1} skipped (expected an object)")
            continue
        payment = dict(raw)
        try:
            amount = float(payment.get("amount"))
            ts = float(payment.get("ts"))
            if (
                not math.isfinite(amount)
                or amount <= 0
                or amount > 1_000_000_000_000_000
                or not math.isfinite(ts)
            ):
                raise ValueError
            # Finite floats can still be outside the platform datetime range.
            datetime.fromtimestamp(ts, _TZ)
        except (TypeError, ValueError, OverflowError, OSError):
            repairs.append(f"payment #{index + 1} skipped (invalid amount/time)")
            continue
        payment["amount"] = amount
        payment["ts"] = ts
        payment["name"] = str(payment.get("name") or "")
        if "order_id" in payment:
            payment["order_id"] = str(payment.get("order_id") or "")
        status = str(payment.get("status") or "valid").lower()
        if status not in allowed_statuses:
            repairs.append(f"payment #{index + 1} status quarantined")
            payment["original_status"] = status
            status = "quarantined"
        payment["status"] = status
        for key in (
            "post_chat_id", "post_message_id", "cancel_command_message_id"
        ):
            if key in payment:
                converted = _optional_int(payment.get(key))
                if payment.get(key) not in (None, "") and converted is None:
                    repairs.append(f"payment #{index + 1} invalid {key} removed")
                payment[key] = converted
        payments.append(payment)
    data["payments"] = payments
    return data, repairs


def _write_pay_data(data: dict) -> None:
    """Durably and atomically replace pay.json."""
    config.PAY_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = config.PAY_FILE.with_name(config.PAY_FILE.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(config.PAY_FILE)
    # Persist the directory entry when the platform supports directory fsync.
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(str(config.PAY_FILE.parent), os.O_RDONLY | directory_flag)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Directory fsync is a best-effort durability step and raises EINVAL on
        # several filesystems (overlayfs, some network/container mounts). The
        # data is already safely written via the atomic replace above, so this
        # must never propagate and fail the command that triggered the save.
        pass
    finally:
        os.close(descriptor)


def _load_pay() -> dict:
    if not config.PAY_FILE.exists():
        return _default_pay()

    raw_bytes = b""
    repairs = []
    try:
        raw_bytes = config.PAY_FILE.read_bytes()
        raw = raw_bytes.decode("utf-8")
        value = json.loads(raw)
        data, repairs = _normalize_pay_data(value)
    except (UnicodeError, ValueError, OSError) as e:
        data = _default_pay()
        repairs = [f"pay.json could not be read ({type(e).__name__})"]

    if repairs:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = config.PAY_FILE.with_name(f"pay.recovery-{stamp}.json")
        try:
            if raw_bytes:
                backup.write_bytes(raw_bytes)
            _write_pay_data(data)
            backup_note = f" Original saved as {backup.name}." if raw_bytes else ""
            ui.warn(f"Repaired payment data ({'; '.join(repairs)}).{backup_note}")
        except OSError as e:
            ui.warn(f"Payment data was repaired in memory but could not be saved: {e}")
    return data


def _save_pay() -> None:
    _write_pay_data(_pay)


def _now_ts() -> float:
    return datetime.now(_TZ).timestamp()


def _today_key(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(_now_ts() if ts is None else ts, _TZ)
    return dt.strftime("%Y-%m-%d")


def _todays_payments() -> list:
    key = _today_key()
    return [
        p for p in _pay.get("payments", [])
        if p.get("status", "valid") == "valid" and _today_key(p["ts"]) == key
    ]


def fmt_inr(value) -> str:
    """Format a number with the Rupee sign and Indian digit grouping."""
    neg = value < 0
    value = abs(float(value))
    whole = int(value)
    frac = round(value - whole, 2)

    s = str(whole)
    if len(s) > 3:
        last3 = s[-3:]
        rest = s[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        grouped = ",".join(groups) + "," + last3
    else:
        grouped = s

    out = "\u20b9" + grouped  # rupee sign
    if frac > 0:
        out += ("%.2f" % frac)[1:]  # ".50"
    return ("-" if neg else "") + out


def parse_amount(raw: str) -> float:
    return float(str(raw).replace(",", "").replace("\u20b9", "").strip())


# {orderid} suffix uses an unambiguous alphabet (no 0/O/1/I) so IDs are easy to
# read back from a screenshot.
_ORDER_ALPHABET = "ACDEFGHJKLMNPQRSTUVWXYZ23456789"


def _generate_order_id() -> str:
    """A unique per-payment id: config prefix + random suffix (e.g. ANI7F3K9Q)."""
    existing = {
        p.get("order_id")
        for p in _pay.get("payments", [])
        if isinstance(p, dict) and p.get("order_id")
    }
    prefix = config.ORDER_PREFIX
    length = config.ORDER_ID_LENGTH
    for _ in range(50):
        candidate = prefix + "".join(random.choices(_ORDER_ALPHABET, k=length))
        if candidate not in existing:
            return candidate
    # Extremely unlikely; widen the suffix to guarantee uniqueness.
    return prefix + "".join(random.choices(_ORDER_ALPHABET, k=length + 4))


def _pay_mapping(amount: float, name: str, order_id: str = "",
                 include_current: bool = False) -> dict:
    day = _todays_payments()
    today_total = sum(p["amount"] for p in day)
    today_count = len(day)
    if include_current:
        today_total += amount
        today_count += 1

    base = today_total if config.SHARE_BASE == "today" else amount
    rio = base * config.RIO_PCT / 100.0
    marco = base * config.MARCO_PCT / 100.0

    return {
        "{amount}": fmt_inr(amount),
        "{name}": name or "",
        "{orderid}": order_id or "",          # per-payment id, e.g. ANI7F3K9Q
        "{rioshare}": fmt_inr(rio),
        "{marco}": fmt_inr(marco),
        "{total}": str(today_count),          # count of payments today
        "{todaytotal}": fmt_inr(today_total),  # total amount collected today
    }


def _substitute(text: str, entities, mapping: dict):
    """Replace {tokens} in `text` with their values while keeping every entity
    (bold/links AND premium custom emoji) attached to the right span. Entity
    offsets are counted in UTF-16 code units. Returns (new_text, new_entities).
    """
    text = text or ""
    entities = [copy.copy(e) for e in (entities or [])]
    if not mapping:
        return text, entities

    pattern = re.compile("|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)))

    repls = []       # (start_u16, end_u16, delta_u16) in ORIGINAL coordinates
    parts, last = [], 0
    for m in pattern.finditer(text):
        val = mapping[m.group(0)]
        start_u16 = _u16(text[:m.start()])
        old_u16 = _u16(m.group(0))
        new_u16 = _u16(val)
        repls.append((start_u16, start_u16 + old_u16, new_u16 - old_u16))
        parts.append(text[last:m.start()])
        parts.append(val)
        last = m.end()
    parts.append(text[last:])
    new_text = "".join(parts)

    if repls:
        for e in entities:
            e_start, e_end = e.offset, e.offset + e.length
            off_delta = len_delta = 0
            for rs, re_, d in repls:
                if re_ <= e_start:
                    off_delta += d            # replacement is before -> shift
                elif rs >= e_end:
                    pass                      # replacement is after -> no effect
                else:
                    len_delta += d            # replacement is inside -> resize
            e.offset += off_delta
            e.length = max(0, e.length + len_delta)
    return new_text, entities


async def _resolve_template(kind: str):
    """Return (text, entities) for kind in {'done', 'channel'}.

    A message reference (set by replying to a post) is preferred so formatting
    and premium emoji are kept verbatim; otherwise the plain-text template.
    """
    ref = _pay.get(f"{kind}_ref")
    if ref:
        try:
            src = await client.get_messages(ref["chat_id"], ids=ref["message_id"])
        except Exception:  # noqa: BLE001
            src = None
        if src is not None:
            return src.message or "", list(src.entities or [])
    return _pay.get(f"{kind}_template", ""), []


async def _render(kind: str, amount: float, name: str, order_id: str = "",
                  include_current: bool = False):
    text, ents = await _resolve_template(kind)
    return _substitute(
        text,
        ents,
        _pay_mapping(amount, name, order_id=order_id, include_current=include_current),
    )


async def cmd_add(event) -> None:
    m = re.match(r"^/add(?:\s+(\S+)(?:\s+([\s\S]+))?)?$", event.raw_text or "")
    amount_raw = m.group(1) if m else None
    name = (m.group(2).strip() if m and m.group(2) else "")

    if not amount_raw:
        await event.edit("Usage: reply to an image with  /add <amount> [name]")
        return
    try:
        amount = parse_amount(amount_raw)
    except ValueError:
        await event.edit(f"Amount '{amount_raw}' is not a valid number.")
        return

    reply = await event.get_reply_message()
    if not reply or not reply.media:
        await event.edit("Reply to an image/media message with /add.")
        return
    if not _pay.get("post_channel"):
        await event.edit("No post channel set. Type .setchannel in the target channel first.")
        return

    # Snapshot the configured target once. .setchannel cannot move this upload
    # to a different chat while template resolution or Telegram I/O is pending.
    target_channel = int(_pay["post_channel"])
    order_id = _generate_order_id()
    payment = {
        "amount": amount,
        "name": name,
        "order_id": order_id,
        "ts": _now_ts(),
        "status": "pending",
        "post_chat_id": target_channel,
        "post_message_id": None,
    }
    _pay["payments"].append(payment)
    _save_pay()

    # Pending records are excluded from public stats. Render this post with the
    # current payment explicitly included, then finalize it only after Telegram
    # returns the durable message association.
    try:
        ch_text, ch_ents = await _render(
            "channel", amount, name, order_id=order_id, include_current=True
        )
    except Exception as e:  # noqa: BLE001
        payment["status"] = "failed"
        payment["error"] = f"render: {type(e).__name__}: {e}"
        _save_pay()
        await event.edit(f"Could not render payment post: {type(e).__name__}: {e}")
        return

    marker = _expect_automatic_outgoing(target_channel, [ch_text])
    try:
        post_result = await client.send_file(
            target_channel,
            file=reply.media,
            caption=ch_text,
            formatting_entities=ch_ents or None,
        )
    except Exception as e:  # noqa: BLE001
        _cancel_automatic_outgoing(target_channel, marker)
        payment["status"] = "failed"
        payment["error"] = f"upload: {type(e).__name__}: {e}"
        _save_pay()
        await event.edit(f"Posting to the channel failed: {type(e).__name__}: {e}")
        return
    _finish_automatic_outgoing(target_channel, marker, post_result)

    post_ids = sorted(_sent_message_ids(post_result))
    if not post_ids:
        payment["status"] = "untracked"
        payment["error"] = "Telegram returned no posted message ID"
        _save_pay()
        await event.edit(
            "Payment posted, but Telegram returned no message ID. "
            "It was not added to stats because /cancel could not track it."
        )
        return

    payment["post_message_id"] = post_ids[0]
    payment["status"] = "valid"
    payment.pop("error", None)
    _save_pay()

    # 2. Message the user in the private chat (edit the /add command into it).
    us_text, us_ents = await _render("done", amount, name, order_id=order_id)
    try:
        await event.edit(us_text, formatting_entities=us_ents or None)
    except Exception:  # noqa: BLE001 - fall back to a fresh message
        await event.respond(us_text, formatting_entities=us_ents or None)

    count = len(_todays_payments())
    print(ui.green("[pay] ") + f"{fmt_inr(amount)} {name} -> posted (#{count} today)", flush=True)


def _payment_for_post(chat_id: int, message_id: int):
    """Return the payment linked to a generated channel post, newest first."""
    for payment in reversed(_pay.get("payments", [])):
        if (
            payment.get("post_chat_id") == int(chat_id)
            and payment.get("post_message_id") == int(message_id)
        ):
            return payment
    return None


def _fake_caption(message):
    """Prefix a channel payment post with a bold FAKE PAYMENT marker.

    Existing caption entities are cloned and shifted in Telegram's UTF-16
    coordinates, preserving links, formatting, and premium emoji.
    """
    label = "FAKE PAYMENT"
    prefix = f"{label}\n\n"
    text = message.message or ""
    if text.startswith(prefix) or text == label:
        return text, list(message.entities or [])

    shift = _u16(prefix)
    entities = [copy.copy(entity) for entity in (message.entities or [])]
    for entity in entities:
        entity.offset += shift
    entities.insert(0, MessageEntityBold(offset=0, length=_u16(label)))

    # Media captions are limited to 1024 Telegram text units. Preserve as much
    # of the original caption as possible while guaranteeing the fake marker
    # can always be applied, even when the original caption was at the limit.
    new_text = prefix + text
    caption_limit = 1024
    while _u16(new_text) > caption_limit:
        new_text = new_text[:-1]
    final_length = _u16(new_text)
    kept_entities = []
    for entity in entities:
        if entity.offset >= final_length:
            continue
        entity.length = min(entity.length, final_length - entity.offset)
        if entity.length > 0:
            kept_entities.append(entity)
    return new_text, kept_entities


async def cmd_cancel(event) -> None:
    """Mark a replied payment-channel post fake and exclude it from stats."""
    reply = await event.get_reply_message()
    if reply is None:
        await event.edit("Reply to a payment post with /cancel.")
        return

    payment = _payment_for_post(event.chat_id, reply.id)
    if payment is None:
        await event.edit(
            "That post is not linked to a recorded payment in this channel. "
            "Only payment posts created after /cancel support can be matched."
        )
        return
    if payment.get("status") == "fake":
        await event.edit("Payment is already marked fake.")
        return

    fake_text, fake_entities = _fake_caption(reply)
    if payment.get("status") != "cancel_pending":
        payment["cancel_previous_status"] = payment.get("status", "valid")
    payment["status"] = "cancel_pending"
    payment["cancel_command_message_id"] = int(event.id)
    _save_pay()

    peer = await event.get_input_chat()
    edit_succeeded = False
    try:
        await client.edit_message(
            peer,
            reply.id,
            fake_text,
            formatting_entities=fake_entities or None,
        )
        edit_succeeded = True
    except MessageNotModifiedError:
        # A previous attempt may have edited Telegram before its response was
        # lost. Treat the already-applied marker as success.
        edit_succeeded = True
    except Exception as e:  # noqa: BLE001
        # Resolve ambiguous network failures by fetching the actual post. Never
        # restore stats unless a successful fetch proves the marker is absent.
        current = None
        fetch_succeeded = False
        try:
            current = await client.get_messages(peer, ids=reply.id)
            fetch_succeeded = True
        except Exception:  # noqa: BLE001
            pass
        current_text = (getattr(current, "message", "") or "") if current else ""
        if current_text.startswith("FAKE PAYMENT\n\n") or current_text == "FAKE PAYMENT":
            edit_succeeded = True
        elif fetch_succeeded:
            payment["status"] = payment.pop("cancel_previous_status", "valid")
            payment.pop("cancel_command_message_id", None)
            _save_pay()
            await event.edit(f"Could not mark payment fake: {type(e).__name__}: {e}")
            return
        else:
            # Telegram may have applied the edit before the response was lost.
            # Keep this record excluded from stats until /cancel is retried and
            # the post can be reconciled; restoring it could count a visible fake.
            payment["cancel_error"] = f"{type(e).__name__}: {e}"
            _save_pay()
            await event.edit(
                "Cancellation could not be verified. Payment is excluded from "
                "stats; reply /cancel to the payment post again."
            )
            return

    if edit_succeeded:
        payment["status"] = "fake"
        payment["canceled_ts"] = _now_ts()
        payment.pop("cancel_previous_status", None)
        payment.pop("cancel_error", None)
        payment.pop("error", None)
        _save_pay()

    day = _todays_payments()
    total = sum(p["amount"] for p in day)
    try:
        await event.delete()
    except Exception:  # noqa: BLE001
        await event.edit(
            f"Payment marked fake. Today: {fmt_inr(total)} / {len(day)} payment(s)."
        )
    print(
        ui.yellow("[pay fake] ")
        + f"{fmt_inr(payment['amount'])} {payment.get('name', '')} -> "
        + f"today {fmt_inr(total)} / {len(day)} payment(s)",
        flush=True,
    )


async def _ack(event, text: str) -> None:
    """Report a result, falling back to a fresh message if editing the command
    fails — so a completed action is never mis-reported as 'Command failed'."""
    try:
        await event.edit(text)
    except MessageNotModifiedError:
        pass
    except Exception:  # noqa: BLE001
        try:
            await event.respond(text)
        except Exception:  # noqa: BLE001
            pass


async def _set_template(event, kind: str, cmd: str, label: str) -> None:
    """Store a template. Replying to a post keeps its formatting + premium emoji
    (stored as a message reference); inline text is stored as plain text."""
    reply = await event.get_reply_message()
    if reply is not None and (reply.raw_text or ""):
        _pay[f"{kind}_ref"] = {"chat_id": int(reply.chat_id), "message_id": int(reply.id)}
        _pay[f"{kind}_template"] = reply.raw_text  # plain fallback if the post is deleted
        _save_pay()
        await _ack(event, f"{label} saved from that post - formatting and premium emoji kept.")
        return

    m = re.match(rf"^{re.escape(cmd)}(?:\s+([\s\S]+))?$", event.raw_text or "")
    template = (m.group(1).strip() if m and m.group(1) else "")
    if not template:
        await _ack(event, f"Send the text after {cmd}, or reply to a formatted post with {cmd}.")
        return
    _pay[f"{kind}_template"] = template
    _pay.pop(f"{kind}_ref", None)
    _save_pay()
    await _ack(event, f"{label} saved ({len(template)} chars, plain text).")


async def cmd_setdone(event) -> None:
    """Set the message the user gets in the private chat after /add."""
    await _set_template(event, "done", "/setdone", "Private-chat message")


async def cmd_setchannelpost(event) -> None:
    """Set the caption used for the channel post after /add."""
    await _set_template(event, "channel", "/setchannelpostofpayment", "Channel post caption")


async def cmd_setchannel(event) -> None:
    """Set the CURRENT chat/channel as the post target."""
    chat_id = event.chat_id
    chat = await event.get_chat()
    title = getattr(chat, "title", None) or "this chat"
    _pay["post_channel"] = chat_id
    _save_pay()
    await event.edit(f"Post channel set here: {title} ({chat_id})")


async def cmd_stats(event) -> None:
    day = _todays_payments()
    total = sum(p["amount"] for p in day)
    rio = total * config.RIO_PCT / 100.0
    marco = total * config.MARCO_PCT / 100.0
    await event.edit(
        f"Today - {fmt_inr(total)} - {len(day)} payments\n"
        f"Rio {fmt_inr(rio)} - Marco {fmt_inr(marco)}"
    )


async def cmd_clear(event) -> None:
    """Exclude today's valid payments from stats without deleting audit links."""
    key = _today_key()
    cleared = 0
    cleared_ts = _now_ts()
    for payment in _pay.get("payments", []):
        if (
            payment.get("status", "valid") == "valid"
            and _today_key(payment["ts"]) == key
        ):
            payment["status"] = "cleared"
            payment["cleared_ts"] = cleared_ts
            cleared += 1
    _save_pay()
    await event.edit(
        f"Today cleared - {cleared} payment(s) removed from stats.\n"
        f"Today - {fmt_inr(0)} - 0 payments"
    )


HELP_TEXT = (
    "<b>Payment logger</b> (any chat)\n"
    "<code>.ping</code> - verify the quick-reply userbot is running\n"
    "<code>/add &lt;amount&gt; [name]</code> - reply to an image: log it, message the user, post to the channel\n"
    "<code>/setdone &lt;template&gt;</code> - message sent to the user in the private chat\n"
    "<code>/setchannelpostofpayment &lt;template&gt;</code> - caption for the channel post\n"
    "<code>.setchannel</code> - type it in a channel to post media there\n"
    "<code>/stats</code> - today's total, count, and split\n"
    "<code>/cancel</code> - in the upload channel, reply to a payment post: mark it fake and remove it from stats\n"
    "<code>/clear</code> - reset today's stats to zero\n\n"
    "<b>Template parameters</b>\n"
    "<code>{amount}</code> this payment - <code>{name}</code> name from /add - "
    "<code>{orderid}</code> unique order id (e.g. ANI7F3K9Q) - "
    "<code>{rioshare}</code> Rio's share - <code>{marco}</code> Marco's share - "
    "<code>{total}</code> payment count today - <code>{todaytotal}</code> collected today\n"
    "<blockquote>Reply to a formatted post with /setdone or "
    "/setchannelpostofpayment to keep bold, links and premium emoji.</blockquote>\n"
    "<b>Greeting</b> (Saved Messages)\n"
    "<code>/set</code> reply to a post - <code>/unset</code> clear - <code>/show</code> status - "
    "<code>/away on|off|status</code>\n"
    "<b>Broadcast</b> (Saved Messages)\n"
    "<code>/broadcast 9:30 AM 18 JUL THANKS FOR</code> - reply to a post to copy it to every chat "
    "where you sent the keyword from that IST time until now\n"
    "<b>Block</b>\n"
    "<code>L</code> in a private chat - clear the conversation for both sides and block"
)


def _as_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime (Telethon dates are normally UTC-aware)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_broadcast_command(text: str, command_date: datetime):
    """Parse `/broadcast TIME DATE [YEAR] KEYWORD` using IST."""
    match = BROADCAST_RE.match(text.strip())
    if not match:
        raise ValueError(
            "Format: /broadcast 9:30 AM 18 JUL THANKS FOR\n"
            "Optional year: /broadcast 9:30 AM 18 JUL 2026 THANKS FOR"
        )

    hour, minute = int(match.group(1)), int(match.group(2))
    am_pm = match.group(3).upper()
    day = int(match.group(4))
    month_name = match.group(5).upper()
    explicit_year = match.group(6)
    keyword = match.group(7).strip()

    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        raise ValueError("Invalid time. Use 12-hour IST time, for example 9:30 AM.")
    month = MONTHS.get(month_name)
    if month is None:
        raise ValueError(f"Invalid month: {match.group(5)}")

    end_utc = _as_utc(command_date)
    end_ist = end_utc.astimezone(IST)
    year = int(explicit_year) if explicit_year else end_ist.year
    hour_24 = hour % 12 + (12 if am_pm == "PM" else 0)
    try:
        start_ist = datetime(year, month, day, hour_24, minute, tzinfo=IST)
    except ValueError as e:
        raise ValueError(f"Invalid date: {e}") from e

    # With no year, use the current IST calendar year. A future start is
    # rejected instead of silently scanning a previous year and broadcasting
    # to a much wider set of chats than the owner intended.
    if start_ist > end_ist:
        raise ValueError(
            "Start time is in the future (IST). Add the intended year if needed."
        )

    return start_ist.astimezone(timezone.utc), end_utc, keyword


async def _find_broadcast_targets(start_utc: datetime, end_utc: datetime, keyword: str):
    """Find unique chats where this account sent `keyword` during the window."""
    targets = {}
    needle = keyword.casefold()

    # entity=None performs Telegram global search. The pinned Telethon 1.36
    # cannot combine global search with from_user="me" (InputPeerEmpty raises
    # during request construction), so authorship is enforced locally below.
    # Search one second past the command because Telegram's offset_date is
    # exclusive and message timestamps have one-second precision; the local
    # end-date check keeps the requested interval inclusive and exact.
    async for message in client.iter_messages(
        None,
        search=keyword,
        offset_date=end_utc + timedelta(seconds=1),
    ):
        message_date = getattr(message, "date", None)
        if message_date is None:
            continue
        message_date = _as_utc(message_date)
        if message_date < start_utc:
            break  # global search is newest-first
        if message_date > end_utc:
            continue
        if not getattr(message, "out", False):
            continue
        if getattr(message, "sender_id", None) != _state["self_id"]:
            continue
        chat_id = getattr(message, "chat_id", None)
        if chat_id is None or chat_id == _state["self_id"]:
            continue  # never target Saved Messages
        body = getattr(message, "raw_text", "") or ""
        if needle not in body.casefold():
            continue  # Telegram search can be fuzzy/token-based; require exact phrase
        if chat_id in targets:
            continue
        try:
            peer = await message.get_input_chat()
            if peer is None:
                peer = await client.get_input_entity(message.peer_id)
        except Exception as e:  # noqa: BLE001
            ui.warn(f"Couldn't resolve matching chat {chat_id}: {type(e).__name__}")
            continue
        targets[chat_id] = peer
    return targets


async def _send_broadcast_copy(chat_id: int, peer, src):
    """Copy once, waiting and retrying one time if Telegram imposes a flood wait."""
    for attempt in (1, 2):
        marker = _expect_automatic_outgoing(chat_id, [src.message or ""])
        try:
            result = await _copy_to(peer, src)
        except FloodWaitError as e:
            _cancel_automatic_outgoing(chat_id, marker)
            if attempt == 2:
                raise
            wait = max(1, int(e.seconds) + 1)
            ui.warn(f"Broadcast flood wait: sleeping {wait}s, then retrying.")
            await asyncio.sleep(wait)
            continue
        except Exception:
            _cancel_automatic_outgoing(chat_id, marker)
            raise
        _finish_automatic_outgoing(chat_id, marker, result)
        return


async def _run_broadcast(event, src, start_utc: datetime, end_utc: datetime,
                         keyword: str) -> None:
    """Discover matching chats, then copy the replied post to each one."""
    start_ist = start_utc.astimezone(IST)
    end_ist = end_utc.astimezone(IST)
    progress = await event.reply(
        f'Scanning for "{keyword}" from '
        f'{start_ist.strftime("%d %b %Y, %I:%M %p")} IST to '
        f'{end_ist.strftime("%d %b %Y, %I:%M %p")} IST...'
    )

    try:
        targets = await _find_broadcast_targets(start_utc, end_utc, keyword)
    except Exception as e:  # noqa: BLE001
        await progress.edit(f"Broadcast search failed: {type(e).__name__}: {e}")
        return

    if not targets:
        await progress.edit(
            f'No chats found where you sent "{keyword}" in that IST time window.'
        )
        return

    sent = 0
    failures = []
    await progress.edit(f"Found {len(targets)} matching chat(s). Broadcasting...")
    for chat_id, peer in targets.items():
        try:
            await _send_broadcast_copy(chat_id, peer, src)
            sent += 1
            print(ui.green("[broadcast] ") + f"sent to {chat_id}", flush=True)
        except Exception as e:  # noqa: BLE001
            failures.append((chat_id, type(e).__name__))
            ui.error(f"broadcast failed for {chat_id}: {type(e).__name__}: {e}")
        await asyncio.sleep(0.35)

    result = (
        "Broadcast complete.\n"
        f"Keyword: {keyword}\n"
        f"Matched chats: {len(targets)}\n"
        f"Sent: {sent}\n"
        f"Failed: {len(failures)}"
    )
    if failures:
        preview = ", ".join(f"{chat_id} ({name})" for chat_id, name in failures[:8])
        result += f"\nFailures: {preview}"
        if len(failures) > 8:
            result += f" +{len(failures) - 8} more"
    await progress.edit(result)


async def send_greeting(event, generation: int) -> str:
    """Send the greeting if the owner is still away.

    Prefers the /set greeting post; falls back to the Business away message.
    Returns 'greeting', 'away', 'suppressed', or 'none'.
    """
    peer = await event.get_input_sender()
    user_id = event.sender_id

    g = _load_greeting()
    if g:
        try:
            src = await client.get_messages(g["chat_id"], ids=g["message_id"])
        except Exception:
            src = None
        if src is not None:
            if not await _away_still_allowed(generation):
                return "suppressed"
            marker = _expect_automatic_outgoing(user_id, [src.message or ""])
            try:
                result = await _copy_to(peer, src)
            except Exception:
                _cancel_automatic_outgoing(user_id, marker)
                raise
            _finish_automatic_outgoing(user_id, marker, result)
            return "greeting"

    sid = await _away_shortcut_id()
    if sid:
        msgs = await client(GetQuickReplyMessagesRequest(shortcut_id=sid, hash=0, id=None))
        shortcut_msgs = list(getattr(msgs, "messages", []) or [])
        ids = [m.id for m in shortcut_msgs]
        if ids:
            if not await _away_still_allowed(generation):
                return "suppressed"
            marker = _expect_automatic_outgoing(
                user_id,
                [getattr(m, "message", "") or "" for m in shortcut_msgs],
            )
            try:
                result = await client(SendQuickReplyMessagesRequest(
                    peer=peer, shortcut_id=sid, id=ids,
                    random_id=[random.randrange(-(2 ** 63), 2 ** 63) for _ in ids],
                ))
            except Exception:
                _cancel_automatic_outgoing(user_id, marker)
                raise
            _finish_automatic_outgoing(user_id, marker, result)
            return "away"
    return "none"


async def _swap_in_greeting(new_link: str) -> str:
    """Keep the /set greeting post's link current. Returns a status string so
    the caller can always report what happened."""
    g = _load_greeting()
    if not g:
        return "no greeting set"
    try:
        src = await client.get_messages(g["chat_id"], ids=g["message_id"])
    except Exception as e:  # noqa: BLE001
        return f"fetch error: {type(e).__name__}"
    if src is None:
        return "post not found (re-set with /set)"
    swapped = _swap_link(src.message or "", src.entities, new_link)
    if swapped is None:
        return "no invite link in the post"
    new_text, ents = swapped
    try:
        await client.edit_message(g["chat_id"], g["message_id"], new_text,
                                  formatting_entities=ents or None)
        return "updated"
    except MessageNotModifiedError:
        return "unchanged"
    except Exception as e:  # noqa: BLE001
        return f"edit error: {type(e).__name__}: {e}"


async def _current_link():
    """The latest invite link: what the guard last sent, else whatever link is
    already in the /SHORTCUT post."""
    if _state["last"]:
        return _state["last"]
    try:
        res = await client(GetQuickRepliesRequest(hash=0))
        for q in getattr(res, "quick_replies", []) or []:
            if getattr(q, "shortcut", None) != config.SHORTCUT:
                continue
            msgs = await client(GetQuickReplyMessagesRequest(
                shortcut_id=q.shortcut_id, hash=0, id=None))
            for m in getattr(msgs, "messages", []) or []:
                hit = FIND_LINK_RE.search(m.message or "")
                if hit:
                    return hit.group(0)
                for e in (m.entities or []):
                    url = getattr(e, "url", None)
                    if url and FIND_LINK_RE.search(url):
                        return url
    except Exception:
        pass
    return None


async def _is_new_user(event) -> bool:
    """True only if this is the first message in the conversation."""
    try:
        prev = await client.get_messages(event.sender_id, limit=2)
        return len(prev) <= 1
    except Exception:
        return True


async def _handle_link(event, link: str) -> None:
    if link == _state["last"]:
        return
    status = await update_link(config.SHORTCUT, link)
    print(ui.green(f"[/{config.SHORTCUT}] ") + f"{ui.bold(link)} ({status})", flush=True)
    if config.SWAP_GREETING:
        g_status = await _swap_in_greeting(link)
        print(ui.green("[greeting] ") + f"{ui.bold(link)} ({g_status})", flush=True)
    else:
        print(ui.dim("[greeting] link frozen (SWAP_GREETING=0)"), flush=True)
    _state["last"] = link


async def _clear_and_block(event) -> None:
    """Delete a private conversation for both sides and block its other user."""
    peer = await event.get_input_chat()
    errors = []
    try:
        await client(DeleteHistoryRequest(
            peer=peer,
            max_id=0,
            just_clear=False,
            revoke=True,
        ))
    except Exception as e:  # noqa: BLE001
        errors.append(f"clear failed ({type(e).__name__}: {e})")
    try:
        await client(BlockRequest(id=peer))
    except Exception as e:  # noqa: BLE001
        errors.append(f"block failed ({type(e).__name__}: {e})")
    if errors:
        raise RuntimeError("; ".join(errors))
    print(ui.green("[blocked] ") + f"cleared private chat with {event.chat_id}", flush=True)


async def _handle_outgoing(event):
    """Track owner activity, handle commands, and process L."""
    chat_id = event.chat_id
    raw_text = event.raw_text or ""

    # Programmatic greeting sends arrive as outgoing updates too. Consume only
    # the exact message body we registered, not every send in that chat.
    if chat_id != _state["self_id"] and await _consume_automatic_outgoing(event):
        return

    _mark_owner_active()

    # Payment logger + help work in ANY chat (a DM with a customer, the channel,
    # or Saved Messages) — handle them before the Saved-Messages-only gate below.
    # /setdone and /setchannelpostofpayment are matched before /set (Saved
    # Messages) so the longer commands win.
    low_cmd = raw_text.strip().lower()
    if low_cmd in (".ping", "/ping"):
        await event.edit("Quick-reply userbot is running.")
        return
    if low_cmd in (".help", "/help", "/commands", "/start"):
        await event.edit(HELP_TEXT, parse_mode="html")
        return
    if low_cmd.startswith("/add"):
        async with _payment_lock:
            await cmd_add(event)
        return
    if low_cmd == "/cancel":
        async with _payment_lock:
            await cmd_cancel(event)
        return
    if low_cmd.startswith("/setchannelpostofpayment"):
        async with _payment_lock:
            await cmd_setchannelpost(event)
        return
    if low_cmd.startswith("/setdone"):
        async with _payment_lock:
            await cmd_setdone(event)
        return
    if low_cmd == ".setchannel":
        async with _payment_lock:
            await cmd_setchannel(event)
        return
    if low_cmd == "/stats":
        async with _payment_lock:
            await cmd_stats(event)
        return
    if low_cmd == "/clear":
        async with _payment_lock:
            await cmd_clear(event)
        return

    # Only a plain, unforwarded, text-only uppercase L triggers this destructive
    # action. Whitespace, captions, forwarded messages, and Saved Messages do not.
    if (
        event.is_private
        and chat_id != _state["self_id"]
        and raw_text == "L"
        and event.message.media is None
        and event.message.fwd_from is None
        and not event.message.entities
    ):
        try:
            await _clear_and_block(event)
        except Exception as e:  # noqa: BLE001
            ui.error(f"clear/block failed for {chat_id}: {type(e).__name__}: {e}")
        return

    if chat_id != _state["self_id"]:
        return

    text = raw_text.strip()
    low = text.lower()
    if low.startswith("/broadcast"):
        reply = await event.get_reply_message()
        if reply is None:
            await event.reply(
                "Reply to the message you want to send, then use:\n"
                "/broadcast 9:30 AM 18 JUL THANKS FOR"
            )
            return
        try:
            start_utc, end_utc, keyword = _parse_broadcast_command(text, event.date)
        except ValueError as e:
            await event.reply(str(e))
            return
        if _broadcast_lock.locked():
            await event.reply("A broadcast is already running. Wait for it to finish.")
            return
        async with _broadcast_lock:
            await _run_broadcast(event, reply, start_utc, end_utc, keyword)
    elif low.startswith("/set"):
        reply = await event.get_reply_message()
        if reply is None:
            await event.reply("Reply to a post with /set to use it as the greeting.")
            return
        _save_greeting(event.chat_id, reply.id)
        # Apply the current link right away — no restart, no waiting for the
        # next rotation.
        link = await _current_link()
        note = ""
        if link and config.SWAP_GREETING:
            result = await _swap_in_greeting(link)
            note = f"\nLink -> {link} ({result})"
        await event.reply("Greeting saved. New users who DM you first get this." + note)
    elif low == "/unset":
        await event.reply("Greeting cleared." if _clear_greeting() else "No greeting was set.")
    elif low == "/away on":
        try:
            _set_away_enabled(True)
        except OSError as e:
            await event.reply(f"Could not enable away messages: {e}")
        else:
            await event.reply("Away messages enabled.")
    elif low == "/away off":
        try:
            _set_away_enabled(False)
        except OSError as e:
            await event.reply(f"Could not disable away messages: {e}")
        else:
            await event.reply("Away messages disabled.")
    elif low in ("/away", "/away status"):
        state = "enabled" if _state["away_enabled"] else "disabled"
        await event.reply(f"Away messages are {state}.")
    elif low == "/show":
        greeting = "set" if _load_greeting() else "not set"
        away = "enabled" if _state["away_enabled"] else "disabled"
        await event.reply(f"Greeting is {greeting}. Away messages are {away}.")


async def on_outgoing(event):
    """Keep one bad payment record or command from disabling all commands."""
    try:
        await _handle_outgoing(event)
    except Exception as e:  # noqa: BLE001
        text = (getattr(event, "raw_text", "") or "").strip()
        ui.error(f"outgoing handler failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        if text.startswith(("/", ".")) or text == "L":
            detail = f"{type(e).__name__}: {e}".strip()
            if len(detail) > 350:
                detail = detail[:349] + "\u2026"
            try:
                await event.edit(f"Command failed - {detail}")
            except Exception:  # noqa: BLE001
                pass


async def on_msg(event):
    if not event.is_private:
        return
    sender = event.sender_id
    src = _state["source_id"]

    # 1) Invite link relayed from the guard -> update the /demo + greeting link.
    match = LINK_RE.search(event.raw_text or "")
    if match and (src is None or sender == src):
        try:
            await _handle_link(event, match.group(0))
        except Exception as e:  # noqa: BLE001
            ui.error(f"link update failed: {type(e).__name__}: {e}")
            if "PREMIUM" in str(e).upper():
                ui.warn("Business quick replies require Telegram Premium.")
        return

    # 2) A brand-new DM conversation -> send the greeting (once per user), but
    # never while the owner is currently online/recently active.
    if not _state["away_enabled"]:
        return
    if not sender or sender in (_state["self_id"], src) or sender in _greeted:
        return
    if not await _is_new_user(event):
        _greeted.add(sender)          # existing contact -> remember, don't greet
        _save_greeted()
        return
    generation = _state["activity_generation"]
    if await _owner_is_online():
        # Do NOT mark greeted: once the owner is offline again, a later message
        # from this user should still get the away reply.
        print(ui.dim(f"[greet] owner online; will retry when offline: {sender}"), flush=True)
        return
    try:
        status = await send_greeting(event, generation)
        _greeted.add(sender)
        _save_greeted()
        if status == "none":
            ui.warn(f"No greeting set (reply to a post with /set). Skipped {sender}.")
        elif status == "suppressed":
            print(ui.dim(f"[greet] owner became active or away was disabled; skipped {sender}"),
                  flush=True)
        else:
            print(ui.green("[greet] ") + f"sent {status} to {sender}", flush=True)
    except Exception as e:  # noqa: BLE001
        ui.error(f"greet failed for {sender}: {type(e).__name__}: {e}")


async def main() -> None:
    global client
    config.require("API_ID", "API_HASH")

    if not _acquire_single_instance_lock():
        ui.error(
            "Another quickreply.py is already running on this machine. Stop it "
            "first, e.g.  pkill -f quickreply.py  (check screen/tmux/systemd "
            "too). Two copies make commands randomly show 'saved' then 'failed'."
        )
        return

    client = TelegramClient(config.QR_SESSION, config.API_ID, config.API_HASH)
    await client.start()  # prompts phone + OTP for THIS account on first run
    me = await client.get_me()
    _state["self_id"] = me.id

    global _greeted, _pay
    _greeted = _load_greeted()
    _pay = _load_pay()

    src_raw = config.LINK_SOURCE_RAW.strip()
    if not src_raw:
        src_raw = ui.ask(
            "Account that SENDS the invite link (username/id, blank = any)",
            default="",
        )
        if src_raw:
            config.save_env({"LINK_SOURCE": src_raw})
    if src_raw:
        try:
            ent = await client.get_entity(config.coerce(src_raw))
            _state["source_id"] = ent.id
        except Exception:
            ui.warn("Couldn't resolve LINK_SOURCE — accepting links from any private chat.")

    # Incoming DMs -> link update / greet. All outgoing messages -> presence,
    # Saved Messages controls, and the private-chat L action.
    client.add_event_handler(on_msg, events.NewMessage(incoming=True))
    client.add_event_handler(on_outgoing, events.NewMessage(outgoing=True))

    where = f"from {src_raw}" if _state["source_id"] else "from any private chat"
    ui.banner("Quick-reply updater - running")
    ui.success(f"As {ui.bold(me.first_name)} (id {me.id}).")
    ui.info(f"Keeping /{ui.bold(config.SHORTCUT)} link current ({where}).")
    if _state["away_enabled"]:
        has_greeting = _load_greeting() is not None
        state = "set" if has_greeting else ui.yellow("not set - reply to a post with /set")
        ui.info(f"First-time DMs get the greeting post only while you are offline [{state}]. "
                + ui.dim(f"({len(_greeted)} already greeted)"))
    else:
        ui.info("Away messages are disabled.")
    pc = _pay.get("post_channel")
    day = _todays_payments()
    total = sum(p["amount"] for p in day)
    pc_state = str(pc) if pc else ui.yellow("not set - type .setchannel in a channel")
    ui.info(f"Payment logger: post channel [{pc_state}]. "
            + ui.dim(f"today {fmt_inr(total)} / {len(day)} payment(s). '.help' for commands."))
    print(ui.dim("Ctrl+C to stop."))
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
