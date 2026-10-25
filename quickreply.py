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
  /add <amt> <name> <kw...>      ask the admin bot for buyer-bound group
                                 link(s), replace {link} in /setdone, and
                                 optionally post a replied proof image
  /setdone <template>            message sent to the user in the private chat
  /setchannelpostofpayment <t>   caption for the channel post
  .setchannel [id|@username]     the payment upload channel: type it inside the
                                 channel, or name one from any chat. Receipts
                                 that never posted follow the new target.
  .unban <id|@username>          lift that user's ban in every group this
                                 account admins (or reply to the user)
  /stats                         today's total (INR) + count + split
  /cancel                        in the upload channel, reply to a payment post
                                 -> mark it fake, remove it from today's stats,
                                    and revoke/remove buyer-bound access
  /clear                         reset today's stats to zero
  .ping                          verify the quick-reply userbot is running

Backup + screenshot ZIPs (Saved Messages):
  .save                          full backup ZIP (profile, photos, Business,
                                 every quick reply + media, data/*.json) sent
                                 to Saved Messages; no session or API keys
  .sszip [today|yesterday|YYYY-MM-DD]
                                 that IST day's payment screenshots as a ZIP
  .ssauto [on|off]               auto-send each finished day's ZIP to SS_CHANNEL
  .ssto <@channel|id>            where the daily ZIPs go (default @ohthatsthepoint)
  .help                          show every command and template parameter

DM country separator (any chat):
  .separator on|off|status       file DMs from outside India into the
                                 "Videshi" folder and out of "Unread";
                                 Indian DMs stay put
  .separator scan                re-sort DMs now (also runs on every start);
                                 when Videshi is full (200) the quietest
                                 chats leave to make room for new ones
  .separator check               in a DM: its detected country -> Saved Msgs
  .gdpa                          in a DM: delete the command and put the
                                 user's country's per capita income ($) in
                                 the draft (not sent)

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
import tempfile
import time
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    MessageNotModifiedError,
    UserNotParticipantError,
)
from telethon.tl import types as tl_types
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.functions.contacts import BlockRequest
from telethon.tl.functions.messages import (
    DeleteExportedChatInviteRequest,
    DeleteHistoryRequest,
    EditExportedChatInviteRequest,
    EditMessageRequest,
    ExportChatInviteRequest,
    GetQuickRepliesRequest,
    GetQuickReplyMessagesRequest,
    HideChatJoinRequestRequest,
    SaveDraftRequest,
    SendMessageRequest,
    SendQuickReplyMessagesRequest,
)
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    ChannelParticipantsBanned,
    ChannelParticipantsKicked,
    ChatBannedRights,
    InputPeerSelf,
    InputQuickReplyShortcut,
    InputUserSelf,
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageMediaWebPage,
    UpdatePendingJoinRequests,
    UserStatusOnline,
)
from telethon.utils import get_peer_id

import config
import ui
from bot.utils import (
    fuzzy_group_threshold,
    group_fuzzy_score,
    group_literal_rank,
)
from runtime_lock import ProcessLock
import quickreply_extras as extras
import quickreply_restore as restore
import percapita
import separator
import category

# Shared link store (repo root): the BOT writes links here; this userbot only
# reads them to fill {link} in the payment message. It never searches groups.
try:
    import linkstore
except Exception:  # noqa: BLE001
    linkstore = None

# The link the guard sends us (full https invite link).
LINK_RE = re.compile(r"https?://t\.me/(?:joinchat/|\+)[\w-]+", re.IGNORECASE)
# The link inside the saved post (may or may not include the scheme).
FIND_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)[\w-]+", re.IGNORECASE)

# Dot commands accept a leading "." or "/" (both are typed in practice). The
# patterns are anchored so `.setchannel` can never swallow the much longer
# `/setchannelpostofpayment`.
_SETCHANNEL_RE = re.compile(r"^[./]setchannel(?:\s+([\s\S]+))?$", re.IGNORECASE)
_UNBAN_RE = re.compile(r"^[./]unban(?:\s+([\s\S]+))?$", re.IGNORECASE)
_CATEGORY_RE = re.compile(r"^[./]cat(?:egory)?guard(?:\s+(reset|status))?$", re.IGNORECASE)
_SEPARATOR_RE = re.compile(r"^[./]separator(?:\s+(on|off|status|scan|check))?$", re.IGNORECASE)

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
    "Order {orderid}\n"
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
    "ss_auto": config.SS_AUTO,
    "separator": config.SEPARATOR,
}
_greeted: set[int] = set()
_automatic_outgoing: dict[int, dict] = {}
_automatic_message_ids: set[tuple[int, int]] = set()
_broadcast_lock = asyncio.Lock()
_payment_lock = asyncio.Lock()

# Payment logger state (loaded in main): post_channel, done/channel templates
# (+ optional *_ref message references), and the payments log.
_pay: dict = {}

# Keep every acquired lock handle open for the process lifetime. There are two:
# one beside pay.json to protect a checkout's session/data, and one in the host
# temp directory keyed by Telegram account to fence stale copies in other clones.
_instance_locks = []


def _account_instance_lock_path(account_id: int):
    """Host-global lock path shared by every checkout for this Telegram user."""
    return (
        Path(tempfile.gettempdir())
        / f"channelguard-quickreply-account-{int(account_id)}.lock"
    )


def _acquire_single_instance_lock(lock_path=None) -> bool:
    """Take a non-blocking process lock and retain it until shutdown.

    The initial checkout-local lock prevents two processes opening the same
    session/data files. After login, a second account-scoped lock in the host
    temp directory prevents another checkout from handling the same Telegram
    account and overwriting command results a moment later.
    """
    lock_path = Path(lock_path or (config.PAY_FILE.parent / "quickreply.lock"))
    lock = ProcessLock(lock_path)
    if not lock.acquire():
        holder = ""
        try:
            with open(lock_path, encoding="utf-8") as existing:
                holder = existing.read().strip()
        except OSError:
            pass
        detail = f" ({holder})" if holder else ""
        ui.error(f"Another quickreply.py owns {lock_path}{detail}.")
        return False

    _instance_locks.append(lock)
    return True


def _write_small_json(path, data) -> None:
    """Atomically persist small state files used by the greeting handlers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _load_greeted() -> set[int]:
    if config.GREETED_FILE.exists():
        try:
            return set(json.loads(config.GREETED_FILE.read_text()))
        except (ValueError, OSError):
            return set()
    return set()


def _save_greeted() -> None:
    _write_small_json(config.GREETED_FILE, sorted(_greeted))


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
    _write_small_json(
        config.GREETING_FILE,
        {"chat_id": int(chat_id), "message_id": int(message_id)},
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
QUICKREPLY_BUILD = "secure-payment-v4"


def _configured_post_channel() -> int | None:
    value = config.payment_channel()
    return value if isinstance(value, int) else None


def _default_pay() -> dict:
    channel = _configured_post_channel()
    return {
        "post_channel": channel,
        # The PAYMENT_CHANNEL value this store was last reconciled with. It is
        # what makes an operator's manual .env edit distinguishable from a .env
        # write that never landed; see _sync_env_post_channel().
        "post_channel_env": channel,
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
    if post_channel is None:
        post_channel = _configured_post_channel()
    data["post_channel"] = post_channel
    if "post_channel_env" in data:
        data["post_channel_env"] = _optional_int(data.get("post_channel_env"))

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
            amount = parse_amount(payment.get("amount"))
            ts = float(payment.get("ts"))
            if not math.isfinite(ts):
                raise ValueError
            # Finite floats can still be outside the platform datetime range.
            datetime.fromtimestamp(ts, _TZ)
        except (TypeError, ValueError, InvalidOperation, OverflowError, OSError):
            repairs.append(f"payment #{index + 1} skipped (invalid amount/time)")
            continue
        payment["amount"] = format(amount, "f")
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
            "post_chat_id",
            "post_message_id",
            "source_chat_id",
            "source_message_id",
            "cancel_command_message_id",
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


def _sync_env_post_channel(data: dict) -> tuple[bool, str]:
    """Reconcile the runtime upload target with the PAYMENT_CHANNEL parameter.

    ``.setchannel`` keeps .env and pay.json in step, so the two normally agree.
    When they disagree the operator edited the parameter by hand (or restored an
    older pay.json), and the declared parameter has to win — otherwise the
    setting reads as applied while every receipt keeps going somewhere else.

    The one case where .env must NOT win is a ``.setchannel`` whose .env write
    failed: ``post_channel_env`` still records the value .env actually holds, so
    a stale parameter cannot quietly move deliveries back.

    Returns ``(changed, note)`` where ``changed`` means the store needs saving.
    """
    env_channel = _configured_post_channel()
    current = _optional_int(data.get("post_channel"))
    known_env = "post_channel_env" in data
    seen_env = _optional_int(data.get("post_channel_env"))

    note = ""
    if env_channel is not None and env_channel != current:
        if current is None:
            data["post_channel"] = env_channel
            note = f"upload channel restored from PAYMENT_CHANNEL ({env_channel})"
        elif not known_env or env_channel != seen_env:
            data["post_channel"] = env_channel
            note = (
                f"upload channel moved to PAYMENT_CHANNEL ({env_channel}); "
                f"pay.json still pointed at {current}"
            )

    changed = bool(note)
    if not known_env or seen_env != env_channel:
        data["post_channel_env"] = env_channel
        changed = True
    return changed, note


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

    # A parameter change is normal operator input, not damage: reconcile it
    # before the repair write so it lands without a bogus recovery backup.
    env_changed, env_note = _sync_env_post_channel(data)

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
    elif env_changed:
        try:
            _write_pay_data(data)
        except OSError as e:
            ui.warn(f"Payment channel parameter could not be persisted: {e}")
    if env_note:
        ui.info(env_note)
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


_CENT = Decimal("0.01")
_MAX_AMOUNT = Decimal("1000000000000000")


def parse_amount(raw: str) -> Decimal:
    """Parse one positive, finite INR amount and round it to paise."""
    cleaned = str(raw).replace(",", "").replace("\u20b9", "").strip()
    cleaned = re.sub(r"\s*INR\s*$", "", cleaned, flags=re.IGNORECASE)
    try:
        value = Decimal(cleaned)
    except InvalidOperation as error:
        raise ValueError("invalid amount") from error
    if not value.is_finite() or value <= 0 or value > _MAX_AMOUNT:
        raise ValueError("amount must be positive and finite")
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _money(value) -> Decimal:
    """Normalize trusted stored/calculated money without accepting NaN/Inf."""
    value = Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)
    if not value.is_finite():
        raise ValueError("non-finite money value")
    return value


def _payment_total(payments) -> Decimal:
    return sum((_money(payment["amount"]) for payment in payments), Decimal("0.00"))


def fmt_inr(value) -> str:
    """Format exact money with the Rupee sign and Indian digit grouping."""
    value = _money(value)
    neg = value < 0
    value = abs(value)
    whole = int(value)
    paise = int((value - Decimal(whole)) * 100)

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

    out = "\u20b9" + grouped
    if paise:
        out += f".{paise:02d}"
    return ("-" if neg else "") + out


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


def _pay_mapping(amount: Decimal, name: str, order_id: str = "",
                 include_current: bool = False, link: str = "") -> dict:
    amount = _money(amount)
    day = _todays_payments()
    today_total = _payment_total(day)
    today_count = len(day)
    if include_current:
        today_total += amount
        today_count += 1

    base = today_total if config.SHARE_BASE == "today" else amount
    rio = base * Decimal(str(config.RIO_PCT)) / Decimal("100")
    marco = base * Decimal(str(config.MARCO_PCT)) / Decimal("100")

    return {
        "{amount}": fmt_inr(amount),
        "{name}": name or "",
        "{orderid}": order_id or "",          # per-payment id, e.g. ANI7F3K9Q
        "{link}": link or "",                 # bot-generated reservation link(s)
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


_TEMPLATE_TOKEN_ALIAS_RE = re.compile(
    r"[\{\uff5b\ufe5b\u2774]"
    r"([^{}\uff5b\uff5d\ufe5b\ufe5c\u2774\u2775]{1,40})"
    r"[\}\uff5d\ufe5c\u2775]",
    re.IGNORECASE,
)
_TEMPLATE_TOKEN_ALIASES = {
    "amount": "{amount}",
    "name": "{name}",
    "orderid": "{orderid}",
    "link": "{link}",
    "rioshare": "{rioshare}",
    "marco": "{marco}",
    "total": "{total}",
    "todaytotal": "{todaytotal}",
}


def _canonicalize_template_tokens(text: str, entities):
    """Normalize visually equivalent payment tokens before substitution.

    Telegram templates are often copied from styled posts and may contain
    full-width braces, capitalization, spaces, underscores, or invisible
    joiners. Keep entity offsets correct while converting those forms to their
    canonical tokens. This covers every supported payment value, not only
    ``{orderid}``.
    """
    aliases = {}
    for match in _TEMPLATE_TOKEN_ALIAS_RE.finditer(text or ""):
        key = unicodedata.normalize("NFKC", match.group(1))
        key = re.sub(r"[\s_\-\u200b-\u200f\u2060\ufeff]+", "", key).casefold()
        canonical = _TEMPLATE_TOKEN_ALIASES.get(key)
        if canonical:
            aliases[match.group(0)] = canonical
    if not aliases:
        return text or "", [copy.copy(entity) for entity in (entities or [])]
    return _substitute(text, entities, aliases)


_LEGACY_PAYMENT_FIELD_PATTERNS = (
    (
        re.compile(
            r"(?im)\bpayment\s+of\s*"
            r"(?P<value>(?:₹\s*|INR\s*)?[+-]?\d[\d,]*(?:\.\d+)?)"
        ),
        "{amount}",
    ),
    (
        re.compile(
            r"(?im)^\s*amount\s*:\s*"
            r"(?P<value>(?:₹\s*|INR\s*)?[+-]?\d[\d,]*(?:\.\d+)?)"
        ),
        "{amount}",
    ),
    (
        re.compile(
            r"(?im)^\s*marco(?:'s)?\s+share\s*:\s*"
            r"(?P<value>(?:₹\s*|INR\s*)?[+-]?\d[\d,]*(?:\.\d+)?)"
        ),
        "{marco}",
    ),
    (
        re.compile(
            r"(?im)^\s*rio(?:'s)?\s+share\s*:\s*"
            r"(?P<value>(?:₹\s*|INR\s*)?[+-]?\d[\d,]*(?:\.\d+)?)"
        ),
        "{rioshare}",
    ),
    (
        re.compile(
            r"(?im)^\s*payment\s+count\s*:\s*#?\s*"
            r"(?P<value>\d+)"
        ),
        "{total}",
    ),
    (
        re.compile(
            r"(?im)^\s*total\s*:\s*"
            r"(?P<value>(?:₹\s*|INR\s*)?[+-]?\d[\d,]*(?:\.\d+)?)"
        ),
        "{todaytotal}",
    ),
    (
        re.compile(
            r"(?im)^\s*order\s+id\s*:\s*"
            r"(?P<value>[A-Za-z][A-Za-z0-9_-]{3,})"
        ),
        "{orderid}",
    ),
)


def _substitute_spans(text: str, entities, replacements: list[tuple]):
    """Replace non-overlapping character spans while preserving entities."""
    text = text or ""
    entities = [copy.copy(entity) for entity in (entities or [])]
    replacements = sorted(replacements, key=lambda item: (item[0], item[1]))
    accepted = []
    cursor = 0
    for start, end, value in replacements:
        if start < cursor or end <= start:
            continue
        accepted.append((start, end, str(value)))
        cursor = end
    if not accepted:
        return text, entities

    parts = []
    cursor = 0
    changes = []
    for start, end, value in accepted:
        parts.extend((text[cursor:start], value))
        start_u16 = _u16(text[:start])
        old_u16 = _u16(text[start:end])
        changes.append(
            (start_u16, start_u16 + old_u16, _u16(value) - old_u16)
        )
        cursor = end
    parts.append(text[cursor:])

    for entity in entities:
        entity_start = entity.offset
        entity_end = entity.offset + entity.length
        offset_delta = length_delta = 0
        for start, end, delta in changes:
            if end <= entity_start:
                offset_delta += delta
            elif start < entity_end:
                length_delta += delta
        entity.offset += offset_delta
        entity.length = max(0, entity.length + length_delta)
    return "".join(parts), entities


def _canonicalize_legacy_payment_fields(text: str, entities):
    """Recover live tokens from templates saved from an old rendered receipt.

    Owners commonly reply to a previous receipt with ``/setdone``. That receipt
    already contains values such as ``₹1`` or ``#25`` instead of tokens. Known
    payment labels make those values unambiguous, so convert them back to live
    placeholders rather than carrying the old values into every future order.
    """
    replacements = []
    for pattern, token in _LEGACY_PAYMENT_FIELD_PATTERNS:
        for match in pattern.finditer(text or ""):
            replacements.append((*match.span("value"), token))
    return _substitute_spans(text, entities, replacements)


def _force_required_token_values(
    text: str, entities, amount: Decimal, order_id: str
):
    """Replace required tokens again at the final delivery boundary.

    This deliberately protects legacy ``pay.json`` templates too: even if they
    were saved by an older release with unusual Unicode braces or invisible
    characters, a receipt can never expose an amount/order placeholder.
    """
    text, entities = _canonicalize_template_tokens(text, entities)
    return _substitute(
        text,
        entities,
        {
            "{amount}": fmt_inr(amount),
            "{orderid}": str(order_id or ""),
        },
    )


def _serialize_entities(entities) -> list[dict]:
    """Store entities as plain dicts so formatting + premium emoji survive even
    if the original template post is later deleted (no re-fetch needed)."""
    out = []
    for e in entities or []:
        d = {k: v for k, v in e.to_dict().items() if k != "_"}
        d["_type"] = type(e).__name__
        out.append(d)
    return out


def _deserialize_entities(dicts) -> list:
    """Rebuild Telethon entity objects from _serialize_entities output."""
    out = []
    for d in dicts or []:
        cls = getattr(tl_types, d.get("_type", ""), None)
        if cls is None:
            continue
        try:
            out.append(cls(**{k: v for k, v in d.items() if k != "_type"}))
        except Exception:  # noqa: BLE001
            continue
    return out


async def _resolve_template(kind: str):
    """Return (text, entities) for kind in {'done', 'channel'}.

    Prefers the entities stored at /set time (verbatim, no re-fetch — this is
    what keeps bold/blockquote/premium emoji even if the source post is gone),
    then a message reference (older templates), then plain text.
    """
    text = _pay.get(f"{kind}_template", "")
    stored = _deserialize_entities(_pay.get(f"{kind}_entities"))
    if stored:
        return text, stored

    ref = _pay.get(f"{kind}_ref")
    if ref:
        try:
            src = await client.get_messages(ref["chat_id"], ids=ref["message_id"])
        except Exception as e:  # noqa: BLE001
            ui.warn(f"{kind} template: couldn't re-fetch the source post "
                    f"({type(e).__name__}); re-run /set{kind if kind!='done' else 'done'} "
                    "so formatting is stored.")
            src = None
        if src is not None:
            ents = list(src.entities or [])
            if not ents:
                ui.warn(f"{kind} template: source post has no formatting/entities.")
            return src.message or text, ents
    if not text:
        return "", []
    ui.warn(f"{kind} template has no stored formatting — reply to your formatted "
            f"post with /set{'done' if kind=='done' else 'channelpostofpayment'} again.")
    return text, []


async def _render(kind: str, amount: Decimal, name: str, order_id: str = "",
                  include_current: bool = False, link: str = ""):
    """Render a payment template using only its explicit bot reservation link.

    Guard/demo links are deliberately never a payment fallback.
    """
    text, ents = await _resolve_template(kind)
    text, ents = _canonicalize_template_tokens(text, ents)
    text, ents = _canonicalize_legacy_payment_fields(text, ents)
    return _substitute(
        text,
        ents,
        _pay_mapping(amount, name, order_id=order_id,
                     include_current=include_current, link=link or ""),
    )


async def _export_invite(entity, title: str = "access") -> str | None:
    """Create an APPROVAL-REQUIRED invite link for a chat this account admins."""
    try:
        res = await client(ExportChatInviteRequest(
            peer=entity, request_needed=True, title=title[:32],
        ))
        return getattr(res, "link", None)
    except Exception as e:  # noqa: BLE001
        ui.warn(f"Couldn't create invite link: {type(e).__name__}: {e}")
        return None


async def _revoke_and_delete_link(group, link: str | None) -> None:
    """Revoke then delete an exported invite link (best-effort)."""
    if not link:
        return
    try:
        await client(EditExportedChatInviteRequest(peer=group, link=link, revoked=True))
    except Exception as e:  # noqa: BLE001
        ui.warn(f"Couldn't revoke link: {type(e).__name__}: {e}")
    try:
        await client(DeleteExportedChatInviteRequest(peer=group, link=link))
    except Exception:  # noqa: BLE001
        pass


async def _reserve_links(
    keywords,
    user_id: int,
    request_key: str,
    *,
    metadata=None,
    timeout: float = 45.0,
) -> dict:
    """Send one idempotent reservation request to the admin bot."""
    empty = {
        "request_id": "",
        "metadata": dict(metadata or {}),
        "entries": [],
        "failures": [],
    }
    if linkstore is None:
        return {
            "request_id": "",
            "metadata": dict(metadata or {}),
            "entries": [],
            "failures": [{"keyword": "bridge", "reason": "bridge unavailable"}],
        }
    try:
        rid = linkstore.request_links(
            keywords,
            int(user_id),
            request_key=request_key,
            metadata=metadata,
        )
        request = linkstore.get_request_details(rid) or {}
        canonical_metadata = dict(request.get("metadata") or metadata or {})
    except Exception as e:  # noqa: BLE001
        return {
            "request_id": "",
            "metadata": dict(metadata or {}),
            "entries": [],
            "failures": [{"keyword": "bridge", "reason": str(e)}],
        }
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = linkstore.get_result_details(rid)
            if result is not None:
                return {
                    "request_id": rid,
                    "metadata": canonical_metadata,
                    "entries": list(result.get("entries") or []),
                    "failures": list(result.get("failures") or []),
                }
        except Exception as e:  # noqa: BLE001
            return {
                "request_id": rid,
                "metadata": canonical_metadata,
                "entries": [],
                "failures": [{"keyword": "bridge", "reason": str(e)}],
            }
        await asyncio.sleep(0.4)
    try:
        cancelled = linkstore.cancel_request(rid)
        if not cancelled:
            result = linkstore.get_result_details(rid)
            if result is not None:
                return {
                    "request_id": rid,
                    "metadata": canonical_metadata,
                    "entries": list(result.get("entries") or []),
                    "failures": list(result.get("failures") or []),
                }
    except Exception:  # noqa: BLE001
        pass
    empty["request_id"] = rid
    empty["metadata"] = canonical_metadata
    empty["failures"].append({
        "keyword": "bridge", "reason": "bot response timed out"
    })
    return empty


def _build_link_block(entries: list):
    """A numbered, bold-marked, blockquoted list of the reserved links.
    Returns (text, entities) with entity offsets relative to the block start
    (UTF-16 units, how Telegram counts them)."""
    lines, ents, cursor = [], [], 0
    total = len(entries)
    for i, e in enumerate(entries, 1):
        link = (e.get("link") if isinstance(e, dict) else str(e)) or ""
        prefix = f"{i}. "
        line = prefix + link
        ents.append(MessageEntityBold(offset=cursor, length=_u16(prefix)))
        cursor += _u16(line)
        if i < total:
            cursor += _u16("\n")
        lines.append(line)
    block = "\n".join(lines)
    ents.insert(0, MessageEntityBlockquote(offset=0, length=_u16(block)))
    return block, ents


async def _render_multi(kind: str, amount: Decimal, name: str, order_id: str,
                        entries: list, include_current: bool = False):
    """Render a template but put the numbered/bold/blockquoted link block where
    {link} is (or append it if the template has no {link})."""
    text, ents = await _resolve_template(kind)
    text, ents = _canonicalize_template_tokens(text, ents)
    text, ents = _canonicalize_legacy_payment_fields(text, ents)
    ents = [copy.copy(e) for e in ents]
    # Substitute every token EXCEPT {link}; we place the styled block by hand.
    mapping = _pay_mapping(amount, name, order_id=order_id,
                           include_current=include_current, link="")
    mapping.pop("{link}", None)
    text, ents = _substitute(text, ents, mapping)

    block, block_ents = _build_link_block(entries)
    token = "{link}"
    positions = []
    if token not in text:
        sep = "" if (not text or text.endswith("\n")) else "\n"
        positions.append(_u16(text) + _u16(sep))
        text = text + sep + block
    else:
        while token in text:
            idx = text.find(token)
            pos = _u16(text[:idx])
            positions.append(pos)
            delta = _u16(block) - _u16(token)
            r_start, r_end = pos, pos + _u16(token)
            for entity in ents:
                e_start = entity.offset
                e_end = entity.offset + entity.length
                if e_end <= r_start:
                    pass
                elif e_start >= r_end:
                    entity.offset += delta
                else:
                    entity.length = max(0, entity.length + delta)
            text = text[:idx] + block + text[idx + len(token):]
    for pos in positions:
        for entity in block_ents:
            clone = copy.copy(entity)
            clone.offset = pos + entity.offset
            ents.append(clone)
    return text, ents


def _failure_detail(failures: list) -> str:
    return "; ".join(
        f"{item.get('keyword', 'group')}: "
        f"{item.get('reason', 'unavailable')}"
        for item in failures
        if isinstance(item, dict)
    )


def _ensure_payment_metadata(
    text: str, amount: Decimal, order_id: str
) -> str:
    """Guarantee that receipts retain the two canonical payment identifiers."""
    text = text or ""
    missing = []
    if order_id and order_id not in text:
        missing.append(f"Order ID: {order_id}")
    rendered_amount = fmt_inr(amount)
    if rendered_amount not in text:
        missing.append(f"Amount: {rendered_amount}")
    if missing:
        separator = "\n\n" if text else ""
        text += separator + "\n".join(missing)
    return text


def _decorate_payment_output(
    text: str,
    entities,
    amount: Decimal,
    order_id: str,
    failures: list,
):
    text = _ensure_payment_metadata(text, amount, order_id)
    detail = _failure_detail(failures)
    if detail:
        text += f"\n\nUnavailable: {detail}"
    return text, list(entities or [])


def _fallback_payment_output(
    kind: str,
    amount: Decimal,
    name: str,
    order_id: str,
    entries: list,
    failures: list,
    include_current: bool,
):
    """Complete plain-text output used when a saved rich template is damaged."""
    base = DEFAULT_DONE if kind == "done" else DEFAULT_CHANNEL
    mapping = _pay_mapping(
        amount,
        name,
        order_id=order_id,
        include_current=include_current,
        link="",
    )
    text, _entities = _substitute(base, [], mapping)
    if entries:
        links = "\n".join(
            f"{index}. {entry.get('link', '')}"
            for index, entry in enumerate(entries, 1)
        )
        text += f"\n\n{links}"
    return _decorate_payment_output(
        text, [], amount, order_id, failures
    )


async def _safe_payment_output(
    kind: str,
    amount: Decimal,
    name: str,
    order_id: str,
    entries: list,
    failures: list,
    include_current: bool = False,
):
    """Render a complete message; damaged custom templates degrade to defaults."""
    try:
        if entries:
            text, entities = await _render_multi(
                kind,
                amount,
                name,
                order_id,
                entries,
                include_current=include_current,
            )
        else:
            text, entities = await _render(
                kind,
                amount,
                name,
                order_id=order_id,
                include_current=include_current,
                link="",
            )
        text, entities = _force_required_token_values(
            text, entities, amount, order_id
        )
        return _decorate_payment_output(
            text, entities, amount, order_id, failures
        )
    except Exception as error:  # noqa: BLE001
        ui.warn(
            f"{kind} template failed; using complete plain-text fallback: "
            f"{type(error).__name__}: {error}"
        )
        return _fallback_payment_output(
            kind,
            amount,
            name,
            order_id,
            entries,
            failures,
            include_current,
        )


def _required_payment_text(
    amount: Decimal,
    name: str,
    order_id: str,
    entries: list,
    failures: list,
    *,
    include_current: bool,
    expose_links: bool = True,
) -> str:
    """Plain complete metadata used when Telegram requires message overflow."""
    mapping = _pay_mapping(
        amount,
        name,
        order_id=order_id,
        include_current=include_current,
    )
    lines = [
        f"Order ID: {order_id}",
        f"Amount: {mapping['{amount}']}",
        f"Name: {name}",
        f"Rio: {mapping['{rioshare}']}",
        f"Marco: {mapping['{marco}']}",
        f"Payments today: {mapping['{total}']}",
        f"Collected today: {mapping['{todaytotal}']}",
    ]
    if entries:
        lines.append("")
        if expose_links:
            lines.append("Links:")
            lines.extend(
                f"{index}. {entry.get('link', '')}"
                for index, entry in enumerate(entries, 1)
            )
        else:
            lines.append(
                f"Delivery: {len(entries)} buyer-bound link(s) sent privately"
            )
            titles = [
                str(entry.get("title") or entry.get("keyword") or "group")
                for entry in entries
            ]
            lines.append("Groups: " + ", ".join(titles))
    detail = _failure_detail(failures)
    if detail:
        lines.extend(("", f"Unavailable: {detail}"))
    return "\n".join(lines)


def _channel_delivery_summary(entries: list) -> str:
    """Describe successful delivery without exposing private invite URLs."""
    if not entries:
        return ""
    titles = [
        str(entry.get("title") or entry.get("keyword") or "group")
        for entry in entries
    ]
    return (
        f"Delivery: {len(entries)} buyer-bound link(s) sent privately\n"
        f"Groups: {', '.join(titles)}"
    )


def _decorate_channel_output(text: str, entries: list) -> str:
    summary = _channel_delivery_summary(entries)
    if not summary:
        return text
    return (text.rstrip() + "\n\n" + summary).strip()


_PRIVATE_INVITE_RE = re.compile(
    r"https?://(?:www\.)?t\.me/(?:joinchat/|\+)[^\s<>()]+",
    re.IGNORECASE,
)


def _sanitize_channel_invites(text: str, entities):
    """Remove visible/entity-only private invites from a channel template."""
    matches = list(dict.fromkeys(_PRIVATE_INVITE_RE.findall(text or "")))
    if matches:
        text, entities = _substitute(
            text,
            entities,
            {match: "[private link delivered in DM]" for match in matches},
        )
    safe_entities = []
    for entity in entities or []:
        target = str(getattr(entity, "url", "") or "")
        if target and _PRIVATE_INVITE_RE.search(target):
            continue
        safe_entities.append(entity)
    return text, safe_entities


def _split_plain_text(
    text: str, *, header: str = "", limit: int = 4000
) -> list[str]:
    """Split at line boundaries and repeat canonical metadata on later chunks."""
    chunks = []
    current = ""
    continued_prefix = (header + "\n") if header else ""
    available = max(1, limit - _u16(continued_prefix))

    def split_units(value: str, max_units: int) -> list[str]:
        pieces = []
        piece = ""
        piece_units = 0
        for char in value:
            char_units = 2 if ord(char) > 0xFFFF else 1
            if piece_units + char_units > max_units:
                if piece:
                    pieces.append(piece)
                piece = char
                piece_units = char_units
            else:
                piece += char
                piece_units += char_units
        if piece or not pieces:
            pieces.append(piece)
        return pieces

    for line in (text or "").splitlines() or [""]:
        candidate = line if not current else current + "\n" + line
        if _u16(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        pieces = split_units(line, available)
        for piece in pieces[:-1]:
            chunks.append(continued_prefix + piece)
        current = continued_prefix + pieces[-1]
    if current:
        chunks.append(current)
    return chunks or [header or ""]


async def _deliver_payment_receipt(
    event,
    text: str,
    entities,
    amount: Decimal,
    name: str,
    order_id: str,
    entries: list,
    failures: list,
) -> bool:
    if _u16(text) <= 4096:
        try:
            dropped = await _edit_rich(event, text, entities)
            if dropped:
                ui.warn(
                    f"Done message: dropped {dropped} unsupported entity(ies)."
                )
            return True
        except Exception as error:  # noqa: BLE001
            ui.warn(
                f"Rich receipt edit failed; sending plain text: "
                f"{type(error).__name__}: {error}"
            )
            return await _ack(event, text)

    fallback = _required_payment_text(
        amount,
        name,
        order_id,
        entries,
        failures,
        include_current=True,
    )
    chunks = _split_plain_text(
        fallback, header=f"Order ID: {order_id}"
    )
    if not await _ack(event, chunks[0]):
        return False
    for chunk in chunks[1:]:
        try:
            await event.respond(chunk)
        except Exception as error:  # noqa: BLE001
            ui.warn(f"Receipt overflow send failed: {type(error).__name__}: {error}")
            return False
    return True


async def _send_payment_overflow(
    target_channel: int,
    amount: Decimal,
    name: str,
    order_id: str,
    entries: list,
    failures: list,
) -> str:
    """Send complete required caption data; return an error string on failure."""
    overflow = _required_payment_text(
        amount,
        name,
        order_id,
        entries,
        failures,
        include_current=False,
        expose_links=False,
    )
    overflow = _PRIVATE_INVITE_RE.sub(
        "[private link delivered in DM]", overflow
    )
    chunks = _split_plain_text(overflow, limit=3800)
    marker = None
    try:
        for index, chunk in enumerate(chunks, 1):
            label = (
                f"Payment details {index}/{len(chunks)} — Order {order_id}"
            )
            if await _find_existing_channel_text(target_channel, label):
                continue
            payload = f"{label}\n{chunk}"
            marker = _expect_automatic_outgoing(target_channel, [payload])
            sent = await client.send_message(
                target_channel, payload, link_preview=False
            )
            _finish_automatic_outgoing(target_channel, marker, sent)
            marker = None
    except Exception as error:  # noqa: BLE001
        if marker is not None:
            _cancel_automatic_outgoing(target_channel, marker)
        return f"{type(error).__name__}: {error}"
    return ""


async def _find_existing_channel_text(
    target_channel: int, marker: str
) -> int | None:
    iterator = getattr(client, "iter_messages", None)
    if iterator is None:
        return None
    try:
        async for message in iterator(
            target_channel, search=marker, limit=50
        ):
            text = str(
                getattr(message, "message", "")
                or getattr(message, "raw_text", "")
                or ""
            )
            if marker in text and getattr(message, "id", None):
                return int(message.id)
    except Exception as error:  # noqa: BLE001
        ui.warn(
            "Payment overflow reconciliation failed: "
            f"{type(error).__name__}: {error}"
        )
    return None


async def _find_existing_channel_post(
    target_channel: int, order_id: str
) -> int | None:
    """Reconcile an ambiguous upload by locating its order-bearing media post."""
    iterator = getattr(client, "iter_messages", None)
    if iterator is None:
        return None
    try:
        async for message in iterator(
            target_channel, search=order_id, limit=50
        ):
            text = str(
                getattr(message, "message", "")
                or getattr(message, "raw_text", "")
                or ""
            )
            if (
                order_id.casefold() in text.casefold()
                and getattr(message, "media", None) is not None
                and getattr(message, "id", None)
            ):
                return int(message.id)
    except Exception as error:  # noqa: BLE001
        ui.warn(
            "Payment-channel reconciliation failed: "
            f"{type(error).__name__}: {error}"
        )
    return None


def _post_retry_delay(attempts: int) -> float:
    return float(min(300, max(5, 5 * (2 ** min(max(attempts - 1, 0), 6)))))


async def _post_payment_to_channel(
    payment: dict,
    *,
    source_message=None,
    notify_failure: bool = True,
) -> bool:
    """Post or reconcile one committed payment without leaking invite links."""
    if payment.get("status") != "valid":
        return False
    if payment.get("post_status") == "posted" and payment.get("post_message_id"):
        return True

    # The live setting wins over the snapshot taken when the payment was
    # recorded: a receipt queued against an older channel (or against none at
    # all) has to follow `.setchannel`, not the channel that was configured
    # minutes ago. The snapshot is kept only to reconcile an in-flight upload.
    snapshot = _optional_int(payment.get("post_chat_id"))
    configured = _optional_int(_pay.get("post_channel"))
    target = configured if configured is not None else snapshot
    if target is None:
        payment["post_status"] = "not_configured"
        payment["post_error"] = "PAYMENT_CHANNEL is not configured"
        payment["next_post_retry"] = _now_ts() + 60
        _save_pay()
        if notify_failure and not payment.get("post_failure_notified"):
            payment["post_failure_notified"] = True
            _save_pay()
            await _notify_owner(
                f"Payment {payment.get('order_id', '')} was recorded, but "
                "PAYMENT_CHANNEL is not set. Run .setchannel in the target "
                "channel; delivery will retry automatically."
            )
        return False

    order_id = str(payment.get("order_id") or "")

    # A send may have reached Telegram just before the process lost its reply.
    # Search first so retries converge on the accepted media post. Both the old
    # and the new chat are checked, so moving the channel mid-flight cannot
    # duplicate a receipt that already landed in the previous one.
    if payment.get("post_status") in {"posting", "untracked", "failed"}:
        candidates = [chat for chat in (snapshot, target) if chat is not None]
        for candidate in dict.fromkeys(candidates):
            existing_id = await _find_existing_channel_post(candidate, order_id)
            if existing_id:
                payment["post_chat_id"] = int(candidate)
                payment["post_message_id"] = existing_id
                payment["post_status"] = "posted"
                payment.pop("post_error", None)
                payment.pop("next_post_retry", None)
                _save_pay()
                return True

    payment["post_chat_id"] = int(target)

    if source_message is None:
        source_chat_id = _optional_int(payment.get("source_chat_id"))
        source_message_id = _optional_int(payment.get("source_message_id"))
        if source_chat_id is not None and source_message_id is not None:
            try:
                source_message = await client.get_messages(
                    source_chat_id, ids=source_message_id
                )
            except Exception as error:  # noqa: BLE001
                payment["post_error"] = (
                    f"source fetch failed: {type(error).__name__}: {error}"
                )

    if source_message is None or not getattr(source_message, "media", None):
        attempts = int(payment.get("post_attempts") or 0)
        payment["post_attempts"] = attempts + 1
        payment["post_status"] = "failed"
        payment.setdefault("post_error", "source payment image is unavailable")
        payment["next_post_retry"] = (
            _now_ts() + _post_retry_delay(payment["post_attempts"])
        )
        _save_pay()
        if notify_failure and not payment.get("post_failure_notified"):
            payment["post_failure_notified"] = True
            _save_pay()
            await _notify_owner(
                f"Payment-channel delivery failed for {order_id}: "
                f"{payment['post_error']}. It remains queued for retry."
            )
        return False

    amount = parse_amount(payment["amount"])
    name = str(payment.get("name") or "")
    entries = [
        entry
        for entry in (payment.get("reservation_entries") or [])
        if isinstance(entry, dict)
    ]
    failures = [
        failure
        for failure in (payment.get("reservation_failures") or [])
        if isinstance(failure, dict)
    ]
    text, entities = await _safe_payment_output(
        "channel",
        amount,
        name,
        order_id,
        [],
        failures,
        include_current=False,
    )
    text = _decorate_channel_output(text, entries)
    text, entities = _sanitize_channel_invites(text, entities)
    text, entities, truncated = _fit_media_caption(
        text, entities, preserve=(order_id, fmt_inr(amount))
    )

    payment["channel_caption_truncated"] = bool(truncated)
    payment["post_status"] = "posting"
    payment["post_attempts"] = int(payment.get("post_attempts") or 0) + 1
    payment.pop("next_post_retry", None)
    # Persist the intent before the network call. If this write fails, do not
    # upload an untracked post.
    _save_pay()

    marker = _expect_automatic_outgoing(target, [text])
    post_result = None
    last_error = None
    for attempt in _entity_fallbacks(entities):
        try:
            post_result = await client.send_file(
                target,
                file=source_message.media,
                caption=text,
                formatting_entities=attempt or None,
            )
            break
        except Exception as error:  # noqa: BLE001
            last_error = error
            if not _is_entity_error(error):
                break

    if post_result is None:
        _cancel_automatic_outgoing(target, marker)
        existing_id = await _find_existing_channel_post(target, order_id)
        if existing_id:
            payment["post_message_id"] = existing_id
            payment["post_status"] = "posted"
            payment.pop("post_error", None)
            payment.pop("next_post_retry", None)
            _save_pay()
            return True
        payment["post_status"] = "failed"
        payment["post_error"] = f"{type(last_error).__name__}: {last_error}"
        payment["next_post_retry"] = (
            _now_ts() + _post_retry_delay(payment["post_attempts"])
        )
        _save_pay()
        ui.warn(f"Payment channel post failed: {payment['post_error']}")
        if notify_failure and not payment.get("post_failure_notified"):
            payment["post_failure_notified"] = True
            _save_pay()
            await _notify_owner(
                f"Payment-channel delivery failed for {order_id}: "
                f"{payment['post_error']}. It remains queued for retry."
            )
        return False

    _finish_automatic_outgoing(target, marker, post_result)
    post_ids = sorted(_sent_message_ids(post_result))
    payment["post_message_id"] = post_ids[0] if post_ids else None
    payment["post_status"] = "posted" if post_ids else "untracked"
    payment.pop("post_failure_notified", None)
    if post_ids:
        payment.pop("post_error", None)
        payment.pop("next_post_retry", None)
    else:
        payment["post_error"] = "Telegram returned no posted message ID"
        payment["next_post_retry"] = (
            _now_ts() + _post_retry_delay(payment["post_attempts"])
        )
    _save_pay()

    if truncated and payment.get("overflow_status") != "posted":
        payment["overflow_status"] = "posting"
        _save_pay()
        overflow_error = await _send_payment_overflow(
            target, amount, name, order_id, entries, failures
        )
        payment["overflow_status"] = "failed" if overflow_error else "posted"
        if overflow_error:
            payment["overflow_error"] = overflow_error
            ui.warn("Payment metadata overflow post failed: " + overflow_error)
        else:
            payment.pop("overflow_error", None)
        _save_pay()
    return bool(post_ids)


def _entity_fallbacks(ents):
    """Full entities, then custom-emoji-free entities, then plain text."""
    ents = list(ents or [])
    tries = [ents]
    no_custom = [e for e in ents if type(e).__name__ != "MessageEntityCustomEmoji"]
    if len(no_custom) != len(ents):
        tries.append(no_custom)
    if ents:
        tries.append([])
    return tries


def _fit_media_caption(text: str, entities, preserve="", limit: int = 1024):
    """Fit a rendered caption to Telegram's UTF-16 limit.

    If required metadata would otherwise be cut from an oversized caption, keep
    it at the front. Formatting entities are shifted/clipped to remain valid,
    so a long template cannot make the channel upload fail.
    """
    text = text or ""
    entities = [copy.copy(entity) for entity in (entities or [])]
    if _u16(text) <= limit:
        return text, entities, False

    def prefix_length(max_units: int) -> int:
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if _u16(text[:middle]) <= max_units:
                low = middle
            else:
                high = middle - 1
        return low

    if isinstance(preserve, str):
        required = [preserve] if preserve else []
    else:
        required = [str(value) for value in (preserve or []) if value]

    prefix = ""
    keep = prefix_length(limit)
    positions = [(value, text.find(value)) for value in required]
    clipped_required = [
        (value, position)
        for value, position in positions
        if position < 0 or position + len(value) > keep
    ]
    if clipped_required:
        prefix = " | ".join(required) + "\n"
        keep = prefix_length(max(0, limit - _u16(prefix)))
        # Do not leave a partial duplicate of required metadata at the end.
        duplicate_positions = [
            position for _value, position in positions
            if 0 <= position < keep
        ]
        if duplicate_positions:
            keep = min(duplicate_positions)

    clipped = prefix + text[:keep]
    final_length = _u16(clipped)
    shift = _u16(prefix)
    kept_entities = []
    original_limit = final_length - shift
    for entity in entities:
        if entity.offset >= original_limit:
            continue
        entity.length = min(entity.length, original_limit - entity.offset)
        if entity.length <= 0:
            continue
        entity.offset += shift
        kept_entities.append(entity)
    return clipped, kept_entities, True


def _is_entity_error(error: Exception) -> bool:
    """Only formatting/entity rejections are safe to retry with fewer entities."""
    detail = f"{type(error).__name__}: {error}".upper()
    markers = (
        "ENTITY", "CUSTOM_EMOJI", "CUSTOM EMOJI", "EMOTICON",
        "PREMIUM_ACCOUNT_REQUIRED", "PREMIUM ACCOUNT REQUIRED",
    )
    return any(marker in detail for marker in markers)


async def _edit_rich(event, text, ents):
    """Edit once, degrading entities only for a Telegram entity rejection."""
    original = list(ents or [])
    last_error = None
    for attempt in _entity_fallbacks(original):
        try:
            await event.edit(text, formatting_entities=attempt or None)
            return len(original) - len(attempt)
        except MessageNotModifiedError:
            return len(original) - len(attempt)
        except Exception as e:  # noqa: BLE001
            last_error = e
            if not _is_entity_error(e):
                raise
    if last_error is not None:
        raise last_error
    return 0


async def _cancel_reserved_links(request_id: str) -> bool:
    """Ask the admin bot to revoke every link minted for a failed payment."""
    if not request_id:
        return True
    if linkstore is None:
        return False
    try:
        changed = linkstore.cancel_request(request_id, force=True)
        return bool(changed or linkstore.is_request_cancelled(request_id))
    except Exception as error:  # noqa: BLE001
        ui.warn(
            f"Reservation cleanup could not be queued: "
            f"{type(error).__name__}: {error}"
        )
        return False


async def _fail_payment(event, payment: dict, request_id: str, message: str) -> None:
    payment["status"] = "failed"
    payment["error"] = message[:500]
    save_error = None
    try:
        _save_pay()
    except Exception as error:  # noqa: BLE001
        save_error = error
    cleanup = await _cancel_reserved_links(request_id)
    payment["reservation_cleanup"] = "queued" if cleanup else "retry_needed"
    try:
        _save_pay()
    except Exception as error:  # noqa: BLE001
        save_error = save_error or error
    if save_error is not None:
        message += (
            "\nPayment state could not be saved; access cleanup was still "
            f"attempted ({type(save_error).__name__})."
        )
    await _ack(event, message)


async def _recover_pending_payments(
    *, recover_interrupted_adds: bool = True
) -> None:
    """Recover interrupted adds and retry durable cleanup/channel delivery."""
    changed = False
    for payment in _pay.get("payments", []):
        if recover_interrupted_adds and payment.get("status") == "pending":
            payment["status"] = "failed"
            payment["error"] = "interrupted before receipt completion"
            await _cancel_reserved_links(
                payment.get("reservation_request_id", "")
            )
            changed = True
        elif payment.get("status") in {"cancel_pending", "fake"}:
            queued = await _cancel_reserved_links(
                payment.get("reservation_request_id", "")
            )
            state = "queued" if queued else "retry_needed"
            if payment.get("reservation_cleanup") != state:
                payment["reservation_cleanup"] = state
                changed = True
        if (
            payment.get("status") == "valid"
            and payment.get("source_message_id")
            and payment.get("post_status")
            in {
                "queued",
                "posting",
                "failed",
                "untracked",
                "not_configured",
                None,
            }
            and float(payment.get("next_post_retry") or 0) <= _now_ts()
        ):
            try:
                await _post_payment_to_channel(
                    payment, notify_failure=False
                )
            except Exception as error:  # noqa: BLE001
                payment["post_status"] = "failed"
                payment["post_error"] = f"{type(error).__name__}: {error}"
                payment["next_post_retry"] = _now_ts() + 30
                changed = True
        if (
            payment.get("overflow_status") in {"posting", "failed"}
            and payment.get("post_chat_id")
            and payment.get("order_id")
        ):
            try:
                amount = parse_amount(payment["amount"])
                overflow_error = await _send_payment_overflow(
                    int(payment["post_chat_id"]),
                    amount,
                    str(payment.get("name") or ""),
                    str(payment["order_id"]),
                    [
                        entry
                        for entry in (
                            payment.get("reservation_entries") or []
                        )
                        if isinstance(entry, dict)
                    ],
                    [
                        failure
                        for failure in (
                            payment.get("reservation_failures") or []
                        )
                        if isinstance(failure, dict)
                    ],
                )
            except Exception as error:  # noqa: BLE001
                overflow_error = f"{type(error).__name__}: {error}"
            payment["overflow_status"] = (
                "failed" if overflow_error else "posted"
            )
            if overflow_error:
                payment["overflow_error"] = overflow_error
            else:
                payment.pop("overflow_error", None)
            changed = True
    if changed:
        _save_pay()
        ui.warn(
            "Recovered interrupted payment/cancellation/channel-delivery state."
        )


async def _payment_recovery_loop() -> None:
    while True:
        try:
            async with _payment_lock:
                await _recover_pending_payments(
                    recover_interrupted_adds=False
                )
        except Exception as error:  # noqa: BLE001
            ui.warn(
                "Payment recovery loop failed: "
                f"{type(error).__name__}: {error}"
            )
        await asyncio.sleep(20)


async def cmd_add(event) -> None:
    """/add <amount> <name> <keyword...> — run in the payer's DM (or reply to them).

    Reserves a link for EACH keyword via the BOT, bound to the payer. Multiple
    keywords -> multiple groups (e.g. /add 252 aka cp op lp fp); 'all' -> every
    group the bot admins. The reserved links are listed numbered/bold/blockquoted
    where {link} sits in the done message. When the payer joins any of them, the
    bot approves only them and revokes that link."""
    m = re.match(r"^/add(?:\s+(\S+)(?:\s+(\S+)(?:\s+([\s\S]+))?)?)?$", event.raw_text or "")
    amount_raw = m.group(1) if m else None
    accname = (m.group(2).strip() if m and m.group(2) else "")
    keywords = (m.group(3).split() if m and m.group(3) else [])

    if not amount_raw:
        await _ack(event, "Usage: /add <amount> <name> <keyword...>  (in the payer's DM)")
        return
    try:
        amount = parse_amount(amount_raw)
    except ValueError:
        await _ack(event, f"Amount '{amount_raw}' is not a positive number.")
        return

    command_key = _command_identity(event)
    existing_payment = next(
        (
            payment for payment in reversed(_pay.get("payments", []))
            if payment.get("command_key") == command_key
        ),
        None,
    )
    if existing_payment:
        await _ack(
            event,
            f"Payment already recorded — {existing_payment.get('order_id', '')} "
            f"({existing_payment.get('status', 'valid')})",
        )
        return
    order_id = _generate_order_id()

    reply = await event.get_reply_message()
    has_media = bool(reply and reply.media)

    # 1) Send one batched request to the BOT. The userbot never searches groups
    #    or reuses the guard's /demo link for a payment.
    entries = []
    failures = []
    payer_id = None
    request_id = ""
    if keywords:
        if reply and getattr(reply, "sender_id", None) and reply.sender_id != _state["self_id"]:
            payer_id = reply.sender_id
        elif event.is_private:
            payer_id = event.chat_id
        if not payer_id:
            await _ack(event, "Can't tell who paid - run /add in the customer's DM "
                              "or reply to their message.")
            return
        if linkstore is None:
            await _ack(event, "Bot bridge unavailable.")
            return

        print(
            ui.green("[reserve] ")
            + f"asking bot: {' '.join(keywords)} -> {payer_id}",
            flush=True,
        )
        result = await _reserve_links(
            keywords,
            int(payer_id),
            request_key=command_key,
            metadata={
                "order_id": order_id,
                "amount": format(amount, "f"),
                "account_name": accname,
                "keyword": " ".join(keywords),
            },
        )
        request_id = result.get("request_id", "")
        order_id = (
            str((result.get("metadata") or {}).get("order_id") or order_id)
        )
        failures = result["failures"]
        seen_links = set()
        for entry in result["entries"]:
            link = entry.get("link") if isinstance(entry, dict) else None
            if link and link not in seen_links:
                seen_links.add(link)
                entries.append(entry)
        if not entries:
            detail = "; ".join(
                f"{item.get('keyword')}: {item.get('reason')}"
                for item in failures
            ) or "no links returned"
            await _cancel_reserved_links(request_id)
            await _ack(event, f"No group links: {detail}")
            return
        print(ui.green("[reserve] ") + f"got {len(entries)} link(s)", flush=True)

    if not keywords and not has_media:
        await _ack(event, "Give keyword(s) to send links: /add <amount> <name> <kw...>, "
                          "or reply to an image to just log a payment.")
        return

    us_text, us_ents = await _safe_payment_output(
        "done",
        amount,
        accname,
        order_id,
        entries,
        failures,
        include_current=True,
    )

    payment = {
        "amount": format(amount, "f"),
        "name": accname,
        "order_id": order_id,
        "command_key": command_key,
        "reservation_request_id": request_id,
        "ts": _now_ts(),
        "status": "pending",
    }
    if has_media:
        payment["source_chat_id"] = int(
            getattr(reply, "chat_id", None) or event.chat_id
        )
        source_id = getattr(reply, "id", None)
        if source_id is not None:
            payment["source_message_id"] = int(source_id)
        payment["post_chat_id"] = _optional_int(_pay.get("post_channel"))
        payment["post_status"] = (
            "queued" if payment["post_chat_id"] is not None else "not_configured"
        )
    if entries:
        payment.update({
            "invite_links": [e["link"] for e in entries],
            "payer_id": payer_id, "keywords": keywords,
            "reservation_entries": [
                {
                    "link": e.get("link", ""),
                    "title": e.get("title", ""),
                    "keyword": e.get("keyword", ""),
                }
                for e in entries
            ],
        })
        if failures:
            payment["reservation_failures"] = failures
    _pay.setdefault("payments", []).append(payment)
    try:
        _save_pay()
    except Exception as error:  # noqa: BLE001
        _pay["payments"].remove(payment)
        await _cancel_reserved_links(request_id)
        await _ack(
            event,
            "Payment was not recorded because its state could not be saved. "
            "Reserved links were cancelled. "
            f"({type(error).__name__}: {error})",
        )
        return

    # 2) Deliver the private receipt first. This is the authoritative commit:
    # startup recovery only compensates records that never reached this point.
    receipt_sent = await _deliver_payment_receipt(
        event,
        us_text,
        us_ents,
        amount,
        accname,
        order_id,
        entries,
        failures,
    )
    if not receipt_sent:
        await _fail_payment(
            event, payment, request_id, "Could not deliver the payment receipt."
        )
        return

    payment["status"] = "valid"
    payment.pop("error", None)
    try:
        _save_pay()
    except Exception as error:  # noqa: BLE001
        await _fail_payment(
            event,
            payment,
            request_id,
            "The receipt was delivered, but durable payment commit failed. "
            f"Reserved access was cancelled ({type(error).__name__}: {error}).",
        )
        return

    # 3) Channel posting is secondary. The valid receipt is persisted first, so
    # a crash after Telegram accepts this upload never invalidates its links.
    if has_media:
        try:
            await _post_payment_to_channel(payment, source_message=reply)
        except Exception as error:  # noqa: BLE001
            ui.warn(
                "Payment was committed, but channel delivery state failed: "
                f"{type(error).__name__}: {error}"
            )
            await _notify_owner(
                f"Payment {order_id} is committed, but payment-channel "
                f"delivery needs attention: {type(error).__name__}: {error}"
            )

    count = len(_todays_payments())
    print(ui.green("[pay] ")
          + f"{order_id} {fmt_inr(amount)} {accname} (#{count} today)", flush=True)


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
        await _ack(event, "Reply to a payment post with /cancel.")
        return

    payment = _payment_for_post(event.chat_id, reply.id)
    if payment is None:
        await _ack(
            event,
            "That post is not linked to a recorded payment in this channel. "
            "Only payment posts created after /cancel support can be matched."
        )
        return
    if payment.get("status") == "fake":
        cleanup = await _cancel_reserved_links(
            payment.get("reservation_request_id", "")
        )
        payment["reservation_cleanup"] = (
            "queued" if cleanup else "retry_needed"
        )
        save_error = ""
        try:
            _save_pay()
        except Exception as error:  # noqa: BLE001
            save_error = f" State save failed: {type(error).__name__}: {error}."
        await _ack(
            event,
            "Payment is already marked fake. "
            + (
                "Access-link cleanup is queued."
                if cleanup
                else "Access-link cleanup still needs a retry."
            )
            + save_error,
        )
        return

    fake_text, fake_entities = _fake_caption(reply)
    if payment.get("status") != "cancel_pending":
        payment["cancel_previous_status"] = payment.get("status", "valid")
    payment["status"] = "cancel_pending"
    payment["cancel_command_message_id"] = int(event.id)
    payment["reservation_cleanup"] = "pending"
    transition_save_error = None
    try:
        _save_pay()
    except Exception as error:  # noqa: BLE001
        transition_save_error = error

    # Revoke buyer-bound access as soon as /cancel is accepted. Caption edits
    # can be retried, but a fake payment must never retain a usable invite.
    cleanup = await _cancel_reserved_links(
        payment.get("reservation_request_id", "")
    )
    payment["reservation_cleanup"] = (
        "queued" if cleanup else "retry_needed"
    )
    try:
        _save_pay()
        transition_save_error = None
    except Exception as error:  # noqa: BLE001
        transition_save_error = transition_save_error or error

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
            # The access cancellation has already been committed. Keep the
            # payment excluded and make the visual marker explicitly retryable.
            payment["cancel_error"] = f"{type(e).__name__}: {e}"
            try:
                _save_pay()
            except Exception as save_error:  # noqa: BLE001
                ui.warn(
                    "Cancellation error state could not be saved: "
                    f"{type(save_error).__name__}: {save_error}"
                )
            await _ack(
                event,
                "Access cancellation is queued, but the channel marker failed: "
                f"{type(e).__name__}: {e}. Reply /cancel again to retry it.",
            )
            return
        else:
            # Telegram may have applied the edit before the response was lost.
            # Keep this record excluded from stats until /cancel is retried and
            # the post can be reconciled; restoring it could count a visible fake.
            payment["cancel_error"] = f"{type(e).__name__}: {e}"
            try:
                _save_pay()
            except Exception as save_error:  # noqa: BLE001
                ui.warn(
                    "Unverified cancellation state could not be saved: "
                    f"{type(save_error).__name__}: {save_error}"
                )
            await _ack(
                event,
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
        try:
            _save_pay()
            transition_save_error = None
        except Exception as error:  # noqa: BLE001
            transition_save_error = transition_save_error or error

    day = _todays_payments()
    total = _payment_total(day)
    if transition_save_error is not None:
        await _ack(
            event,
            "Access cancellation/marker was attempted, but payment state "
            f"could not be saved: {type(transition_save_error).__name__}: "
            f"{transition_save_error}. Do not restart until storage is fixed "
            "and /cancel is retried."
        )
    else:
        try:
            await event.delete()
        except Exception:  # noqa: BLE001
            await _ack(
                event,
                f"Payment marked fake. Today: {fmt_inr(total)} / "
                f"{len(day)} payment(s)."
            )
    print(
        ui.yellow("[pay fake] ")
        + f"{fmt_inr(payment['amount'])} {payment.get('name', '')} -> "
        + f"today {fmt_inr(total)} / {len(day)} payment(s)",
        flush=True,
    )


async def _ack(event, text: str, **kwargs) -> bool:
    """Edit the command or send one fallback response; report delivery."""
    try:
        await event.edit(text, **kwargs)
        return True
    except MessageNotModifiedError:
        return True
    except Exception:  # noqa: BLE001
        try:
            await event.respond(text, **kwargs)
            return True
        except Exception:  # noqa: BLE001
            return False


async def _set_template(event, kind: str, cmd: str, label: str) -> None:
    """Store a template. Replying to a post keeps its formatting + premium emoji
    (stored as a message reference); inline text is stored as plain text."""
    reply = await event.get_reply_message()
    if reply is not None and (reply.raw_text or ""):
        _pay[f"{kind}_ref"] = {"chat_id": int(reply.chat_id), "message_id": int(reply.id)}
        template, entities = _canonicalize_template_tokens(
            reply.raw_text, reply.entities
        )
        template, entities = _canonicalize_legacy_payment_fields(
            template, entities
        )
        _pay[f"{kind}_template"] = template
        # Store the formatting verbatim so it survives even if the post is gone.
        _pay[f"{kind}_entities"] = _serialize_entities(entities)
        _save_pay()
        n = len(_pay[f"{kind}_entities"])
        await _ack(event, f"{label} saved from that post - {n} formatting/emoji "
                          "entity(ies) stored.")
        return

    m = re.match(rf"^{re.escape(cmd)}(?:\s+([\s\S]+))?$", event.raw_text or "")
    template = (m.group(1).strip() if m and m.group(1) else "")
    if not template:
        await _ack(event, f"Send the text after {cmd}, or reply to a formatted post with {cmd}.")
        return
    template, _ = _canonicalize_template_tokens(template, [])
    template, _ = _canonicalize_legacy_payment_fields(template, [])
    _pay[f"{kind}_template"] = template
    _pay.pop(f"{kind}_ref", None)
    _pay.pop(f"{kind}_entities", None)
    _save_pay()
    await _ack(event, f"{label} saved ({len(template)} chars, plain text).")


async def cmd_setdone(event) -> None:
    """Set the message the user gets in the private chat after /add."""
    await _set_template(event, "done", "/setdone", "Private-chat message")


async def cmd_setchannelpost(event) -> None:
    """Set the caption used for the channel post after /add."""
    await _set_template(event, "channel", "/setchannelpostofpayment", "Channel post caption")


def _pending_channel_payments() -> list:
    """Committed payments whose proof image has not reached the channel yet."""
    return [
        payment
        for payment in _pay.get("payments", [])
        if payment.get("status") == "valid"
        and payment.get("source_message_id")
        and payment.get("post_status") != "posted"
    ]


def _runtime_status_text() -> str:
    """`.ping`: the build plus the target deliveries actually use."""
    target = _optional_int(_pay.get("post_channel"))
    parts = [
        f"Quick-reply userbot is running ({QUICKREPLY_BUILD}).",
        f"Payment channel: {target if target is not None else 'NOT SET'}.",
    ]
    declared = _configured_post_channel()
    if declared != target:
        # Only reachable when a `.setchannel` could not write .env, and the two
        # would diverge again after a restart. Surface it instead of hiding it.
        parts.append(
            "PAYMENT_CHANNEL in .env: "
            f"{declared if declared is not None else 'NOT SET'}."
        )
    pending = len(_pending_channel_payments())
    if pending:
        parts.append(f"Pending deliveries: {pending}.")
    return " ".join(parts)


async def _resolve_post_target(argument: str) -> tuple[int, str]:
    """Resolve a `.setchannel <target>` argument to ``(chat_id, title)``."""
    value = argument.split()[0] if argument.split() else ""
    private = re.match(r"^(?:https?://)?t\.me/c/(\d+)(?:/\d+)?/?$", value, re.I)
    public = re.match(r"^(?:https?://)?t\.me/([A-Za-z][\w]{3,})/?$", value, re.I)
    if private:
        value = f"-100{private.group(1)}"
    elif public:
        value = f"@{public.group(1)}"
    entity = await client.get_entity(config.coerce(value))
    if isinstance(entity, tl_types.User):
        raise ValueError(f"{value} is a user, not a channel or group.")
    try:
        chat_id = get_peer_id(entity)
    except (TypeError, ValueError):
        chat_id = _optional_int(value)
    if chat_id is None:
        raise ValueError(f"Couldn't determine the chat id for {value}.")
    return chat_id, getattr(entity, "title", None) or value


async def cmd_setchannel(event) -> None:
    """Set the payment upload channel: here, or by id/@username/t.me link."""
    match = _SETCHANNEL_RE.match((getattr(event, "raw_text", "") or "").strip())
    argument = (match.group(1).strip() if match and match.group(1) else "")

    if argument.casefold() in ("", "here", "this"):
        if not getattr(event, "is_channel", False):
            await _ack(
                event,
                "Run .setchannel inside the target channel, or name it: "
                ".setchannel <id|@username>",
            )
            return
        chat_id = event.chat_id
        chat = await event.get_chat()
        title = getattr(chat, "title", None) or "this chat"
        where = "here: "
    else:
        try:
            chat_id, title = await _resolve_post_target(argument)
        except ValueError as error:
            await _ack(event, str(error))
            return
        except Exception as error:  # noqa: BLE001
            await _ack(
                event,
                f"Couldn't resolve {argument}: {type(error).__name__}: {error}",
            )
            return
        where = ""

    # Captured before save_env(), which refreshes the live value: on a failed
    # .env write this is what .env still holds, and remembering it keeps the
    # next start from "adopting" that stale parameter over this new target.
    previous_env = _configured_post_channel()
    _pay["post_channel"] = int(chat_id)
    _pay["post_channel_env"] = int(chat_id)
    persisted = True
    try:
        config.save_env({"PAYMENT_CHANNEL": str(chat_id)})
    except OSError as error:
        persisted = False
        _pay["post_channel_env"] = previous_env
        ui.warn(f"Couldn't persist PAYMENT_CHANNEL in .env: {error}")

    # Every receipt that never reached a channel follows the new target, not
    # just the ones recorded while no channel was configured at all.
    requeued = 0
    for payment in _pending_channel_payments():
        payment["next_post_retry"] = 0
        payment.pop("post_failure_notified", None)
        if payment.get("post_status") == "not_configured":
            payment["post_status"] = "queued"
            payment.pop("post_error", None)
        requeued += 1
    _save_pay()

    suffix = "" if persisted else " (saved for this data folder only)"
    message = f"Post channel set {where}{title} ({chat_id}){suffix}"
    if requeued:
        message += f"\nRe-queued {requeued} pending delivery(s)."
    await _ack(event, message)
    await _recover_pending_payments(recover_interrupted_adds=False)


async def cmd_stats(event) -> None:
    day = _todays_payments()
    total = _payment_total(day)
    rio = total * Decimal(str(config.RIO_PCT)) / Decimal("100")
    marco = total * Decimal(str(config.MARCO_PCT)) / Decimal("100")
    await _ack(
        event,
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
    await _ack(
        event,
        f"Today cleared - {cleared} payment(s) removed from stats.\n"
        f"Today - {fmt_inr(0)} - 0 payments"
    )


async def _find_groups(query: str) -> list[tuple[object, str]]:
    """Return groups using title words/prefixes before title-only typo matching.

    Admin rights are deliberately NOT pre-checked: dialog entities frequently
    carry stale or empty admin_rights, which was silently dropping the exact
    group the account admins. Whether a link can actually be made is decided by
    TRYING (_export_invite) — the only reliable signal."""
    literal: list[tuple[int, object, str]] = []
    fuzzy: list[tuple[float, object, str]] = []
    async for dialog in client.iter_dialogs():
        if not (getattr(dialog, "is_group", False) or getattr(dialog, "is_channel", False)):
            continue
        name = dialog.name or ""
        uname = getattr(dialog.entity, "username", "") or ""
        if not query.strip():
            literal.append((4, dialog.entity, name))
            continue

        candidate = {
            "title": name,
            "username": uname,
            "short_code": "",
        }
        rank = group_literal_rank(query, candidate)
        if rank:
            literal.append((rank, dialog.entity, name))
            continue
        score = group_fuzzy_score(query, candidate)
        threshold = fuzzy_group_threshold(query)
        if score >= threshold:
            fuzzy.append((score, dialog.entity, name))

    if literal:
        best_rank = max(rank for rank, _entity, _name in literal)
        selected = [
            (entity, name)
            for rank, entity, name in literal
            if rank == best_rank
        ]
        selected.sort(key=lambda item: item[1].casefold())
        return selected

    fuzzy.sort(key=lambda item: (-item[0], item[2].casefold()))
    if len(fuzzy) > 1 and fuzzy[0][0] - fuzzy[1][0] >= 0.08:
        fuzzy = fuzzy[:1]
    return [(entity, name) for _score, entity, name in fuzzy]


async def cmd_group_link(event) -> None:
    """/link <name>: search groups this account admins (fancy fonts folded to
    plain text), create an approval-required invite link, and reply with the
    match(es). The first match becomes {link} for later /add posts."""
    m = re.match(r"^/link(?:\s+([\s\S]+))?$", event.raw_text or "")
    query = (m.group(1).strip() if m and m.group(1) else "")
    if not query:
        await _ack(event, "Usage: /link <group name> - searches groups you admin.")
        return
    try:
        matches = await _find_groups(query)
    except Exception as e:  # noqa: BLE001
        await _ack(event, f"Group search failed: {type(e).__name__}: {e}")
        return
    if not matches:
        await _ack(event, f'No admin group matches "{query}".')
        return

    lines = []
    selected_link = selected_title = None
    for entity, name in matches[:10]:
        link = await _export_invite(entity)
        if link and selected_link is None:
            selected_link, selected_title = link, name
        lines.append(f"{name}\n{link or 'could not create a link'}")
    if selected_link:
        _state["selected_link"] = selected_link
        _state["selected_title"] = selected_title
        print(ui.green("[link] ") + f"{selected_title}: {ui.bold(selected_link)}", flush=True)
    await _ack(event, "\n\n".join(lines))


async def cmd_groups(event) -> None:
    """/groups: list the groups/channels this account can invite to."""
    try:
        groups = await _find_groups("")
    except Exception as e:  # noqa: BLE001
        await _ack(event, f"Group list failed: {type(e).__name__}: {e}")
        return
    if not groups:
        await _ack(event, "No groups you admin were found.")
        return
    lines = [name for _, name in groups[:50]]
    await _ack(event, "Groups you admin:\n" + "\n".join(lines))


async def cmd_remove(event) -> None:
    """/remove <orderid>: revoke+delete that order's invite link and ban the
    payer from the group."""
    m = re.match(r"^/remove(?:\s+(\S+))?$", event.raw_text or "")
    oid = (m.group(1).strip() if m and m.group(1) else "")
    if not oid:
        await _ack(event, "Usage: /remove <orderid>")
        return
    order = next(
        (p for p in _pay.get("payments", [])
         if str(p.get("order_id", "")).upper() == oid.upper()),
        None,
    )
    if not order:
        await _ack(event, f"No order {oid}.")
        return
    gid = order.get("group_id")
    await _revoke_and_delete_link(gid, order.get("invite_link"))
    banned = False
    pid = order.get("payer_id")
    if gid and pid:
        try:
            await client.edit_permissions(gid, pid, view_messages=False)  # ban
            banned = True
        except Exception as e:  # noqa: BLE001
            ui.warn(f"Couldn't ban {pid}: {type(e).__name__}: {e}")
    order["join_status"] = "removed"
    _save_pay()
    await _ack(event, f"Order {order.get('order_id')} removed. Link revoked"
                      + (" and user banned." if banned else "."))


# Every restriction lifted: an all-false ChatBannedRights drops the user from
# the chat's banned list instead of re-adding them as a member.
_UNBAN_RIGHTS = ChatBannedRights(until_date=None, view_messages=False)


def _user_label(entity, user_id: int) -> str:
    username = getattr(entity, "username", "") or ""
    if username:
        return f"@{username} ({user_id})"
    name = " ".join(
        part
        for part in (
            getattr(entity, "first_name", "") or "",
            getattr(entity, "last_name", "") or "",
        )
        if part
    ).strip()
    return f"{name} ({user_id})" if name else str(user_id)


async def _resolve_unban_target(event, argument: str) -> tuple[int, str]:
    """Return ``(user_id, label)`` for a `.unban` argument or replied message."""
    parts = argument.split()
    value = parts[0] if parts else ""
    if not value:
        reply = None
        if hasattr(event, "get_reply_message"):
            reply = await event.get_reply_message()
        sender_id = _optional_int(getattr(reply, "sender_id", None))
        if sender_id is None:
            raise ValueError(
                "Usage: .unban <user id|@username> - or reply to the user."
            )
        return sender_id, str(sender_id)

    link = re.match(r"^(?:https?://)?t\.me/([A-Za-z][\w]{3,})/?$", value, re.I)
    if link:
        value = f"@{link.group(1)}"
    numeric = _optional_int(value)
    if numeric is not None:
        # A raw id needs no lookup: the banned lists below carry the full user
        # object, so this works for ids the account has never had a chat with.
        return numeric, str(numeric)
    try:
        entity = await client.get_entity(value)
    except Exception as error:  # noqa: BLE001
        raise ValueError(
            f"Couldn't resolve {value}: {type(error).__name__}: {error}"
        ) from error
    user_id = _optional_int(getattr(entity, "id", None))
    if user_id is None or not isinstance(entity, tl_types.User):
        raise ValueError(f"{value} is not a user.")
    return user_id, _user_label(entity, user_id)


def _is_guarded_channel(entity, chat_id: int) -> bool:
    """guard.py permanently bans every non-admin in CHANNEL by design."""
    guarded = config.channel()
    if not guarded:
        return False
    if isinstance(guarded, int):
        return chat_id == guarded or getattr(entity, "id", None) == abs(guarded)
    username = (getattr(entity, "username", "") or "").casefold()
    return bool(username) and username == str(guarded).lstrip("@").casefold()


async def _ban_state(chat, user_id: int) -> tuple[bool, object]:
    """Return ``(is_banned, target)`` for ``user_id`` in ``chat``.

    ``target`` is what EditBannedRequest should be handed: the id itself when
    Telethon can resolve it, or the full user recovered from the chat's ban list
    when it cannot.
    """
    try:
        permissions = await client.get_permissions(chat, user_id)
    except FloodWaitError as error:
        await asyncio.sleep(min(int(error.seconds), 60) + 1)
        return False, None
    except UserNotParticipantError:
        return False, None          # definitively not banned in this chat
    except (AttributeError, TypeError, ValueError):
        pass                        # unknown id: recover it from the ban list
    except Exception:  # noqa: BLE001 - no admin rights, not a channel, ...
        return False, None
    else:
        return bool(getattr(permissions, "is_banned", False)), user_id

    member = await _banned_participant(chat, user_id)
    return member is not None, member


async def _banned_participant(chat, user_id: int):
    """Return the banned/restricted user object for ``user_id`` in ``chat``.

    Reading the ban lists (instead of asking for one participant) resolves the
    full user — access hash included — which is what makes `.unban <raw id>`
    work in chats the account has no cached entity for.
    """
    for participant_filter in (
        ChannelParticipantsKicked(q=""),
        ChannelParticipantsBanned(q=""),
    ):
        try:
            async for user in client.iter_participants(
                chat, filter=participant_filter
            ):
                if user.id == user_id:
                    return user
        except FloodWaitError as error:
            await asyncio.sleep(min(int(error.seconds), 60) + 1)
        except Exception:  # noqa: BLE001 - not a channel, or no admin rights
            continue
    return None


async def _lift_ban(chat, member) -> tuple[bool, str]:
    for _ in range(2):
        try:
            await client(EditBannedRequest(chat, member, _UNBAN_RIGHTS))
            return True, ""
        except FloodWaitError as error:
            await asyncio.sleep(min(int(error.seconds), 60) + 1)
        except Exception as error:  # noqa: BLE001
            return False, f"{type(error).__name__}: {error}"
    return False, "flood limited"


def _clear_removed_orders(user_id: int) -> int:
    """Retire this account's own "removed" markers for an unbanned buyer."""
    cleared = 0
    for payment in _pay.get("payments", []):
        if (
            _optional_int(payment.get("payer_id")) == user_id
            and payment.get("join_status") == "removed"
        ):
            payment["join_status"] = "unbanned"
            cleared += 1
    if cleared:
        try:
            _save_pay()
        except Exception as error:  # noqa: BLE001
            ui.warn(f"Couldn't record the unban in pay.json: {error}")
    return cleared


def _queue_bot_unban(user_id: int) -> bool:
    """Have the admin bot drop its durable ban records for this user too.

    The bot retries permanent bans from its own SQLite journal, so without this
    handoff its next sweep would simply re-ban whoever was just unbanned.
    """
    if linkstore is None:
        return False
    try:
        linkstore.queue_unban(user_id)
        return True
    except Exception as error:  # noqa: BLE001
        ui.warn(
            "Couldn't hand the unban to the admin bot: "
            f"{type(error).__name__}: {error}"
        )
        return False


async def cmd_unban(event) -> None:
    """`.unban <id|@username>` (or reply): lift the ban in every group."""
    match = _UNBAN_RE.match((getattr(event, "raw_text", "") or "").strip())
    argument = (match.group(1).strip() if match and match.group(1) else "")
    try:
        user_id, label = await _resolve_unban_target(event, argument)
    except ValueError as error:
        await _ack(event, str(error))
        return

    try:
        chats = []
        async for dialog in client.iter_dialogs():
            if not (
                getattr(dialog, "is_group", False)
                or getattr(dialog, "is_channel", False)
            ):
                continue
            entity = dialog.entity
            chats.append((entity, dialog.name or "", get_peer_id(entity)))
    except Exception as error:  # noqa: BLE001
        await _ack(event, f"Chat list failed: {type(error).__name__}: {error}")
        return

    unbanned, failed, guarded = [], [], []
    for entity, name, chat_id in chats:
        title = name or str(chat_id)
        if _is_guarded_channel(entity, chat_id):
            guarded.append(title)
            continue
        is_banned, member = await _ban_state(entity, user_id)
        if not is_banned or member is None:
            continue
        ok, error = await _lift_ban(entity, member)
        if ok:
            unbanned.append(title)
            print(ui.green("[unban] ") + f"{user_id} in {title}", flush=True)
        else:
            failed.append(f"{title}: {error}")

    async with _payment_lock:
        _clear_removed_orders(user_id)
    _queue_bot_unban(user_id)

    if not unbanned and not failed:
        lines = [f"No ban found for {label} in {len(chats)} chat(s)."]
    else:
        lines = [f"Unbanned {label} in {len(unbanned)} of {len(chats)} chat(s)."]
        if unbanned:
            lines.append("\n".join(unbanned[:20]))
            if len(unbanned) > 20:
                lines.append(f"+{len(unbanned) - 20} more")
    if failed:
        lines.append("Failed:\n" + "\n".join(failed[:10]))
    if guarded:
        lines.append("Skipped, guard.py enforces it: " + ", ".join(guarded))
    await _ack(event, "\n\n".join(lines))


async def _notify_owner(text: str) -> None:
    """Send a note to the configured OWNER (falls back to Saved Messages)."""
    target = config.owner() or _state.get("self_id") or "me"
    try:
        await client.send_message(target, text)
    except Exception as e:  # noqa: BLE001
        ui.warn(f"Couldn't notify owner: {type(e).__name__}: {e}")


async def _revoke_orders_on_join(chat_id: int, joined_ids: list[int]) -> None:
    """When a paid user joins a tracked group, revoke+delete that order's link."""
    changed = False
    for p in _pay.get("payments", []):
        if p.get("join_status") != "awaiting" or p.get("group_id") != chat_id:
            continue
        pid = p.get("payer_id")
        if pid and pid not in joined_ids:
            continue
        await _revoke_and_delete_link(chat_id, p.get("invite_link"))
        p["join_status"] = "joined"
        changed = True
        await _notify_owner(
            f"Paid user joined {p.get('group_title') or chat_id}. "
            f"Order {p.get('order_id')} link revoked."
        )
        print(ui.green("[join] ") + f"{p.get('order_id')} joined; link revoked", flush=True)
    if changed:
        _save_pay()


async def on_chat_action(event) -> None:
    """(a) this account added to a group -> tell the owner.
       (b) a paid user joins a tracked group -> revoke that order's link."""
    try:
        if not (getattr(event, "user_added", False) or getattr(event, "user_joined", False)):
            return
        ids = list(event.user_ids or ([event.user_id] if event.user_id else []))
        if _state["self_id"] in ids:
            try:
                chat = await event.get_chat()
                title = getattr(chat, "title", None) or str(event.chat_id)
            except Exception:  # noqa: BLE001
                title = str(event.chat_id)
            await _notify_owner(f"Added to group: {title}\nchat id: {event.chat_id}")
            print(ui.green("[added] ") + f"{title} ({event.chat_id})", flush=True)
            return
        await _revoke_orders_on_join(event.chat_id, ids)
    except Exception as e:  # noqa: BLE001
        ui.error(f"chat action handler failed: {type(e).__name__}: {e}")


async def on_raw_update(update) -> None:
    """Best-effort: auto-approve a paid user's join request on the exact group
    the order targets. Silently no-ops if the account doesn't get the update."""
    try:
        if not isinstance(update, UpdatePendingJoinRequests):
            return
        gid = get_peer_id(update.peer)
        requesters = list(getattr(update, "recent_requesters", []) or [])
        if not requesters:
            return
        peer = None
        for p in _pay.get("payments", []):
            if p.get("join_status") != "awaiting" or p.get("group_id") != gid:
                continue
            pid = p.get("payer_id")
            if not pid or pid not in requesters:
                continue
            if peer is None:
                peer = await client.get_input_entity(update.peer)
            try:
                await client(HideChatJoinRequestRequest(peer=peer, user_id=pid, approved=True))
                print(ui.green("[approve] ") + f"{p.get('order_id')} approved {pid}", flush=True)
            except Exception as e:  # noqa: BLE001
                ui.warn(f"Couldn't approve {pid}: {type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        ui.error(f"join-request handler failed: {type(e).__name__}: {e}")


HELP_TEXT = (
    "<b>Payment logger</b> (any chat)\n"
    "<code>.ping</code> - verify the quick-reply userbot is running\n"
    "<code>/add &lt;amount&gt; &lt;name&gt; &lt;kw...&gt;</code> - in the payer's DM: the BOT reserves a link per keyword (or <code>all</code>) for that user; links go where {link} is, numbered/bold/blockquoted\n"
    "<code>/setdone &lt;template&gt;</code> - message sent to the user in the private chat\n"
    "<code>/setchannelpostofpayment &lt;template&gt;</code> - safe caption for the channel post (private invite URLs are never copied there)\n"
    "<code>.setchannel</code> - type it in the target channel, or "
    "<code>.setchannel &lt;id|@username&gt;</code> from anywhere; pending "
    "deliveries follow the new target\n"
    "<code>.unban &lt;id|@username&gt;</code> - lift that user's ban in every "
    "group this account admins (or reply to them)\n"
    "<code>/stats</code> - today's total, count, and split\n"
    "<code>/cancel</code> - in the upload channel, reply to a payment post: mark it fake, remove it from stats, and revoke buyer-bound access\n"
    "<code>/clear</code> - reset today's stats to zero\n\n"
    "<b>Template parameters</b>\n"
    "<code>{amount}</code> this payment - <code>{name}</code> name from /add - "
    "<code>{orderid}</code> unique order id (e.g. ANI7F3K9Q) - "
    "<code>{link}</code> buyer-bound invite link(s), private receipt only - "
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
    "<b>Backup + screenshots</b> (Saved Messages)\n"
    "<code>.save</code> - full backup ZIP (profile, photos, Business, every quick reply + media, "
    "payments/templates data) sent here; no session or API keys inside\n"
    "<code>.sszip [today|yesterday|YYYY-MM-DD]</code> - that IST day's payment screenshots as a ZIP\n"
    "<code>.ssauto [on|off]</code> - auto-send each finished day's ZIP; "
    "<code>.ssto &lt;@channel|id&gt;</code> - where it goes\n"
    "<code>.setup</code> - on a NEW account: rebuild it from the newest backup (or reply to a "
    "backup ZIP); preview first, then <code>.setup confirm</code>. Username and gifts are not moved\n"
    "<b>Separator</b> (any chat)\n"
    "<code>.separator on|off|status</code> - DMs from outside India go to the "
    "<b>Videshi</b> folder and leave your Unread folder; when it is full (200) the "
    "quietest chats leave for new ones. Indian DMs stay put. Runs on every start; "
    "<code>.separator scan</code> re-sorts now; "
    "<code>.separator check</code> in a DM shows its country in Saved Messages\n"
    "<code>.gdpa</code> - in a customer's DM: deletes itself and puts their country's "
    "per capita income ($) in your draft, without sending it\n"
    "<b>/category</b> (Business quick reply)\n"
    "Once per chat. Sent again later: the new copy is deleted. Sent twice within 1 minute: "
    "both are deleted and the next one is allowed. Outside India, /category is deleted and "
    "<code>/forgein</code> is sent instead (once per chat). <code>.categoryguard reset</code> in a chat "
    "allows it again there; <code>.categoryguard</code> shows the status\n"
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
    _state["last"] = link
    # Group/link management belongs to the BOT (bot/). By default this userbot
    # NEVER changes the demo link — it just records what it saw.
    if not config.USERBOT_RELAY_LINK:
        print(ui.dim("[link] seen but demo link is frozen (USERBOT_RELAY_LINK=0)"), flush=True)
        return
    status = await update_link(config.SHORTCUT, link)
    print(ui.green(f"[/{config.SHORTCUT}] ") + f"{ui.bold(link)} ({status})", flush=True)
    if config.SWAP_GREETING:
        g_status = await _swap_in_greeting(link)
        print(ui.green("[greeting] ") + f"{ui.bold(link)} ({g_status})", flush=True)
    else:
        print(ui.dim("[greeting] link frozen (SWAP_GREETING=0)"), flush=True)


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
    if _category is not None and chat_id != _state["self_id"]:
        _watch_category(event)

    _mark_owner_active()
    if event.is_private:
        _sep_touch(chat_id, raw_text, getattr(event, "date", None))

    # Payment logger + help work in ANY chat (a DM with a customer, the channel,
    # or Saved Messages) — handle them before the Saved-Messages-only gate below.
    # /setdone and /setchannelpostofpayment are matched before /set (Saved
    # Messages) so the longer commands win.
    low_cmd = raw_text.strip().lower()
    if low_cmd in (".ping", "/ping"):
        await _ack(event, _runtime_status_text())
        return
    if low_cmd in (".help", "/help", "/commands", "/start"):
        await _ack(event, HELP_TEXT, parse_mode="html")
        return
    if _CATEGORY_RE.match(low_cmd):
        await cmd_categoryguard(event)
        return
    if _SEPARATOR_RE.match(low_cmd):
        await cmd_separator(event)
        return
    if low_cmd in (".gdpa", "/gdpa"):
        await cmd_gdpa(event)
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
    # /link, /groups, /remove and group-access live in the BOT (bot/), not here.
    if _SETCHANNEL_RE.match(raw_text.strip()):
        async with _payment_lock:
            await cmd_setchannel(event)
        return
    if _UNBAN_RE.match(raw_text.strip()):
        await cmd_unban(event)
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
    command = low.split()[0] if low else ""
    if command == ".save":
        await cmd_save(event)
        return
    if command == ".sszip":
        await cmd_sszip(event)
        return
    if command == ".ssauto":
        await cmd_ssauto(event)
        return
    if command == ".ssto":
        await cmd_ssto(event)
        return
    if command == ".setup":
        await cmd_setup(event)
        return
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


# --------------------------------------------------------------------------
# .save backup + payment-screenshot ZIPs (logic lives in quickreply_extras.py)
# --------------------------------------------------------------------------
SS_STATE_FILE = config.DATA_DIR / "ss_archive.json"
_ss_lock = asyncio.Lock()


async def cmd_save(event) -> None:
    """`.save`: full account backup ZIP, verified, sent to Saved Messages."""
    await _ack(event, "Saving everything (profile, photos, Business, quick replies, data)...")
    try:
        result = await extras.save_everything(
            client, data_dir=config.DATA_DIR, env_path=config.ENV_PATH,
            greeting_ref=_load_greeting(),
        )
        await client.send_file("me", file=str(result["path"]), caption=extras.backup_caption(result),
                               force_document=True)
    except Exception as error:  # noqa: BLE001
        await _ack(event, f"Backup failed: {type(error).__name__}: {error}")
        return
    size = result["size"] / (1024 * 1024)
    await _ack(event, ("Backup complete" if result["complete"] else "Backup saved but PARTIAL")
               + f" - {result['files']} file(s), {size:.1f} MB. A copy is kept in "
               f"data/backups/{result['path'].name}.")
    print(ui.green("[save] ") + f"{result['path'].name} ({size:.1f} MB)", flush=True)


async def cmd_sszip(event) -> None:
    """`.sszip [day]`: that IST day's payment screenshots as one ZIP, here."""
    parts = (event.raw_text or "").split()
    try:
        day = extras.parse_day(parts[1] if len(parts) > 1 else "", _TZ)
    except ValueError as error:
        await _ack(event, f"{error}. Usage: .sszip [today|yesterday|YYYY-MM-DD]")
        return
    await _ack(event, f"Downloading payment screenshots for {day}...")
    import tempfile as _tempfile

    async with _ss_lock:
        with _tempfile.TemporaryDirectory(prefix="qr-ss-") as folder:
            try:
                result = await extras.build_ss_zip(
                    client, list(_pay.get("payments", [])), day, _TZ, Path(folder))
                if result["path"] is None:
                    detail = (f"{result['expected']} payment(s) found but no screenshot could be "
                              "downloaded." if result["expected"] else "no payments with a screenshot.")
                    await _ack(event, f"{day}: {detail}")
                    return
                await client.send_file(event.chat_id, file=str(result["path"]),
                                       caption=extras.ss_caption(result), force_document=True)
            except Exception as error:  # noqa: BLE001
                await _ack(event, f"Screenshot ZIP failed: {type(error).__name__}: {error}")
                return
    await _ack(event, f"{day}: {result['count']} screenshot(s) zipped"
               + (f", {len(result['missing'])} missing" if result["missing"] else "") + ".")


def _ss_status_text() -> str:
    state = extras.SSState(SS_STATE_FILE)
    delivered = state.data["delivered"]
    lines = [
        f"Daily screenshot ZIPs: {'ON' if _state['ss_auto'] else 'OFF'} -> {config.SS_CHANNEL_RAW or 'NOT SET'}",
        f"Started: {state.data.get('enabled_since') or 'not yet'} (earlier days: use .sszip <date>)",
    ]
    for day in sorted(delivered)[-5:]:
        info = delivered[day]
        lines.append(f"{day}: {info.get('count', 0)} screenshot(s) delivered")
    for day, failure in sorted(state.data["failures"].items()):
        lines.append(f"{day}: retrying ({failure.get('error', 'failed')})")
    return "\n".join(lines)


async def cmd_ssauto(event) -> None:
    parts = (event.raw_text or "").lower().split()
    if len(parts) > 1 and parts[1] in ("on", "off"):
        enabled = parts[1] == "on"
        try:
            config.save_env({"SS_AUTO": "1" if enabled else "0"})
        except OSError as error:
            ui.warn(f"Couldn't persist SS_AUTO: {error}")
        _state["ss_auto"] = enabled
    await _ack(event, _ss_status_text())


async def cmd_ssto(event) -> None:
    parts = (event.raw_text or "").split()
    if len(parts) < 2:
        await _ack(event, "Usage: .ssto <@channel|id|t.me link>\n" + _ss_status_text())
        return
    try:
        chat_id, title = await _resolve_post_target(parts[1])
    except Exception as error:  # noqa: BLE001
        await _ack(event, f"Couldn't resolve {parts[1]}: {type(error).__name__}: {error}")
        return
    try:
        config.save_env({"SS_CHANNEL": str(chat_id)})
    except OSError as error:
        ui.warn(f"Couldn't persist SS_CHANNEL: {error}")
        config.SS_CHANNEL_RAW = str(chat_id)
    await _ack(event, f"Daily screenshot ZIPs now go to {title} ({chat_id}).")


async def _ss_archive_cycle() -> None:
    if not _state["ss_auto"] or not config.SS_CHANNEL_RAW:
        return
    state = extras.SSState(SS_STATE_FILE)
    today = extras.parse_day("today", _TZ)
    state.ensure_start(today)
    due = state.due_days(today)
    if not due:
        return
    target = await client.get_entity(config.ss_channel())
    async with _ss_lock:
        for day in due:
            try:
                line = await extras.deliver_day(
                    client, list(_pay.get("payments", [])), day, _TZ, target, state)
                print(ui.green("[ss] ") + line, flush=True)
            except Exception as error:  # noqa: BLE001
                detail = f"{type(error).__name__}: {error}"
                attempts = state.mark_failed(day, detail)
                ui.warn(f"[ss] {day} not delivered: {detail}")
                if attempts == 1:
                    await _notify_owner(
                        f"Payment screenshot ZIP for {day} could not be sent to "
                        f"{config.SS_CHANNEL_RAW}: {detail}. It will retry automatically "
                        "(.ssauto shows the status, .ssto changes the channel)."
                    )


async def _ss_archive_loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await _ss_archive_cycle()
        except Exception as error:  # noqa: BLE001
            ui.warn(f"Screenshot ZIP loop failed: {type(error).__name__}: {error}")
        await asyncio.sleep(300)


# --------------------------------------------------------------------------
# .setup - rebuild this account from a .save backup
# --------------------------------------------------------------------------
_pending_setup: dict = {}


async def _setup_source(event):
    """Replied backup ZIP (downloaded) or the newest data/backups ZIP."""
    reply = await event.get_reply_message()
    if reply is not None and getattr(reply, "file", None) is not None:
        name = getattr(reply.file, "name", "") or ""
        if not name.endswith(".zip"):
            raise ValueError("Reply to a quickreply_backup_*.zip file.")
        target = config.DATA_DIR / "backups" / Path(name).name
        target.parent.mkdir(parents=True, exist_ok=True)
        await client.download_media(reply, file=str(target))
        return target
    path = restore.newest_backup(config.DATA_DIR)
    if path is None:
        raise ValueError("No backup found. Run .save on the old account first, or forward its "
                         "quickreply_backup_*.zip here and reply to it with .setup")
    return path


async def cmd_setup(event) -> None:
    """`.setup` preview, `.setup confirm` apply, `.setup cancel`."""
    parts = (event.raw_text or "").lower().split()
    sub = parts[1] if len(parts) > 1 else ""
    if sub == "cancel":
        _pending_setup.clear()
        await _ack(event, "Setup cancelled.")
        return
    if sub == "confirm":
        path = _pending_setup.get("path")
        if not path or time.monotonic() > _pending_setup.get("until", 0):
            await _ack(event, "Nothing to confirm (or it expired). Send .setup first.")
            return
        _pending_setup.clear()
        await _ack(event, "Restoring... this takes a minute (photos and media are uploaded).")
        backup = restore.Backup(path)
        try:
            async with _payment_lock:
                report, greeting = await restore.run(client, backup, dry_run=False,
                                                     data_dir=config.DATA_DIR, sleep=asyncio.sleep,
                                                     pay_store=_pay)
                _save_pay()
        finally:
            backup.close()
        if greeting:
            _save_greeting(greeting["chat_id"], greeting["message_id"])
        head = ("SETUP DONE" if not report.failed else f"SETUP DONE WITH {len(report.failed)} PROBLEM(S)")
        await _ack(event, f"{head}\n\n{report.text()}"[:4000])
        return
    try:
        path = await _setup_source(event)
        backup = restore.Backup(path)
    except Exception as error:  # noqa: BLE001
        await _ack(event, f"Setup: {error}")
        return
    try:
        report, _ = await restore.run(client, backup, dry_run=True, data_dir=config.DATA_DIR,
                                      sleep=asyncio.sleep, pay_store=None)
        info = restore.describe(backup)
        source_id = (backup.doc.get("profile") or {}).get("id")
    finally:
        backup.close()
    same = source_id == _state["self_id"]
    _pending_setup.update(path=path, until=time.monotonic() + 600)
    warn = ("\nNOTE: this backup is from THIS account; restoring adds the photos again.\n" if same else "")
    await _ack(event, f"SETUP PREVIEW (nothing changed yet)\n{info}{warn}\n\n{report.text()}\n\n"
                      "Send .setup confirm within 10 minutes to apply, or .setup cancel."[:4000])


# --------------------------------------------------------------------------
# .separator on|off - file foreign DMs into the "Videshi" folder
# .gdpa            - per capita income of this DM's country, as a draft
# (country detection + folder logic live in separator.py)
# --------------------------------------------------------------------------
_category: category.CategoryGuard | None = None
_category_tasks: set = set()


def _watch_category(event) -> None:
    """Check an outgoing message against /category without delaying anything."""
    async def run():
        try:
            action = await _category.on_outgoing(event.chat_id, event.message)
            notes = {
                "duplicate": "already sent here - deleted the new copy",
                "reset": "sent twice within 1 min - deleted both, next one allowed",
                "foreign": "foreign chat - deleted /category, sent the foreign quick reply",
                "foreign-kept": "foreign chat - deleted /category (foreign reply already sent)",
            }
            if action in notes:
                print(ui.yellow(f"[category] {event.chat_id}: ") + notes[action], flush=True)
        except Exception as error:  # noqa: BLE001
            ui.error(f"[category] {event.chat_id}: {type(error).__name__}: {error}")
    task = asyncio.create_task(run())
    _category_tasks.add(task)
    task.add_done_callback(_category_tasks.discard)


async def _category_is_foreign(chat_id) -> bool:
    """Outside India (unknown country = not foreign, /category stays)."""
    if not config.FOREIGN_SHORTCUT or _sep_state is None:
        return False
    entity = await client.get_entity(chat_id)
    if not isinstance(entity, tl_types.User) or entity.bot:
        return False
    peer = await client.get_input_entity(chat_id)
    country = await separator.country_for(client, _sep_state, peer, chat_id, entity)
    return bool(country) and country != separator.HOME_COUNTRY


async def _send_foreign_reply(chat_id) -> None:
    """Send the first existing FOREIGN_SHORTCUT quick reply to this chat."""
    names = [n.strip().lstrip("/").lower() for n in config.FOREIGN_SHORTCUT.split(",") if n.strip()]
    res = await client(GetQuickRepliesRequest(hash=0))
    shortcuts = {getattr(q, "shortcut", "").lower(): q for q in getattr(res, "quick_replies", []) or []}
    target = next((shortcuts[n] for n in names if n in shortcuts), None)
    if target is None:
        raise ValueError(f"quick reply /{names[0] if names else 'forgein'} not found")
    msgs = await client(GetQuickReplyMessagesRequest(shortcut_id=target.shortcut_id, hash=0, id=None))
    shortcut_msgs = sorted(getattr(msgs, "messages", []) or [], key=lambda m: m.id)
    ids = [m.id for m in shortcut_msgs]
    if not ids:
        raise ValueError(f"quick reply /{target.shortcut} is empty")
    peer = await client.get_input_entity(chat_id)
    marker = _expect_automatic_outgoing(chat_id, [getattr(m, "message", "") or "" for m in shortcut_msgs])
    try:
        result = await client(SendQuickReplyMessagesRequest(
            peer=peer, shortcut_id=target.shortcut_id, id=ids,
            random_id=[random.randrange(-(2 ** 63), 2 ** 63) for _ in ids]))
    except Exception:
        _cancel_automatic_outgoing(chat_id, marker)
        raise
    _finish_automatic_outgoing(chat_id, marker, result)
    return sorted(_sent_message_ids(result))


async def cmd_categoryguard(event) -> None:
    """`.categoryguard [reset|status]`: reset allows /category again in this chat."""
    if _category is None:
        await _ack(event, "The /category guard is off (CATEGORY_SHORTCUT is blank).")
        return
    choice = _CATEGORY_RE.match((event.raw_text or "").strip()).group(1) or "status"
    if choice == "reset":
        if event.chat_id == _state["self_id"]:
            await _ack(event, "Send .categoryguard reset inside the chat to reset.")
            return
        done = _category.forget(event.chat_id)
        try:
            await event.delete()
        except Exception:  # noqa: BLE001
            pass
        if not done:
            await client.send_message("me", "/category hadn't been used in that chat yet.")
        return
    found = await _category.refresh(force=True)
    await _ack(event, (f"/{_category.shortcut} guard: used in {len(_category.chats)} chat(s). "
                       if found else f"Quick reply /{_category.shortcut} not found. ")
               + "Once per chat; again later = new copy deleted; twice within 1 min = "
               "both deleted and the next one is allowed. "
               ".categoryguard reset (in a chat) allows it again there.")


_sep_state: separator.SeparatorState | None = None
_sep_folders: separator.FolderManager | None = None
_sep_tasks: set = set()
_sep_scan: dict = {}
# Live DMs found to be foreign wait here briefly so many of them cost one
# folder edit (Telegram rate-limits folder edits hard).
_sep_pending: dict = {}
_sep_wake: asyncio.Event | None = None
_sep_commit_lock = asyncio.Lock()
SCAN_DIALOG_LIMIT = 1000   # most recent private chats checked by a scan
SCAN_CONCURRENCY = 8       # country lookups in flight at once
FLUSH_DELAY = 1.5          # seconds to gather live DMs into one folder edit


def _sep_init(premium: bool) -> None:
    """Create the separator state for this account (called from main)."""
    global _sep_state, _sep_folders, _sep_wake
    _sep_state = separator.SeparatorState(config.DATA_DIR / "separator.json")
    limit = separator.FOLDER_FULL_AT * (2 if premium else 1)
    _sep_folders = separator.FolderManager(client, limit=limit)
    _sep_wake = asyncio.Event()


def _separator_status_text() -> str:
    filed = len(_sep_state.filed) if _sep_state else 0
    limit = _sep_folders.limit if _sep_folders else separator.FOLDER_FULL_AT
    state = "ON" if _state["separator"] else "OFF"
    return (f"Separator is {state}. {filed}/{limit} chats in {separator.FOLDER_TITLE}. "
            f"While ON, DMs from outside India go to {separator.FOLDER_TITLE} and out of your "
            "Unread folder; when it is full the quietest chats (silent 7+ days, few "
            "messages/words) leave to make room for new ones. Indian DMs stay where they "
            "are. Every restart re-sorts your DMs.")


async def cmd_separator(event) -> None:
    """`.separator on|off|status|scan|check`, persisted as SEPARATOR in .env."""
    choice = (_SEPARATOR_RE.match((event.raw_text or "").strip().lower()).group(1) or "status")
    if choice == "check":
        await _separator_check(event)
        return
    if choice in ("on", "off"):
        enabled = choice == "on"
        try:
            config.save_env({"SEPARATOR": "1" if enabled else "0"})
        except OSError as error:
            ui.warn(f"Couldn't persist SEPARATOR: {error}")
        _state["separator"] = enabled
    if choice in ("scan", "on") and _state["separator"]:
        started = _start_separator_scan(event.chat_id)
        await _ack(event, _separator_status_text() + (
            "\nSorting your DMs now; the result is sent here when done."
            if started else "\nA scan is already running."))
        return
    if choice == "scan":
        await _ack(event, "Turn it on first: .separator on")
        return
    text = _separator_status_text()
    try:
        members = await _sep_folders.members()
        text += "\n\nUnread folder:\n" + "\n".join(
            await _sep_folders.unread_report(set(members), _sep_state.excluded | _sep_state.foreign_ids()))
    except Exception as error:  # noqa: BLE001
        text += f"\n\nCouldn't read your folders: {type(error).__name__}: {error}"
    await _ack(event, text)


def _sep_skip_ids() -> list:
    return [_state["source_id"]] if _state["source_id"] else []


async def _sep_fill_stats(user_ids) -> None:
    """Read history (one request per chat) for chats with no recent stats."""
    now = time.time()
    todo = [uid for uid, peer in user_ids.items()
            if now - _sep_state.stats.get(str(uid), {}).get("at", 0) > separator.STATS_MAX_AGE]
    gate = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def one(uid):
        async with gate:
            for attempt in range(2):
                try:
                    stat = await separator.chat_stats(client, user_ids[uid])
                    old = _sep_state.stats.get(str(uid), {})
                    stat["last"] = max(stat["last"], old.get("last", 0))
                    _sep_state.stats[str(uid)] = stat
                    return
                except FloodWaitError as error:
                    if attempt or error.seconds > separator.MAX_FLOOD_SLEEP:
                        return
                    await asyncio.sleep(error.seconds + 1)
                except Exception:  # noqa: BLE001 - unknown stats = never evicted
                    return

    await asyncio.gather(*(one(uid) for uid in todo))


async def _sep_commit(peers: dict, *, live: bool = False) -> tuple[list, list, list, list]:
    """Put foreign chats {uid: peer} in Videshi, rotating out the quietest.

    ``live`` = these chats just messaged you: they get the newcomer
    protection (old chats found by a scan don't). Returns (added ids,
    evicted ids, changed folders, problems); one edit per folder.
    """
    async with _sep_commit_lock:
        members = await _sep_folders.members()
        pool = {**members, **peers}
        now = time.time()
        if live:
            for uid in peers:
                if uid not in members:
                    _sep_state.joined.setdefault(str(uid), now)
        evicted: list = []
        limit = _sep_folders.limit
        if len(pool) > limit:
            await _sep_fill_stats(pool)
            ordered = sorted(pool, reverse=True, key=lambda uid: separator.rank(
                _sep_state.stats.get(str(uid)), _sep_state.joined.get(str(uid)), now))
            left_out = ordered[limit:]
            evicted = [uid for uid in left_out if uid in members]  # really moved out
            for uid in left_out:
                _sep_state.joined.pop(str(uid), None)
            wanted = {uid: pool[uid] for uid in ordered[:limit]}
        else:
            wanted = pool
        def need(uid):  # who most needs hiding from Unread: unread, then most recent
            ts = _sep_state.unread.get(str(uid))
            last = _sep_state.stats.get(str(uid), {}).get("last", 0)
            return (ts is not None, ts or last)
        ours, changed, problems, placed = await _sep_folders.apply(
            wanted, _sep_state.excluded, priority=sorted(wanted, key=need, reverse=True),
            unread={int(u) for u in _sep_state.unread}, foreign_known=_sep_state.foreign_ids())
        _sep_state.excluded = ours
        _sep_state.filed = {str(uid): separator.FOLDER_TITLE for uid in placed}
        wanted = {uid: wanted[uid] for uid in placed}
        _sep_state.save()
    added = [uid for uid in wanted if uid not in members]
    for problem in problems:
        ui.warn(f"[separator] {problem}")
    note = "; ".join(problems)
    if note and note != _state.get("sep_last_problem"):
        _state["sep_last_problem"] = note  # tell the owner once per new problem
        try:
            await client.send_message("me", f"Separator couldn't fully update Unread: {note}")
        except Exception:  # noqa: BLE001
            pass
    elif not note:
        _state.pop("sep_last_problem", None)
    return added, evicted, changed, problems


def _sep_touch(chat_id, text, date, *, incoming=False) -> None:
    """Count a DM message toward that chat's activity (no request).

    Incoming = that chat is unread now; your own message = you've read it.
    """
    if _sep_state is None or not chat_id or chat_id == _state["self_id"]:
        return
    ts = date.timestamp() if date else time.time()
    _sep_state.touch(chat_id, text, ts)
    if incoming:
        _sep_state.unread[str(chat_id)] = int(ts)
    else:
        _sep_state.unread.pop(str(chat_id), None)


async def on_inbox_read(event) -> None:
    """You read a DM (on any device): it no longer needs hiding from Unread."""
    if _sep_state is not None and event.is_private:
        _sep_state.unread.pop(str(event.chat_id), None)


async def _sep_flush_loop() -> None:
    """Commit live foreign DMs in small batches; failures are retried."""
    while True:
        await _sep_wake.wait()
        await asyncio.sleep(FLUSH_DELAY)
        _sep_wake.clear()
        batch = dict(_sep_pending)
        _sep_pending.clear()
        if not batch or not _state["separator"]:
            continue
        try:
            added, evicted, _changed, _problems = await _sep_commit(batch, live=True)
            if added or evicted:
                print(ui.green("[separator] ") + f"{len(added)} DM(s) -> {separator.FOLDER_TITLE}"
                      + (f", {len(evicted)} quiet chat(s) moved out" if evicted else ""),
                      flush=True)
        except Exception as error:  # noqa: BLE001 - keep them and retry
            wait = getattr(error, "seconds", 30) + 1
            ui.warn(f"[separator] folder update failed ({type(error).__name__}: {error}); "
                    f"retrying in {wait}s")
            for uid, peer in batch.items():
                _sep_pending.setdefault(uid, peer)
            await asyncio.sleep(wait)
            _sep_wake.set()


async def _sep_drain(live: bool = False) -> None:
    """Commit whatever is pending right now (tests / shutdown helpers)."""
    batch = dict(_sep_pending)
    _sep_pending.clear()
    if batch:
        await _sep_commit(batch, live=live)


def _start_separator_scan(report_chat) -> bool:
    task = _sep_scan.get("task")
    if task is not None and not task.done():
        return False
    _sep_scan["task"] = asyncio.create_task(_separator_scan(report_chat))
    return True


async def _separator_scan(report_chat) -> None:
    """Sort every recent DM: lookups run in parallel, folders are edited once.

    Telegram's folder contents are the truth: foreign chats missing from
    Videshi are put back (if they rank high enough to fit).
    """
    started = time.monotonic()
    counts = {"india": 0, "unknown": 0, "already": 0, "checked": 0}
    added, evicted, changed, problems = [], [], [], []
    failed = False
    try:
        members = await _sep_folders.members()
        candidates = []
        async for dialog in client.iter_dialogs(limit=SCAN_DIALOG_LIMIT):
            if dialog.is_user and separator.should_check(
                    dialog.entity, _state["self_id"], _sep_skip_ids()):
                candidates.append((dialog.entity, dialog.input_entity))
                uid = str(dialog.entity.id)
                if dialog.unread_count or getattr(dialog.dialog, "unread_mark", False):
                    _sep_state.unread[uid] = int(dialog.date.timestamp()) if dialog.date else int(time.time())
                else:
                    _sep_state.unread.pop(uid, None)
                stat = _sep_state.stats.get(str(dialog.entity.id))
                if stat is not None and dialog.date:  # free last-activity refresh
                    stat["last"] = max(stat.get("last", 0), int(dialog.date.timestamp()))
        counts["checked"] = len(candidates)
        foreign: dict = {}
        gate = asyncio.Semaphore(SCAN_CONCURRENCY)

        async def classify(user, peer):
            if user.id in members:
                counts["already"] += 1
                foreign[user.id] = peer
                return
            country = None
            async with gate:
                for attempt in range(2):
                    try:
                        country = await separator.country_for(
                            client, _sep_state, peer, user.id, user, save=False)
                        break
                    except FloodWaitError as error:
                        if attempt or error.seconds > separator.MAX_FLOOD_SLEEP:
                            break  # not cached: tried again on their next message
                        await asyncio.sleep(error.seconds + 1)
                    except Exception:  # noqa: BLE001
                        break
            if country is None:
                counts["unknown"] += 1
            elif country == separator.HOME_COUNTRY:
                counts["india"] += 1
            else:
                foreign[user.id] = peer

        await asyncio.gather(*(classify(u, p) for u, p in candidates))
        _sep_state.save()
        if _state["separator"] and (foreign or members):
            added, evicted, changed, problems = await _sep_commit(foreign)
        limit = _sep_folders.limit
        text = (f"Separator: sorted {counts['checked']} DMs in "
                f"{time.monotonic() - started:.0f}s. {separator.FOLDER_TITLE} now has "
                f"{len(_sep_state.filed)}/{limit} chats: {len(added)} came in"
                + (f", {len(evicted)} quiet chat(s) moved out to make room" if evicted else "")
                + f". {counts['india']} Indian (left alone), {counts['unknown']} unknown country "
                "(checked again when they message you).")
        if problems:
            text += "\n" + "; ".join(problems)
    except Exception as error:  # noqa: BLE001
        text = (f"Separator scan stopped: {type(error).__name__}: {error}. "
                "It runs again on the next restart, or send .separator scan.")
        ui.error(f"[separator] {text}")
        failed = True
    print(ui.green("[separator] ") + text, flush=True)
    if report_chat is None and not (added or evicted or failed):
        return  # quiet boot: nothing moved (warnings stay in the console)
    try:
        await client.send_message(report_chat or "me", text)
    except Exception:  # noqa: BLE001
        try:
            await client.send_message("me", text)
        except Exception:  # noqa: BLE001
            pass


async def _separate_dm(event) -> None:
    """Live DM: look up the country; foreign chats are queued for a batch edit."""
    try:
        sender = await event.get_sender()
        if not separator.should_check(sender, _state["self_id"], _sep_skip_ids()):
            return
        if str(sender.id) in _sep_state.filed:
            if sender.id not in _sep_state.excluded:  # unread now: hide it from Unread
                _sep_pending[sender.id] = await event.get_input_chat()
                _sep_wake.set()
            return
        peer = await event.get_input_chat()
        country = await separator.country_for(client, _sep_state, peer, sender.id, sender)
        if country is None:
            print(ui.dim(f"[separator] {sender.id}: no phone country yet; "
                         "retrying on a later message"), flush=True)
            return
        if country == separator.HOME_COUNTRY:
            return
        _sep_pending[sender.id] = peer
        _sep_wake.set()
    except FloodWaitError as error:
        ui.warn(f"[separator] flood wait {error.seconds}s; will retry on their next message")
    except Exception as error:  # noqa: BLE001
        ui.error(f"[separator] failed for {event.chat_id}: {type(error).__name__}: {error}")


async def _separator_check(event) -> None:
    """`.separator check` in a DM: fresh lookup, report to Saved Messages only."""
    if not event.is_private or event.chat_id == _state["self_id"]:
        await _ack(event, "Send .separator check inside the customer's DM.")
        return
    try:
        await event.delete()  # the customer must not see it
    except Exception:  # noqa: BLE001
        pass
    user = await event.get_chat()
    peer = await event.get_input_chat()
    name = getattr(user, "first_name", None) or event.chat_id
    country, source = await separator.detect_country(client, peer, user)
    _sep_state.remember(user.id, country, source)
    _sep_state.save()
    lines = [f"Separator check for {name} ({event.chat_id}):",
             f"Country: {country or 'unknown'} (from {source})"]
    if not _state["separator"]:
        lines.append("Separator is OFF, so nothing was moved (.separator on).")
    elif country and country != separator.HOME_COUNTRY:
        added, evicted, _c, _p = await _sep_commit({user.id: peer}, live=True)
        if user.id in _sep_state.filed:
            lines.append(f"In {separator.FOLDER_TITLE}; taken out of any Unread folder."
                         + (f" {len(evicted)} quieter chat(s) moved out to make room." if evicted else ""))
        else:
            lines.append(f"{separator.FOLDER_TITLE} is full of more active chats, so this one "
                         "waits outside until it talks more.")
    elif country == separator.HOME_COUNTRY:
        lines.append("Indian number, so it stays where it is.")
    else:
        lines.append("Telegram doesn't show this user's phone country and their number "
                     "is hidden, so it can't be sorted.")
    await client.send_message("me", "\n".join(lines))


async def _prefetch_country(event) -> None:
    """Remember a DM sender's country in the background so .gdpa is instant."""
    try:
        user_id = event.sender_id
        if not user_id or user_id == _state["self_id"]:
            return
        hit, _country = _sep_state.cached_country(user_id)
        if hit:
            return
        peer = await event.get_input_chat()
        await separator.country_for(client, _sep_state, peer, user_id)
    except Exception:  # noqa: BLE001 - best effort; .gdpa looks it up itself
        pass


def _schedule_separator(event) -> None:
    """Run the separator (or a country prefetch) beside the greeting logic."""
    if _sep_state is None:
        return
    _sep_touch(event.chat_id, event.raw_text, getattr(event, "date", None), incoming=True)
    work = _separate_dm(event) if _state["separator"] else _prefetch_country(event)
    task = asyncio.create_task(work)
    _sep_tasks.add(task)
    task.add_done_callback(_sep_tasks.discard)


async def cmd_gdpa(event) -> None:
    """`.gdpa` in a DM: delete the command, put the per capita income in the draft.

    Nothing is sent to the other person; problems are reported in Saved Messages.
    """
    chat_id = event.chat_id
    if not event.is_private or chat_id == _state["self_id"]:
        await _ack(event, "Use .gdpa inside a private chat with a customer.")
        return
    async def _delete():
        try:
            await event.delete()  # revoke: the customer must not see ".gdpa"
        except Exception as error:  # noqa: BLE001
            ui.warn(f"[gdpa] couldn't delete the command in {chat_id}: {error}")

    # Delete the command while the country is looked up, not one after the other.
    delete_task = asyncio.create_task(_delete())
    try:
        peer = await event.get_input_chat()  # from Telethon's cache, no request
        hit, country = _sep_state.cached_country(chat_id)  # instant when cached
        if country is None:  # not cached, or cached "unknown": ask Telegram once
            country = await separator.country_for(client, _sep_state, peer, chat_id, fresh=True)
        text = percapita.per_capita_text(country) if country else None  # local chart
        if text is None:
            await delete_task
            user = await event.get_chat()
            name = getattr(user, "first_name", None) or chat_id
            reason = (f"no per capita data for country {country}" if country
                      else "Telegram doesn't show their phone country")
            await client.send_message("me", f".gdpa for {name}: {reason}.")
            return
        await client(SaveDraftRequest(peer=peer, message=text))
        print(ui.green("[gdpa] ") + f"{chat_id} ({country}) draft: {text}", flush=True)
        await delete_task
    except Exception as error:  # noqa: BLE001
        await asyncio.gather(delete_task, return_exceptions=True)
        ui.error(f"[gdpa] failed for {chat_id}: {type(error).__name__}: {error}")
        await client.send_message("me", f".gdpa failed for chat {chat_id}: {type(error).__name__}: {error}")


def _command_identity(event) -> str:
    """Stable identity used to prevent two workers handling one command twice."""
    text = (getattr(event, "raw_text", "") or "").strip()
    if not (text.startswith(("/", ".")) or text == "L"):
        return ""
    chat_id = getattr(event, "chat_id", None)
    message_id = getattr(event, "id", None)
    if chat_id is None or message_id is None:
        return ""
    return f"{_state.get('self_id', 0)}:{chat_id}:{message_id}"


async def _renew_command_claim(key: str, token: str) -> None:
    """Keep long-running commands fenced until their handler exits."""
    while True:
        await asyncio.sleep(60)
        try:
            if not linkstore.renew_command(key, token):
                return
        except Exception as error:  # noqa: BLE001
            ui.error(f"command lock renewal failed: {type(error).__name__}: {error}")
            return


async def on_outgoing(event):
    """Run each command message once, even if updates or workers duplicate it."""
    command_key = _command_identity(event)
    command_token = ""
    renew_task = None
    if command_key and linkstore is not None:
        try:
            command_token = linkstore.claim_command(command_key) or ""
        except Exception as error:  # noqa: BLE001
            ui.error(f"command lock failed: {type(error).__name__}: {error}")
            return
        if not command_token:
            return
        renew_task = asyncio.create_task(
            _renew_command_claim(command_key, command_token)
        )
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
            await _ack(event, f"Command failed - {detail}")
    finally:
        if renew_task is not None:
            renew_task.cancel()
            await asyncio.gather(renew_task, return_exceptions=True)
        if command_key and command_token:
            try:
                linkstore.complete_command(command_key, command_token)
            except Exception as error:  # noqa: BLE001
                ui.error(f"command completion save failed: {type(error).__name__}: {error}")


def _is_guard_demo_link(sender_id: int, link: str) -> bool:
    """Accept /demo rotations only from guard.py, never from the admin bot."""
    try:
        published_by_guard = (
            linkstore is not None and linkstore.is_demo_link(link)
        )
    except Exception:  # noqa: BLE001
        published_by_guard = False
    source_id = _state["source_id"]
    if source_id is not None:
        return sender_id == source_id and published_by_guard
    return published_by_guard


async def on_msg(event):
    if not event.is_private:
        return
    _schedule_separator(event)
    sender = event.sender_id
    src = _state["source_id"]

    # Only guard.py may rotate /demo. The link must match guard.py's local
    # handoff; LINK_SOURCE, when configured, is an additional sender check.
    match = LINK_RE.search(event.raw_text or "")
    if match and _is_guard_demo_link(sender, match.group(0)):
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
        if status in {"greeting", "away"}:
            _greeted.add(sender)
            _save_greeted()
            print(ui.green("[greet] ") + f"sent {status} to {sender}", flush=True)
        elif status == "none":
            ui.warn(f"No greeting set (reply to a post with /set). Skipped {sender}.")
        else:
            print(ui.dim(f"[greet] owner became active or away was disabled; skipped {sender}"),
                  flush=True)
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
        raise SystemExit(1)

    client = TelegramClient(config.QR_SESSION, config.API_ID, config.API_HASH)
    await client.start()  # prompts phone + OTP for THIS account on first run
    me = await client.get_me()
    if not _acquire_single_instance_lock(_account_instance_lock_path(me.id)):
        ui.error(
            "Another checkout is already running quickreply.py for this Telegram "
            "account. Stop every stale quickreply.py process, then start one copy."
        )
        await client.disconnect()
        raise SystemExit(1)
    _state["self_id"] = me.id

    global _greeted, _pay
    _greeted = _load_greeted()
    _pay = _load_pay()
    _sep_init(bool(getattr(me, "premium", False)))
    global _category
    if config.CATEGORY_SHORTCUT:
        _category = category.CategoryGuard(
            client, config.CATEGORY_SHORTCUT, config.DATA_DIR / "category.json",
            is_foreign=_category_is_foreign, send_foreign=_send_foreign_reply)
        try:
            if not await _category.refresh(force=True):
                ui.warn(f"Quick reply /{config.CATEGORY_SHORTCUT} not found yet; "
                        "the once-per-chat guard starts when you create it.")
        except Exception as error:  # noqa: BLE001
            ui.warn(f"/category guard: couldn't read quick replies ({error})")
    await _recover_pending_payments()

    src_raw = config.LINK_SOURCE_RAW.strip()
    if not src_raw:
        src_raw = ui.ask(
            "Guard account that sends demo links (username/id, blank = local guard handoff)",
            default="",
        )
        if src_raw:
            config.save_env({"LINK_SOURCE": src_raw})
    if src_raw:
        try:
            ent = await client.get_entity(config.coerce(src_raw))
            _state["source_id"] = ent.id
        except Exception:
            ui.warn("Couldn't resolve LINK_SOURCE — /demo will accept only the "
                    "link published by local guard.py.")

    # Incoming DMs -> link update / greet. All outgoing messages -> presence,
    # Saved Messages controls, and the private-chat L action.
    client.add_event_handler(on_msg, events.NewMessage(incoming=True))
    client.add_event_handler(on_outgoing, events.NewMessage(outgoing=True))
    client.add_event_handler(on_inbox_read, events.MessageRead(inbox=True))

    where = (f"from {src_raw}" if _state["source_id"]
             else "from local guard.py handoff only")
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
    total = _payment_total(day)
    pc_state = str(pc) if pc else ui.yellow(
        "not set - .setchannel in the channel, or .setchannel <id|@username>"
    )
    pending = len(_pending_channel_payments())
    ui.info(f"Payment logger: post channel [{pc_state}]. "
            + ui.dim(f"today {fmt_inr(total)} / {len(day)} payment(s). '.help' for commands."))
    if pending:
        ui.warn(f"{pending} payment(s) still waiting for channel delivery.")
    if linkstore is not None:
        ui.info("Bot link bridge: " + ui.bold("ready")
                + ui.dim(f" ({linkstore.REQUESTS}) — the bot must run from the same folder."))
    else:
        ui.warn("Bot link bridge: NOT loaded — run this from the repo root so "
                "'import linkstore' works, or /add <keyword> can't reach the bot.")
    print(ui.dim("Ctrl+C to stop."))
    ui.info("Backup: .save  |  screenshot ZIPs: .sszip [day], daily auto "
            + (f"ON -> {config.SS_CHANNEL_RAW}" if _state["ss_auto"] else "OFF (.ssauto on)"))
    ui.info(f"Separator: {'ON' if _state['separator'] else 'OFF'} "
            + ui.dim(f"(foreign DMs -> '{separator.FOLDER_TITLE}' folder; .separator on|off, .gdpa)"))
    recovery_task = asyncio.create_task(
        _payment_recovery_loop(), name="payment-channel-recovery"
    )
    ss_task = asyncio.create_task(_ss_archive_loop(), name="payment-screenshot-zips")
    sep_task = asyncio.create_task(_sep_flush_loop(), name="separator-flush")
    if _state["separator"]:
        _start_separator_scan(None)  # every boot: sort all recent DMs
    try:
        await client.run_until_disconnected()
    finally:
        for task in (recovery_task, ss_task, sep_task):
            task.cancel()
        await asyncio.gather(recovery_task, ss_task, sep_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
