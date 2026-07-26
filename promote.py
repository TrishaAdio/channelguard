"""Promote a bot to admin in every group/channel this account manages.

Standalone: no local `config` / `ui` modules needed - drop this single file
anywhere Telethon is installed and run it.

Reuses the existing heartvault.session next to this file (no phone/code prompt
if that session is still valid). API_ID / API_HASH come from the environment,
then a .env file next to this script, then any sibling .py script, then an
interactive prompt (saved to .env so you're only asked once).

Run:  python3 promote.py        (Ctrl+C to stop)
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

from telethon import TelegramClient, functions
from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.tl.types import Channel, Chat

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

# Reuse the account you already logged in with. Override with PROMOTE_SESSION.
SESSION_NAME = os.getenv("PROMOTE_SESSION") or "heartvault"
SESSION = str(BASE_DIR / SESSION_NAME)

# ----------------------------------------------------------------- tiny ui ---
_TTY = sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


def bold(s):    return _c("1", s)
def dim(s):     return _c("2", s)
def green(s):   return _c("32", s)
def red(s):     return _c("31", s)
def yellow(s):  return _c("33", s)
def cyan(s):    return _c("36", s)


def banner(text: str) -> None:
    line = "=" * max(len(text) + 4, 40)
    print(f"\n{cyan(line)}\n  {bold(text)}\n{cyan(line)}\n")


def rule(text: str) -> None:
    print(f"\n{dim('-' * 40)}\n{text}\n")


def info(msg):    print(f"{cyan('*')} {msg}")
def success(msg): print(f"{green('+')} {msg}")
def warn(msg):    print(f"{yellow('!')} {msg}")
def error(msg):   print(f"{red('x')} {msg}")


def ask(prompt: str) -> str:
    return input(f"{cyan('?')} {prompt}: ").strip()


# ---------------------------------------------------------------- env/creds ---
def load_env(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE, # comments, optional quotes)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def scavenge_creds() -> tuple[str | None, str | None]:
    """Last resort: pull API_ID / API_HASH out of any sibling .py script."""
    api_id = api_hash = None
    for py in sorted(BASE_DIR.glob("*.py")):
        if py.name == Path(__file__).name:
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if api_id is None:
            m = re.search(r"""API_ID\s*=\s*['"]?(\d{5,})['"]?""", text)
            if m:
                api_id = m.group(1)
        if api_hash is None:
            m = re.search(r"""API_HASH\s*=\s*['"]([0-9a-fA-F]{32})['"]""", text)
            if m:
                api_hash = m.group(1)
        if api_id and api_hash:
            break
    return api_id, api_hash


def get_credentials() -> tuple[int, str]:
    load_env(ENV_FILE)
    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")

    if not (api_id and api_hash):
        found_id, found_hash = scavenge_creds()
        api_id = api_id or found_id
        api_hash = api_hash or found_hash

    asked = False
    while not api_id or not api_id.isdigit():
        api_id = ask("API_ID (from my.telegram.org)")
        asked = True
    while not api_hash or len(api_hash) != 32:
        api_hash = ask("API_HASH")
        asked = True

    if asked:
        try:
            with ENV_FILE.open("a", encoding="utf-8") as fh:
                fh.write(f"\nAPI_ID={api_id}\nAPI_HASH={api_hash}\n")
            info(f"Saved credentials to {dim(str(ENV_FILE))}")
        except OSError as e:
            warn(f"Couldn't write .env: {e}")

    return int(api_id), api_hash


# ------------------------------------------------------------------- rights ---
# post/edit_messages only matter for broadcast channels; ignored for groups.
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
    except Exception:  # noqa: BLE001
        pass


async def _promote(client, ent, bot) -> None:
    await client.edit_admin(ent, bot, title="admin", **FULL_RIGHTS)


async def promote_everywhere(client, bot) -> None:
    dialogs = await _admin_dialogs(client)
    if not dialogs:
        warn("You are not admin/creator in any group or channel.")
        return
    info(f"Found {bold(str(len(dialogs)))} chat(s) you manage. Promoting the bot...")

    ok = failed = 0
    for ent in dialogs:
        title = getattr(ent, "title", None) or str(getattr(ent, "id", "?"))
        try:
            await _try_add(client, ent, bot)
            await _promote(client, ent, bot)
            success(f"{bold(title)} " + dim("- full admin rights"))
            ok += 1
        except FloodWaitError as e:
            warn(f"{title} - rate limited, waiting {e.seconds}s")
            await asyncio.sleep(int(e.seconds) + 1)
            try:
                await _promote(client, ent, bot)
                success(f"{bold(title)} " + dim("- full admin rights (after wait)"))
                ok += 1
            except Exception as e2:  # noqa: BLE001
                error(f"{title} - {type(e2).__name__}: {e2}")
                failed += 1
        except Exception as e:  # noqa: BLE001
            error(f"{title} - {type(e).__name__}: {e}")
            failed += 1
        await asyncio.sleep(0.4)  # be gentle with the API

    rule(f"Done - {green(str(ok) + ' promoted')}, {red(str(failed) + ' failed')}")


async def main() -> None:
    banner("Promote bot to admin everywhere")

    if not Path(SESSION + ".session").is_file():
        warn(f"{SESSION}.session not found - you'll be asked to log in.")

    api_id, api_hash = get_credentials()

    client = TelegramClient(SESSION, api_id, api_hash)
    await client.start()  # silent if the session is still authorized
    me = await client.get_me()
    success(f"Logged in as {bold(me.first_name or str(me.id))} (id {me.id}).")

    raw = ask("Bot @username to promote")
    try:
        bot = await client.get_entity(raw)
    except Exception as e:  # noqa: BLE001
        error(f"Couldn't resolve '{raw}': {type(e).__name__}: {e}")
        await client.disconnect()
        return
    if not getattr(bot, "bot", False):
        warn(f"'{raw}' doesn't look like a bot account - continuing anyway.")
    handle = "@" + bot.username if getattr(bot, "username", None) else str(bot.id)
    info(f"Target bot: {bold(handle)} (id {bot.id})")

    await promote_everywhere(client, bot)
    await client.disconnect()
    info("Disconnected.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
