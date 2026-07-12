"""Quick-reply userbot (second account, typically the OWNER).

Jobs:
  1. When it receives a t.me invite link from LINK_SOURCE (the guard account),
     it swaps ONLY the link inside your "/SHORTCUT" post AND inside the greeting
     post — text, markdown, and premium (custom) emoji stay exactly as they were.
  2. When someone DMs this account for the FIRST time, it sends them the greeting
     post (GREET_NEW=1). Set the greeting from your own Saved Messages: reply to
     any post with /set. (Falls back to the Business away message if no greeting
     is set.)
  3. Payment logger: reply to an image with /add <amount> [name] to record a
     payment (INR), auto-post that image + a templated caption to your post
     channel, and track a daily total/count.

Commands (send them yourself — outgoing):
  /set              reply to a post -> use it as the greeting  (Saved Messages)
  /unset            clear the greeting                          (Saved Messages)
  /show             whether a greeting is set                   (Saved Messages)
  /add <amt> [name] reply to an image -> log payment + post it  (any chat)
  /setdone <tpl>    set the post caption template (or reply to a post)
  /setpostchannel   set where media is posted (blank = current chat)
  /stats            today's total (INR) + payment count + split
  /scan             list your chats/channels in the terminal
  .help             show every command and template parameter

Business quick replies require Telegram Premium (the payment logger does not).

Run:  python quickreply.py    (Ctrl+C to stop)
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

from telethon import TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.tl.functions.messages import (
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
)

import config
import ui

# The link the guard sends us (full https invite link).
LINK_RE = re.compile(r"https?://t\.me/(?:joinchat/|\+)[\w-]+", re.IGNORECASE)
# The link inside the saved post (may or may not include the scheme).
FIND_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)[\w-]+", re.IGNORECASE)

client: TelegramClient | None = None
_state = {"source_id": None, "last": None, "self_id": 0}
_greeted: set[int] = set()

# Payment logger state: {"post_channel": int|None, "done_template": str, "payments": [...]}
_pay: dict = {}
_TZ = ZoneInfo(config.TZ_NAME) if ZoneInfo else None

DEFAULT_TEMPLATE = (
    "New payment received\n"
    "{name} paid {amount}\n\n"
    "Rio {rioshare} - Marco {marco}\n"
    "Payments today: {total}"
)


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


# --------------------------------------------------------------------------
# Payment logger
# --------------------------------------------------------------------------
def _load_pay() -> dict:
    if config.PAY_FILE.exists():
        try:
            data = json.loads(config.PAY_FILE.read_text())
            data.setdefault("post_channel", None)
            data.setdefault("done_template", DEFAULT_TEMPLATE)
            data.setdefault("payments", [])
            return data
        except (ValueError, OSError):
            pass
    return {"post_channel": None, "done_template": DEFAULT_TEMPLATE, "payments": []}


def _save_pay() -> None:
    config.PAY_FILE.write_text(json.dumps(_pay, ensure_ascii=False, indent=2))


def _now_ts() -> float:
    return datetime.now(_TZ).timestamp() if _TZ else datetime.now().timestamp()


def _today_key(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(_now_ts() if ts is None else ts, _TZ)
    return dt.strftime("%Y-%m-%d")


def _todays_payments() -> list:
    key = _today_key()
    return [p for p in _pay.get("payments", []) if _today_key(p["ts"]) == key]


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


def render_template(tpl: str, amount: float, name: str) -> str:
    day = _todays_payments()
    today_total = sum(p["amount"] for p in day)
    today_count = len(day)

    base = today_total if config.SHARE_BASE == "today" else amount
    rio = base * config.RIO_PCT / 100.0
    marco = base * config.MARCO_PCT / 100.0

    mapping = {
        "{amount}": fmt_inr(amount),
        "{name}": name or "",
        "{rioshare}": fmt_inr(rio),
        "{marco}": fmt_inr(marco),
        "{total}": str(today_count),
    }
    for key, val in mapping.items():
        tpl = tpl.replace(key, val)
    return tpl


HELP_TEXT = (
    "<b>Payment logger</b>\n"
    "<code>/add &lt;amount&gt; [name]</code> - reply to an image: log the payment and post it\n"
    "<code>/setdone &lt;template&gt;</code> - set the caption (or reply to a post with /setdone)\n"
    "<code>/setpostchannel [id]</code> - set the post channel (blank = current chat)\n"
    "<code>/stats</code> - today's total, count, and split\n"
    "<code>/scan</code> - print your chats/channels in the terminal\n\n"
    "<b>Caption parameters</b> (use inside /setdone)\n"
    "<code>{amount}</code> - this payment, in INR\n"
    "<code>{name}</code> - the name from /add\n"
    "<code>{rioshare}</code> - Rio's share, in INR\n"
    "<code>{marco}</code> - Marco's share, in INR\n"
    "<code>{total}</code> - number of payments today\n\n"
    "<b>Greeting</b> (Saved Messages)\n"
    "<code>/set</code> reply to a post - <code>/unset</code> clear - <code>/show</code> status"
)


async def _print_pay_dialogs() -> None:
    print("\n=== Chats / channels this account is in ===")
    print("%-16s  %s" % ("id", "title"))
    print("-" * 60)
    async for d in client.iter_dialogs():
        if d.is_channel or d.is_group:
            print("%-16s  %s" % (d.id, d.name))
    print("-" * 60)
    print("Use  /setpostchannel <id>  to pick where media is posted.\n")


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
        await event.edit("No post channel set. Use /setpostchannel first.")
        return

    # Record first so today's totals include this payment when rendering.
    _pay["payments"].append({"amount": amount, "name": name, "ts": _now_ts()})
    _save_pay()

    caption = render_template(_pay["done_template"], amount, name)
    parse_mode = None if config.PAY_PARSE_MODE in ("", "none", "plain") else config.PAY_PARSE_MODE
    try:
        await client.send_file(_pay["post_channel"], file=reply.media,
                               caption=caption, parse_mode=parse_mode)
    except Exception as e:  # noqa: BLE001
        await event.edit(f"Recorded, but posting failed: {type(e).__name__}: {e}")
        return

    count = len(_todays_payments())
    await event.edit(f"Recorded {fmt_inr(amount)} - {name or '-'} - #{count} today")
    print(ui.green("[pay] ") + f"{fmt_inr(amount)} {name} -> posted (#{count} today)", flush=True)


async def cmd_setdone(event) -> None:
    reply = await event.get_reply_message()
    if reply and (reply.raw_text or ""):
        template = reply.raw_text
    else:
        m = re.match(r"^/setdone(?:\s+([\s\S]+))?$", event.raw_text or "")
        template = (m.group(1).strip() if m and m.group(1) else "")
    if not template:
        await event.edit("Send the template after /setdone, or reply to a post with /setdone.")
        return
    _pay["done_template"] = template
    _save_pay()
    await event.edit(f"Caption template saved ({len(template)} chars).")


async def cmd_setpostchannel(event) -> None:
    m = re.match(r"^/setpostchannel(?:\s+(.+))?$", event.raw_text or "")
    arg = (m.group(1).strip() if m and m.group(1) else "")
    if arg:
        target = config.coerce(arg)
        try:
            entity = await client.get_entity(target)
        except Exception as e:  # noqa: BLE001
            await event.edit(f"Could not resolve that target: {type(e).__name__}")
            return
        chat_id = entity.id
        title = getattr(entity, "title", None) or getattr(entity, "username", str(chat_id))
    else:
        chat_id = event.chat_id
        chat = await event.get_chat()
        title = getattr(chat, "title", None) or "this chat"
    _pay["post_channel"] = chat_id
    _save_pay()
    await event.edit(f"Post channel set: {title} ({chat_id})")


async def cmd_stats(event) -> None:
    day = _todays_payments()
    total = sum(p["amount"] for p in day)
    rio = total * config.RIO_PCT / 100.0
    marco = total * config.MARCO_PCT / 100.0
    await event.edit(
        f"Today - {fmt_inr(total)} - {len(day)} payments\n"
        f"Rio {fmt_inr(rio)} - Marco {fmt_inr(marco)}"
    )


async def cmd_scan(event) -> None:
    await _print_pay_dialogs()
    await event.edit("Chat list printed in the terminal.")


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


async def _copy_to(peer, src) -> None:
    """Send a copy of `src` (text/media + entities, incl. premium emoji)."""
    text = src.message or ""
    ents = src.entities or None
    media = getattr(src, "media", None)
    if media and not isinstance(media, MessageMediaWebPage):
        await client.send_file(peer, file=media, caption=text, formatting_entities=ents)
    else:
        await client.send_message(peer, text, formatting_entities=ents,
                                  link_preview=isinstance(media, MessageMediaWebPage))


async def send_greeting(event) -> str:
    """Send the greeting to the DM sender.

    Prefers the /set greeting post; falls back to the Business away message.
    Returns 'greeting', 'away', or 'none'.
    """
    peer = await event.get_input_sender()

    g = _load_greeting()
    if g:
        try:
            src = await client.get_messages(g["chat_id"], ids=g["message_id"])
        except Exception:
            src = None
        if src is not None:
            await _copy_to(peer, src)
            return "greeting"

    sid = await _away_shortcut_id()
    if sid:
        msgs = await client(GetQuickReplyMessagesRequest(shortcut_id=sid, hash=0, id=None))
        ids = [m.id for m in getattr(msgs, "messages", []) or []]
        if ids:
            await client(SendQuickReplyMessagesRequest(
                peer=peer, shortcut_id=sid, id=ids,
                random_id=[random.randrange(-(2 ** 63), 2 ** 63) for _ in ids],
            ))
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


async def on_command(event):
    """Outgoing commands. Payment/help commands work in any chat; greeting
    commands (/set, /unset, /show) only in Saved Messages."""
    low = (event.raw_text or "").strip().lower()

    # --- help: every command + parameter ---
    if low in (".help", "/help"):
        await event.edit(HELP_TEXT, parse_mode="html")
        return

    # --- payment logger (work in any chat) ---
    if low.startswith("/add"):
        await cmd_add(event)
        return
    if low.startswith("/setdone"):
        await cmd_setdone(event)
        return
    if low.startswith("/setpostchannel"):
        await cmd_setpostchannel(event)
        return
    if low == "/stats":
        await cmd_stats(event)
        return
    if low == "/scan":
        await cmd_scan(event)
        return

    # --- greeting (Saved Messages only) ---
    if event.chat_id != _state["self_id"]:
        return
    if low == "/set":
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
    elif low == "/show":
        await event.reply("Greeting is set." if _load_greeting()
                          else "No greeting set. Reply to a post with /set.")


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

    # 2) A brand-new DM conversation -> send the greeting (once per user).
    if not config.GREET_NEW:
        return
    if not sender or sender in (_state["self_id"], src) or sender in _greeted:
        return
    if not await _is_new_user(event):
        _greeted.add(sender)          # existing contact -> remember, don't greet
        _save_greeted()
        return
    try:
        status = await send_greeting(event)
        _greeted.add(sender)
        _save_greeted()
        if status == "none":
            ui.warn(f"No greeting set (reply to a post with /set). Skipped {sender}.")
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

    # Incoming DMs -> link update / greet.  Outgoing (Saved Messages) -> commands.
    client.add_event_handler(on_msg, events.NewMessage(incoming=True))
    client.add_event_handler(on_command, events.NewMessage(outgoing=True))

    where = f"from {src_raw}" if _state["source_id"] else "from any private chat"
    ui.banner("Quick-reply updater - running")
    ui.success(f"As {ui.bold(me.first_name)} (id {me.id}).")
    ui.info(f"Keeping /{ui.bold(config.SHORTCUT)} link current ({where}).")
    if config.GREET_NEW:
        has_greeting = _load_greeting() is not None
        state = "set" if has_greeting else ui.yellow("not set - reply to a post with /set")
        ui.info(f"First-time DMs get the greeting post [{state}]. "
                + ui.dim(f"({len(_greeted)} already greeted)"))
    pc = _pay.get("post_channel")
    day = _todays_payments()
    total = sum(p["amount"] for p in day)
    pc_state = str(pc) if pc else ui.yellow("not set - use /setpostchannel")
    ui.info(f"Payment logger: post channel [{pc_state}]. "
            + ui.dim(f"today {fmt_inr(total)} / {len(day)} payment(s). '.help' for commands."))
    print(ui.dim("Ctrl+C to stop."))
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
