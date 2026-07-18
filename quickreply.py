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

Business quick replies require Telegram Premium.

Run:  python quickreply.py    (Ctrl+C to stop)
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
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
    MessageMediaWebPage,
    UserStatusOffline,
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
except (ImportError, KeyError):
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")

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
    """Use live Telegram presence plus recent manual activity from any session."""
    if time.monotonic() < _state["owner_active_until"]:
        return True
    try:
        me = await client.get_me()
        status = getattr(me, "status", None)
        if isinstance(status, UserStatusOnline):
            return status.expires.timestamp() > time.time()
        # Unknown/approximate presence must fail closed. Only Telegram's
        # explicit offline status authorizes an away reply.
        return not isinstance(status, UserStatusOffline)
    except Exception as e:  # noqa: BLE001
        ui.warn(f"Couldn't verify owner presence; suppressing away reply: {type(e).__name__}")
        return True


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
    g_status = await _swap_in_greeting(link)
    print(ui.green("[greeting] ") + f"{ui.bold(link)} ({g_status})", flush=True)
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


async def on_outgoing(event):
    """Track owner activity, handle Saved Messages controls, and process L."""
    chat_id = event.chat_id
    raw_text = event.raw_text or ""

    # Programmatic greeting sends arrive as outgoing updates too. Consume only
    # the exact message body we registered, not every send in that chat.
    if chat_id != _state["self_id"] and await _consume_automatic_outgoing(event):
        return

    _mark_owner_active()

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
        if link:
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
        _greeted.add(sender)
        _save_greeted()
        print(ui.dim(f"[greet] owner online; skipped {sender}"), flush=True)
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

    client = TelegramClient(config.QR_SESSION, config.API_ID, config.API_HASH)
    await client.start()  # prompts phone + OTP for THIS account on first run
    me = await client.get_me()
    _state["self_id"] = me.id

    global _greeted
    _greeted = _load_greeted()

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
    print(ui.dim("Ctrl+C to stop."))
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
