"""Channel guard userbot.

Every ROTATE_MINUTES it revokes the channel's primary invite link, issues a
fresh one, and DMs it to the owner. Anyone who joins the channel is immediately
kicked (the owner and admins are exempt).

The logged-in account must be an ADMIN of the channel with "Invite users via
link" and "Ban users" rights.

Run:  python guard.py    (Ctrl+C to stop)
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient, events
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import UpdateChannelParticipant

import config

client: TelegramClient | None = None
_state = {"self_id": 0, "channel": None, "channel_id": 0, "owner": None, "owner_id": 0}


async def notify(text: str) -> None:
    try:
        await client.send_message(_state["owner"], text, parse_mode="html",
                                  link_preview=False)
    except Exception as e:  # noqa: BLE001
        print(f"owner DM failed: {type(e).__name__}: {e}")


async def rotate_once() -> None:
    """Revoke the current primary invite link, issue a new one, DM the owner."""
    res = await client(ExportChatInviteRequest(
        _state["channel"], legacy_revoke_permanent=True,
    ))
    link = res.link
    title = getattr(_state["channel"], "title", "channel")
    await notify(f"<b>{title}</b>\n<code>{link}</code>")
    print(f"rotated invite link: {link}")


async def rotate_loop() -> None:
    while True:
        try:
            await rotate_once()
        except Exception as e:  # noqa: BLE001
            print(f"rotate failed: {type(e).__name__}: {e}")
            await notify(f"Link rotation failed: {type(e).__name__}")
        await asyncio.sleep(config.ROTATE_MINUTES * 60)


async def kick(user_id: int) -> None:
    if user_id in (_state["self_id"], _state["owner_id"]):
        return
    try:
        await client.kick_participant(_state["channel"], user_id)
        print(f"kicked {user_id}")
        await notify(f"Kicked <code>{user_id}</code>")
    except Exception as e:  # noqa: BLE001 - e.g. admins can't be kicked
        print(f"kick {user_id} failed: {type(e).__name__}: {e}")


async def on_participant(update):
    if not isinstance(update, UpdateChannelParticipant):
        return
    if update.channel_id != _state["channel_id"]:
        return
    # A join: no previous participant, a new one present.
    if update.prev_participant is None and update.new_participant is not None:
        await kick(update.user_id)


async def main() -> None:
    global client
    config.require("API_ID", "API_HASH")

    client = TelegramClient(config.SESSION, config.API_ID, config.API_HASH)
    await client.start()  # prompts phone + OTP on first run

    me = await client.get_me()
    _state["self_id"] = me.id

    from resolve import choose_channel, resolve_channel

    # --- channel (pick from a list if the configured one isn't usable) ------
    channel = None
    if config.CHANNEL_RAW.strip():
        channel = await resolve_channel(client, config.channel())
        if channel is None:
            print(f"\nCould not use CHANNEL={config.CHANNEL_RAW!r}. Pick one below.")
    while channel is None:
        value = await choose_channel(client)
        channel = await resolve_channel(client, config.coerce(value))
        if channel is None:
            print("  Couldn't access that one — try another.")
            continue
        config.save_env({"CHANNEL": value})
    _state["channel"] = channel
    _state["channel_id"] = channel.id

    # --- owner --------------------------------------------------------------
    owner_val = config.owner()
    if not owner_val:
        raw = input("\nOwner username or user id (receives the links): ").strip()
        config.save_env({"OWNER": raw})
        owner_val = config.coerce(raw)
    try:
        owner = await client.get_entity(owner_val)
    except Exception as e:  # noqa: BLE001
        print(f"\nCouldn't resolve owner {owner_val!r}: {type(e).__name__}.")
        print("Use a @username, or make sure the account has a chat with the "
              "userbot (or is in the channel).")
        await client.disconnect()
        return
    _state["owner"] = owner
    _state["owner_id"] = owner.id

    # --- run ----------------------------------------------------------------
    client.add_event_handler(on_participant, events.Raw(UpdateChannelParticipant))
    asyncio.create_task(rotate_loop())

    title = getattr(channel, "title", channel)
    print(f"Guarding: {title}")
    print(f"Owner: {getattr(owner, 'first_name', owner.id)} (id {owner.id})")
    print(f"Rotating invite link every {config.ROTATE_MINUTES:g} min; "
          "joiners are kicked. Ctrl+C to stop.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
