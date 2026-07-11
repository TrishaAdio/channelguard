#!/usr/bin/env python3
"""Interactive setup — run this first.

Logs in your userbot (phone -> OTP -> 2FA), lets you pick the channel from a
list, asks who the owner is (the account that receives the rotating links),
then writes .env.

Run:  python setup.py
"""
from __future__ import annotations

import asyncio
from getpass import getpass

from telethon import TelegramClient

import config


def ask(prompt: str, default: str | None = None, secret: bool = False) -> str:
    label = prompt + (f" [{default}]" if default else "") + ": "
    while True:
        value = (getpass(label) if secret else input(label)).strip()
        if not value and default is not None:
            return default
        if value:
            return value
        print("  (required)")


async def main() -> None:
    print("=" * 60)
    print(" Channel guard — setup")
    print(" API_ID / API_HASH come from https://my.telegram.org")
    print("=" * 60)

    api_id = ask("API_ID")
    api_hash = ask("API_HASH", secret=True)

    print("\n" + "-" * 60)
    print(" Logging in your userbot account.")
    print(" Telegram sends a login code to your app; enter it when asked.")
    print("-" * 60)
    phone = ask("Phone number (with country code, e.g. +15551234567)")

    client = TelegramClient(config.SESSION, int(api_id), api_hash)
    await client.start(phone=lambda: phone)  # prompts OTP + 2FA itself
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (id {me.id}).")

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
    print(f"\nWrote {config.ENV_PATH}")
    print("\nStart it with:\n  python guard.py")
    print("\nMake sure this account is an ADMIN of the channel with "
          "'Invite via link' + 'Ban users' rights.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSetup cancelled.")
