#!/usr/bin/env python3
"""Purger userbot.

Reply to any message with ``.purge`` and that message, plus everything sent
after it in the same chat (the ``.purge`` message included), is deleted.

Deletes for everyone where Telegram allows it:
  * private chats   - removed for both sides
  * groups/channels - every message if you are an admin with "Delete
                      messages"; otherwise only the messages you sent

Only a message YOU send can trigger it, and only when it replies to another
message, so it can never fire by accident.

Run:  python purger.py
      First run asks for your phone + login code (and 2FA password if set).
      It uses API_ID / API_HASH from .env and its own purger.session; it
      never touches the guard or quick-reply sessions.
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

import config
import ui
from runtime_lock import ProcessLock, host_lock_path

# Delete in chunks; Telegram accepts up to 100 message ids per request.
BATCH = 100
# A plain, unforwarded ".purge" (or "/purge") reply - nothing else triggers it.
TRIGGERS = {".purge", "/purge"}

client: TelegramClient | None = None
_self_id = 0
_busy = asyncio.Lock()


async def _collect_ids(chat_id: int, start_id: int) -> list[int]:
    """Every message id in ``chat_id`` from ``start_id`` onward (inclusive)."""
    ids = []
    # min_id is exclusive, so start_id - 1 keeps the replied-to message itself.
    async for message in client.iter_messages(chat_id, min_id=start_id - 1):
        if message.id >= start_id:
            ids.append(message.id)
    return sorted(ids)   # oldest first, deterministic batches


async def _delete(chat_id: int, ids: list[int]) -> int:
    """Delete ``ids`` in batches (for everyone). Returns how many were sent."""
    deleted = 0
    for offset in range(0, len(ids), BATCH):
        chunk = ids[offset:offset + BATCH]
        while True:
            try:
                await client.delete_messages(chat_id, chunk, revoke=True)
                deleted += len(chunk)
                break
            except FloodWaitError as error:
                wait = min(int(error.seconds), 60) + 1
                ui.warn(f"Flood wait: pausing {wait}s, then continuing.")
                await asyncio.sleep(wait)
            except Exception as error:  # noqa: BLE001
                ui.error(f"Batch failed ({type(error).__name__}: {error}); skipping it.")
                break
    return deleted


async def _notify(chat_id: int, text: str) -> None:
    """Confirm in Saved Messages (the purged chat has nothing left to edit)."""
    try:
        await client.send_message("me", text)
    except Exception:  # noqa: BLE001
        pass


async def on_purge(event) -> None:
    reply = await event.get_reply_message()
    if reply is None:
        try:
            await event.edit("Reply to a message with .purge - it and everything "
                             "after it in this chat is deleted.")
        except Exception:  # noqa: BLE001
            pass
        return

    if _busy.locked():
        try:
            await event.edit("A purge is already running; wait for it to finish.")
        except Exception:  # noqa: BLE001
            pass
        return

    async with _busy:
        chat_id = event.chat_id
        start_id = reply.id
        try:
            ids = await _collect_ids(chat_id, start_id)
        except Exception as error:  # noqa: BLE001
            try:
                await event.edit(f"Couldn't read the chat: {type(error).__name__}: {error}")
            except Exception:  # noqa: BLE001
                pass
            return

        print(ui.yellow("[purge] ")
              + f"chat {chat_id}: deleting {len(ids)} message(s) from id {start_id}", flush=True)
        deleted = await _delete(chat_id, ids)
        await _notify(
            chat_id,
            f"Purged chat {chat_id}: deleted {deleted} message(s) from message "
            f"{start_id} onward (for everyone where allowed).",
        )
        print(ui.green("[purge] ") + f"chat {chat_id}: done ({deleted} message(s))", flush=True)


async def main() -> None:
    global client, _self_id
    config.require("API_ID", "API_HASH")

    lock = ProcessLock(host_lock_path("purger"))
    if not lock.acquire():
        ui.error("Another purger.py is already running on this machine. Stop it first.")
        raise SystemExit(1)

    session = str(config.BASE_DIR / "purger")
    client = TelegramClient(session, config.API_ID, config.API_HASH)
    await client.start()  # prompts phone + OTP (+ 2FA) on first run
    me = await client.get_me()
    _self_id = me.id

    # Only outgoing (messages this account sends) can trigger a purge.
    client.add_event_handler(
        on_purge,
        events.NewMessage(outgoing=True, func=_is_purge_command),
    )

    ui.banner("Purger - running")
    ui.success(f"As {ui.bold(me.first_name)} (id {me.id}).")
    ui.info("Reply to any message with " + ui.bold(".purge")
            + " - that message and everything after it is wiped.")
    ui.warn("This cannot be undone. The .purge message is deleted too.")
    print(ui.dim("Ctrl+C to stop."))
    try:
        await client.run_until_disconnected()
    finally:
        lock.close()


def _is_purge_command(event) -> bool:
    text = (getattr(event, "raw_text", "") or "").strip().lower()
    message = getattr(event, "message", event)
    if getattr(message, "fwd_from", None) is not None:
        return False
    return text in TRIGGERS


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
