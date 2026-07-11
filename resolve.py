"""Resolve/pick the channel to guard.

Never raises — resolution failures return None so the caller can fall back to
the interactive picker.
"""
from __future__ import annotations

import re

from telethon import TelegramClient, utils
from telethon.errors import (
    InviteHashExpiredError,
    InviteHashInvalidError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    ImportChatInviteRequest,
)


def _invite_hash(text: str):
    m = re.search(r"(?:t\.me/|telegram\.me/)(?:joinchat/|\+)([\w-]+)", text)
    return m.group(1) if m else None


def _username(text: str):
    t = text.strip()
    if t.startswith("@"):
        return t[1:]
    m = re.search(r"(?:t\.me/|telegram\.me/)([A-Za-z]\w{3,})/?$", t)
    return m.group(1) if m else None


async def _try_join(client: TelegramClient, ident):
    if not isinstance(ident, str):
        return None
    h = _invite_hash(ident)
    if h:
        try:
            upd = await client(ImportChatInviteRequest(h))
            print("Joined the channel via invite link.")
            return upd.chats[0]
        except UserAlreadyParticipantError:
            try:
                inv = await client(CheckChatInviteRequest(h))
                return getattr(inv, "chat", None)
            except Exception:
                return None
        except (InviteHashExpiredError, InviteHashInvalidError):
            print("That invite link is expired or invalid — pick a channel below.")
            return None
        except Exception as e:
            print(f"Could not join via invite link: {type(e).__name__}")
            return None

    uname = _username(ident)
    if uname:
        try:
            ent = await client.get_entity(uname)
            try:
                await client(JoinChannelRequest(ent))
            except Exception:
                pass
            return ent
        except Exception:
            return None
    return None


async def resolve_channel(client: TelegramClient, ident):
    """Return the channel entity, or None if the account can't access it."""
    if isinstance(ident, str):
        try:
            joined = await _try_join(client, ident)
        except Exception:
            joined = None
        if joined is not None:
            return joined

    try:
        return await client.get_entity(ident)
    except Exception:
        pass

    try:
        print("Channel not cached — scanning your chats...")
        async for dialog in client.iter_dialogs():
            ent = dialog.entity
            if dialog.id == ident:
                return ent
            try:
                if ent is not None and utils.get_peer_id(ent) == ident:
                    return ent
            except Exception:
                pass
    except Exception:
        pass
    return None


async def list_channels(client: TelegramClient) -> list[dict]:
    """Broadcast channels / supergroups this account is in, with an admin hint."""
    out = []
    async for dialog in client.iter_dialogs():
        if not dialog.is_channel:
            continue
        e = dialog.entity
        creator = bool(getattr(e, "creator", False))
        ar = getattr(e, "admin_rights", None)
        can_manage = creator or (ar is not None and (
            getattr(ar, "invite_users", False) or getattr(ar, "ban_users", False)))
        out.append({
            "id": dialog.id,
            "title": dialog.name or "(no title)",
            "broadcast": bool(getattr(e, "broadcast", False)),
            "can_manage": can_manage,
        })
    return out


async def choose_channel(client: TelegramClient) -> str:
    """Interactive terminal picker. Returns the chosen CHANNEL value."""
    import ui

    ui.info("Loading your channels...")
    chans = await list_channels(client)
    if chans:
        print(ui.bold("\nChannels this account is in:"))
        for i, c in enumerate(chans, 1):
            kind = "channel" if c["broadcast"] else "group"
            adm = ui.green("admin") if c["can_manage"] else ui.red("NOT admin")
            print(f"  {ui.cyan(f'{i:>2}')}. {ui.bold(c['title'])}   "
                  f"[{kind}, {adm}]   {ui.dim('id=' + str(c['id']))}")
    else:
        ui.warn("This account isn't in any channels yet.")
    print(ui.dim("  (or paste a @username or an invite link)"))

    while True:
        raw = input(ui.cyan("> ") + "Pick a number, or paste @username / link / id: ").strip()
        if not raw:
            continue
        if raw.isdigit() and chans:
            idx = int(raw)
            if 1 <= idx <= len(chans):
                chosen = chans[idx - 1]
                if not chosen["can_manage"]:
                    ui.warn("This account isn't an admin there — it needs "
                            "'Invite via link' + 'Ban users' rights to guard it.")
                return str(chosen["id"])
            ui.warn("out of range")
            continue
        return raw
