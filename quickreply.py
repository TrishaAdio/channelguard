"""Quick-reply updater (second userbot).

Runs on the account that holds the Business quick reply (typically the OWNER).
When it receives a message containing a t.me invite link from LINK_SOURCE (the
guard account), it swaps ONLY the invite link inside your "/SHORTCUT" post and
leaves everything else untouched — your text, markdown formatting, and premium
(custom) emoji all stay exactly as they were.

Business quick replies require Telegram Premium.

Run:  python quickreply.py    (Ctrl+C to stop)
"""
from __future__ import annotations

import asyncio
import random
import re

from telethon import TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.tl.functions.messages import (
    EditMessageRequest,
    GetQuickRepliesRequest,
    GetQuickReplyMessagesRequest,
    SendMessageRequest,
)
from telethon.tl.types import (
    InputPeerSelf,
    InputQuickReplyShortcut,
    MessageMediaWebPage,
)

import config
import ui

# The link the guard sends us (full https invite link).
LINK_RE = re.compile(r"https?://t\.me/(?:joinchat/|\+)[\w-]+", re.IGNORECASE)
# The link inside the saved post (may or may not include the scheme).
FIND_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)[\w-]+", re.IGNORECASE)

client: TelegramClient | None = None
_state = {"source_id": None, "last": None}


def _u16(s: str) -> int:
    """Length in UTF-16 code units (how Telegram counts entity offsets)."""
    return len(s.encode("utf-16-le")) // 2


def _swap_link(text: str, entities, new_link: str):
    """Replace the first invite link in `text` with `new_link`, shifting all
    entity offsets/lengths so formatting + custom emoji stay aligned.

    Returns (new_text, new_entities) or None if there's nothing to change.
    """
    m = FIND_LINK_RE.search(text or "")
    if not m:
        return None
    old = m.group(0)
    if old == new_link:
        return None

    pi = m.start()
    o = _u16(text[:pi])          # utf-16 offset where the link starts
    old_len = _u16(old)
    new_len = _u16(new_link)
    delta = new_len - old_len
    r_start, r_end = o, o + old_len

    new_text = text[:pi] + new_link + text[pi + len(old):]

    new_entities = []
    for e in (entities or []):
        start, end = e.offset, e.offset + e.length
        if end <= r_start:
            pass                                  # entirely before the link
        elif start >= r_end:
            e.offset = e.offset + delta           # entirely after -> shift
        else:
            e.length = max(0, e.length + delta)   # covers/equals the link -> resize
        new_entities.append(e)
    return new_text, new_entities


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

    msgs = await client(GetQuickReplyMessagesRequest(
        shortcut_id=target.shortcut_id, hash=0, id=None,
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
                quick_reply_shortcut_id=target.shortcut_id,
                no_webpage=not had_preview,
            ))
            updated += 1
        except MessageNotModifiedError:
            pass
    return f"updated link in {updated} message(s)" if updated else "no link in the post"


async def on_msg(event):
    if not event.is_private:
        return
    if _state["source_id"] is not None and event.sender_id != _state["source_id"]:
        return
    match = LINK_RE.search(event.raw_text or "")
    if not match:
        return
    link = match.group(0)
    if link == _state["last"]:
        return
    try:
        status = await update_link(config.SHORTCUT, link)
        _state["last"] = link
        print(ui.green(f"[/{config.SHORTCUT}] ") + f"{ui.bold(link)} ({status})", flush=True)
    except Exception as e:  # noqa: BLE001
        ui.error(f"quick-reply update failed: {type(e).__name__}: {e}")
        if "PREMIUM" in str(e).upper():
            ui.warn("Business quick replies require Telegram Premium.")


async def main() -> None:
    global client
    config.require("API_ID", "API_HASH")

    client = TelegramClient(config.QR_SESSION, config.API_ID, config.API_HASH)
    await client.start()  # prompts phone + OTP for THIS account on first run
    me = await client.get_me()

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

    client.add_event_handler(on_msg, events.NewMessage)
    where = f"from {src_raw}" if _state["source_id"] else "from any private chat"
    ui.banner("Quick-reply updater - running")
    ui.success(f"As {ui.bold(me.first_name)} (id {me.id}).")
    ui.info(f"Keeping /{ui.bold(config.SHORTCUT)} = the latest invite link ({where}). "
            + ui.dim("Ctrl+C to stop."))
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
