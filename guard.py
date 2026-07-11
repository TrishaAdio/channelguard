"""Channel guard userbot.

Every ROTATE_MINUTES it revokes the channel's primary invite link, issues a
fresh one, and DMs it to the owner. Anyone who joins the channel is immediately
kicked AND unbanned (removed but free to rejoin — never a lasting ban); the
owner and admins are exempt. On startup it also clears every existing ban.

The logged-in account must be an ADMIN of the channel with "Invite users via
link" and "Ban users" rights.

Run:  python guard.py    (Ctrl+C to stop)
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient, events
from telethon.tl.functions.channels import EditBannedRequest, GetParticipantsRequest
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import (
    ChannelParticipantsKicked,
    ChatBannedRights,
    UpdateChannelParticipant,
)

import config
import ui

# Remove-from-channel rights (ban), and cleared rights (unban).
_KICK_RIGHTS = ChatBannedRights(until_date=None, view_messages=True)
_UNBAN_RIGHTS = ChatBannedRights(until_date=None)

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
    print(ui.green("[link] ") + f"rotated -> {ui.bold(link)}")


async def rotate_loop() -> None:
    while True:
        try:
            await rotate_once()
        except Exception as e:  # noqa: BLE001
            print(f"rotate failed: {type(e).__name__}: {e}")
            await notify(f"Link rotation failed: {type(e).__name__}")
        await asyncio.sleep(config.ROTATE_MINUTES * 60)


async def kick(user_id: int) -> None:
    """Remove the user, then immediately lift the ban so it's a kick (they can
    rejoin) — never a lasting ban."""
    if user_id in (_state["self_id"], _state["owner_id"]):
        return
    channel = _state["channel"]
    try:
        await client(EditBannedRequest(channel, user_id, _KICK_RIGHTS))   # remove
        await asyncio.sleep(0.4)
        await client(EditBannedRequest(channel, user_id, _UNBAN_RIGHTS))  # unban
        print(ui.yellow("[kick] ") + f"removed + unbanned {user_id}")
        await notify(f"Kicked <code>{user_id}</code>")
    except Exception as e:  # noqa: BLE001 - e.g. admins can't be kicked
        print(ui.red("[kick] ") + f"{user_id} failed: {type(e).__name__}")


async def unban_all() -> int:
    """Clear every existing ban in the channel (so nobody stays banned)."""
    channel = _state["channel"]
    cleared = 0
    offset = 0
    while True:
        try:
            res = await client(GetParticipantsRequest(
                channel, ChannelParticipantsKicked(q=""), offset, 100, hash=0,
            ))
        except Exception as e:  # noqa: BLE001
            print(ui.red("[unban] ") + f"list failed: {type(e).__name__}")
            break
        if not res.participants:
            break
        for p in res.participants:
            peer = getattr(p, "peer", None)
            if peer is None:
                continue
            try:
                await client(EditBannedRequest(channel, peer, _UNBAN_RIGHTS))
                cleared += 1
                await asyncio.sleep(0.2)
            except Exception:
                pass
        offset += len(res.participants)
        if len(res.participants) < 100:
            break
    return cleared


def _can_ban(channel) -> bool:
    """Whether this account can remove users (creator, or admin with ban_users)."""
    if getattr(channel, "creator", False):
        return True
    ar = getattr(channel, "admin_rights", None)
    return bool(ar and getattr(ar, "ban_users", False))


async def on_participant(update):
    if not isinstance(update, UpdateChannelParticipant):
        return
    if _state["channel_id"] and update.channel_id != _state["channel_id"]:
        return
    joined = update.prev_participant is None and update.new_participant is not None
    print(ui.dim(
        f"[participant] user={update.user_id} "
        f"prev={type(update.prev_participant).__name__ if update.prev_participant else None} "
        f"new={type(update.new_participant).__name__ if update.new_participant else None}"
        f" -> {'JOIN' if joined else 'change'}"
    ))
    if joined:
        await kick(update.user_id)


async def on_chat_action(event):
    """Backup path (fires for supergroups; broadcast channels use the raw update)."""
    if not (event.user_joined or event.user_added):
        return
    for uid in (event.user_ids or ([event.user_id] if event.user_id else [])):
        await kick(uid)


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
            ui.warn(f"Could not use CHANNEL={config.CHANNEL_RAW!r}. Pick one below.")
    while channel is None:
        value = await choose_channel(client)
        channel = await resolve_channel(client, config.coerce(value))
        if channel is None:
            ui.warn("Couldn't access that one — try another.")
            continue
        config.save_env({"CHANNEL": value})
    _state["channel"] = channel
    _state["channel_id"] = channel.id

    if not _can_ban(channel):
        ui.warn("This account is NOT an admin with 'Ban users' rights on that "
                "channel — auto-kick WON'T work (Telegram won't send join "
                "updates and kicks are rejected). Make it an admin with the "
                "'Ban users' right, then restart.")

    # --- owner --------------------------------------------------------------
    owner_val = config.owner()
    if not owner_val:
        raw = input("\nOwner username or user id (receives the links): ").strip()
        config.save_env({"OWNER": raw})
        owner_val = config.coerce(raw)
    try:
        owner = await client.get_entity(owner_val)
    except Exception as e:  # noqa: BLE001
        ui.error(f"Couldn't resolve owner {owner_val!r}: {type(e).__name__}.")
        ui.info("Use a @username, or make sure the account has a chat with the "
                "userbot (or is in the channel).")
        await client.disconnect()
        return
    _state["owner"] = owner
    _state["owner_id"] = owner.id

    # --- clear any existing bans up front -----------------------------------
    cleared = await unban_all()
    if cleared:
        ui.success(f"Unbanned {cleared} previously-banned user(s).")

    # --- run ----------------------------------------------------------------
    # Raw participant updates catch broadcast-channel joins; ChatAction is a
    # backup for supergroups. Listen to ALL raw updates and filter in-handler
    # (a type-filtered Raw can miss updates on some layers).
    client.add_event_handler(on_participant, events.Raw)
    client.add_event_handler(on_chat_action, events.ChatAction(chats=channel))
    asyncio.create_task(rotate_loop())

    title = getattr(channel, "title", channel)
    ui.banner("Channel guard - running")
    ui.success(f"Guarding: {ui.bold(str(title))}")
    ui.info(f"Owner: {getattr(owner, 'first_name', owner.id)} (id {owner.id})")
    ui.info(f"Rotating invite link every {ui.bold(f'{config.ROTATE_MINUTES:g}')} min; "
            "joiners are kicked + unbanned (no lasting ban). "
            + ui.dim("Ctrl+C to stop."))
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
