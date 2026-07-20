"""Promote a bot to admin in every group/channel this account manages.

Logs in a NEW account (it reuses API_ID / API_HASH from your .env — you are NOT
asked for them again; you only enter the phone + login code for the new
account), asks for a bot @username, then in every group/channel where THIS
account is admin or creator it adds the bot and grants it FULL admin rights.

Run:  python promote.py        (Ctrl+C to stop)
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient, functions
from telethon.errors import FloodWaitError, RPCError, UserAlreadyParticipantError
from telethon.tl.types import Channel, Chat

import config
import ui

# A separate session so this login never clobbers the guard/quickreply ones.
PROMOTE_SESSION = str(config.BASE_DIR / "promote")

# Full admin rights to grant the bot. post/edit_messages only matter for
# broadcast channels; Telegram ignores them for groups.
FULL_RIGHTS = dict(
    change_info=True,
    post_messages=True,
    edit_messages=True,
    delete_messages=True,
    ban_users=True,
    invite_users=True,
    pin_messages=True,
    add_admins=True,
    manage_call=True,
    anonymous=False,
)


async def _admin_dialogs(client) -> list:
    """Every group/channel where THIS account is admin or creator."""
    out = []
    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        if not isinstance(ent, (Channel, Chat)):
            continue  # skip users/bots
        if getattr(ent, "creator", False) or getattr(ent, "admin_rights", None):
            out.append(ent)
    return out


async def _try_add(client, ent, bot) -> None:
    """Best-effort add the bot to the chat. If it's already in (or can't be
    added), promotion below will surface the real status."""
    try:
        if isinstance(ent, Channel):
            await client(functions.channels.InviteToChannelRequest(ent, [bot]))
        else:
            await client(functions.messages.AddChatUserRequest(ent.id, bot, fwd_limit=0))
    except UserAlreadyParticipantError:
        pass
    except (RPCError, Exception):  # noqa: BLE001
        pass


async def _promote(client, ent, bot) -> None:
    await client.edit_admin(ent, bot, title="admin", **FULL_RIGHTS)


async def promote_everywhere(client, bot) -> None:
    dialogs = await _admin_dialogs(client)
    if not dialogs:
        ui.warn("You are not admin/creator in any group or channel.")
        return
    ui.info(f"Found {ui.bold(str(len(dialogs)))} chat(s) you manage. Promoting the bot...")

    ok = failed = 0
    for ent in dialogs:
        title = getattr(ent, "title", None) or str(getattr(ent, "id", "?"))
        try:
            await _try_add(client, ent, bot)
            await _promote(client, ent, bot)
            ui.success(f"{ui.bold(title)} " + ui.dim("- full admin rights"))
            ok += 1
        except FloodWaitError as e:
            ui.warn(f"{title} - rate limited, waiting {e.seconds}s")
            await asyncio.sleep(int(e.seconds) + 1)
            try:
                await _promote(client, ent, bot)
                ui.success(f"{ui.bold(title)} " + ui.dim("- full admin rights (after wait)"))
                ok += 1
            except Exception as e2:  # noqa: BLE001
                ui.error(f"{title} - {type(e2).__name__}: {e2}")
                failed += 1
        except RPCError as e:
            ui.error(f"{title} - {type(e).__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            ui.error(f"{title} - {type(e).__name__}: {e}")
            failed += 1
        await asyncio.sleep(0.4)  # be gentle with the API

    ui.rule(f"Done - {ui.green(str(ok) + ' promoted')}, {ui.red(str(failed) + ' failed')}")


async def main() -> None:
    ui.banner("Promote bot to admin everywhere")
    config.require("API_ID", "API_HASH")

    client = TelegramClient(PROMOTE_SESSION, config.API_ID, config.API_HASH)
    # Prompts phone + login code for THIS (new) account on first run.
    await client.start()
    me = await client.get_me()
    ui.success(f"Logged in as {ui.bold(me.first_name or str(me.id))} (id {me.id}).")

    raw = ui.ask("Bot @username to promote")
    try:
        bot = await client.get_entity(raw)
    except Exception as e:  # noqa: BLE001
        ui.error(f"Couldn't resolve '{raw}': {type(e).__name__}: {e}")
        await client.disconnect()
        return
    if not getattr(bot, "bot", False):
        ui.warn(f"'{raw}' doesn't look like a bot account - continuing anyway.")
    handle = "@" + bot.username if getattr(bot, "username", None) else str(bot.id)
    ui.info(f"Target bot: {ui.bold(handle)} (id {bot.id})")

    await promote_everywhere(client, bot)
    await client.disconnect()
    ui.info("Disconnected.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
