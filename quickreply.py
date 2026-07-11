"""Quick-reply updater (second userbot).

Runs on the account that holds the Business quick reply (typically the OWNER).
When it receives a message containing a t.me invite link from LINK_SOURCE (the
guard account), it updates the "/SHORTCUT" quick reply so it contains ONLY that
link — nothing else. So the shortcut always expands to the current invite link.

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
    DeleteQuickReplyShortcutRequest,
    EditMessageRequest,
    GetQuickRepliesRequest,
    SendMessageRequest,
)
from telethon.tl.types import InputPeerSelf, InputQuickReplyShortcut

import config
import ui

# Matches private invite links:  https://t.me/+HASH  or  t.me/joinchat/HASH
LINK_RE = re.compile(r"https?://t\.me/(?:joinchat/|\+)[\w-]+", re.IGNORECASE)

client: TelegramClient | None = None
_state = {"source_id": None, "last": None}


async def set_shortcut(name: str, text: str) -> str:
    """Make the quick reply `name` contain exactly one message: `text`."""
    res = await client(GetQuickRepliesRequest(hash=0))
    shortcuts = getattr(res, "quick_replies", []) or []
    target = next((q for q in shortcuts if getattr(q, "shortcut", None) == name), None)

    # Exactly one message -> edit it in place (no flicker, keeps the shortcut).
    if target is not None and getattr(target, "count", 0) == 1:
        try:
            await client(EditMessageRequest(
                peer=InputPeerSelf(), id=target.top_message, message=text,
                quick_reply_shortcut_id=target.shortcut_id,
            ))
            return "edited"
        except MessageNotModifiedError:
            return "unchanged"

    # Wrong number of messages -> wipe the shortcut so only the link remains.
    if target is not None:
        await client(DeleteQuickReplyShortcutRequest(shortcut_id=target.shortcut_id))

    # (Re)create by sending one message into the shortcut.
    await client(SendMessageRequest(
        peer=InputPeerSelf(), message=text,
        quick_reply_shortcut=InputQuickReplyShortcut(shortcut=name),
        random_id=random.randrange(-(2 ** 63), 2 ** 63),
    ))
    return "created"


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
        status = await set_shortcut(config.SHORTCUT, link)
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
