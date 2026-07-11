#!/usr/bin/env python3
"""Interactive setup — run this first.

Logs in your userbot (phone -> OTP -> 2FA), lets you pick the channel from a
list, asks who the owner is (the account that receives the rotating links),
then writes .env.

Run:  python setup.py
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient

import config
import ui
from ui import ask


async def main() -> None:
    ui.banner("Channel guard - setup")
    ui.info("API_ID / API_HASH come from " + ui.cyan("https://my.telegram.org"))
    print()

    api_id = ask("API_ID")
    api_hash = ask("API_HASH", secret=True)

    print()
    ui.rule("Logging in your userbot account")
    ui.info("Telegram sends a login code to your app; enter it when asked.")
    phone = ask("Phone number (with country code, e.g. +15551234567)")

    client = TelegramClient(config.SESSION, int(api_id), api_hash)
    await client.start(phone=lambda: phone)  # prompts OTP + 2FA itself
    me = await client.get_me()
    ui.success(f"Logged in as {ui.bold(me.first_name)} (id {me.id}).")

    # Choose the channel to guard (must be logged in first).
    from resolve import choose_channel
    channel = await choose_channel(client)

    # Owner who receives the rotating invite links.
    owner = ask("\nOwner username or user id (receives the links)")

    rotate = ask("Rotate every N minutes", default="5")

    await client.disconnect()

    config.save_env({
        "API_ID": api_id,
        "API_HASH": api_hash,
        "CHANNEL": channel,
        "OWNER": owner,
        "ROTATE_MINUTES": rotate,
    })
    ui.success(f"Wrote {config.ENV_PATH}")
    print()
    ui.info("Start it with:  " + ui.bold("python guard.py"))
    ui.warn("This account must be an ADMIN of the channel with "
            "'Invite via link' + 'Ban users' rights.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSetup cancelled.")
