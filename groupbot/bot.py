"""Group-logger bot.

What it does
-----------
* When the bot is added to (or promoted in) a group/channel it logs the event to
  the OWNER with the group's short form, and — if it is an admin — creates an
  invite link that requires admin approval and sends it to the owner.
* Owner DM commands (owner only):
    /groups            list every group where the bot is an admin
    /link <name>       fetch approval invite links for admin groups matching a
                       name (e.g. "Lom"); each is revoked once someone joins
    /add <amount> <account> <keyword>
                       approve the single pending join request matching <keyword>
                       (only that user's request), record the order, and reply
                       with ADD_TEMPLATE (supports {link}, {amount}, ...)
    /remove <username|id>
                       decline the user's pending requests and kick them from
                       every admin group
* Service messages ("X joined / X left") are deleted in groups (clean service).

NOTE — assumptions on the fuzzy parts of the spec are documented in README.md
under "Assumptions"; they are easy to tweak.
"""
from __future__ import annotations

import asyncio
import html
import logging
import random

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import ChatJoinRequest, ChatMemberUpdated, Message

import config
import db
from shortform import short_form

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("grouplogger")

router = Router()

_ADMIN_STATUSES = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
_PRESENT_STATUSES = {
    ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR, ChatMemberStatus.RESTRICTED,
}
_GONE_STATUSES = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
_ORDER_ALPHABET = "ACDEFGHJKLMNPQRSTUVWXYZ23456789"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _order_id() -> str:
    return config.ORDER_PREFIX + "".join(
        random.choices(_ORDER_ALPHABET, k=config.ORDER_ID_LENGTH)
    )


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _is_owner_dm(message: Message) -> bool:
    return (
        message.chat.type == ChatType.PRIVATE
        and message.from_user is not None
        and message.from_user.id == config.OWNER_ID
    )


async def notify_owner(bot: Bot, text: str) -> Message | None:
    try:
        return await bot.send_message(config.OWNER_ID, text, disable_web_page_preview=True)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not message owner: %s: %s", type(e).__name__, e)
        return None


async def ensure_approval_link(bot: Bot, chat_id: int, name: str = "owner") -> str | None:
    """Create an invite link that requires admin approval. None on failure."""
    try:
        link = await bot.create_chat_invite_link(
            chat_id, name=name[:32], creates_join_request=True
        )
        return link.invite_link
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.info("invite link failed for %s: %s", chat_id, e.message)
        return None


# --------------------------------------------------------------------------
# bot added / promoted / removed
# --------------------------------------------------------------------------
@router.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated, bot: Bot) -> None:
    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        return

    status = event.new_chat_member.status
    title = chat.title or ""
    sf = short_form(title, config.SHORT_FORM_WORDS)

    if status in _GONE_STATUSES:
        await db.set_group_admin(chat.id, False)
        await notify_owner(
            bot,
            f"<b>Removed from a {chat.type}</b>\n"
            f"<blockquote>{_esc(title)} ({_esc(sf)})\nID: <code>{chat.id}</code></blockquote>",
        )
        return

    is_admin = status in _ADMIN_STATUSES
    await db.upsert_group(chat.id, title, sf, chat.username, is_admin)

    invite = None
    if is_admin:
        invite = await ensure_approval_link(bot, chat.id)
        await db.set_group_link(chat.id, invite)

    lines = [
        "<b>Promoted to admin</b>" if is_admin else "<b>Added to a group</b>",
        f"<blockquote>{_esc(title)} ({_esc(sf)})\n"
        f"Type: {chat.type}\n"
        f"ID: <code>{chat.id}</code>\n"
        f"Admin: {'yes' if is_admin else 'no'}</blockquote>",
    ]
    if invite:
        lines.append(f"Approval invite link: {invite}")
    elif is_admin:
        lines.append("Could not create an invite link (check 'Invite via link' right).")
    await notify_owner(bot, "\n".join(lines))


# --------------------------------------------------------------------------
# a user requests to join (approval link)
# --------------------------------------------------------------------------
@router.chat_join_request()
async def on_join_request(req: ChatJoinRequest, bot: Bot) -> None:
    user = req.from_user
    chat = req.chat

    # Auto-decline anyone the owner removed.
    if await db.is_removed(user.id):
        try:
            await bot.decline_chat_join_request(chat.id, user.id)
        except Exception:  # noqa: BLE001
            pass
        return

    await db.add_join_request(chat.id, user.id, user.username, user.full_name)
    handle = f"@{user.username}" if user.username else "(no username)"
    sent = await notify_owner(
        bot,
        "<b>Join request</b>\n"
        f"<blockquote>{_esc(user.full_name)} {_esc(handle)}\n"
        f"User ID: <code>{user.id}</code>\n"
        f"Group: {_esc(chat.title)} (<code>{chat.id}</code>)</blockquote>\n"
        f"Approve with: <code>/add &lt;amount&gt; &lt;account&gt; "
        f"{_esc(user.username or user.id)}</code>",
    )
    # Remember this log message so /add can edit it into the confirmation.
    if sent is not None:
        await db.set_request_owner_msg(chat.id, user.id, sent.message_id)


# --------------------------------------------------------------------------
# other members join/leave -> revoke used link + (service msgs cleaned below)
# --------------------------------------------------------------------------
@router.chat_member()
async def on_chat_member(event: ChatMemberUpdated, bot: Bot) -> None:
    old, new = event.old_chat_member.status, event.new_chat_member.status
    joined = old in _GONE_STATUSES and new in _PRESENT_STATUSES
    if not (joined and config.REVOKE_AFTER_JOIN):
        return
    used = event.invite_link
    if not used or not used.invite_link:
        return
    row = await db.get_active_link(used.invite_link)
    if not row:
        return
    try:
        await bot.revoke_chat_invite_link(event.chat.id, used.invite_link)
    except Exception:  # noqa: BLE001
        pass
    await db.deactivate_link(used.invite_link)
    log.info("revoked link after join in %s", event.chat.id)


# --------------------------------------------------------------------------
# clean service messages ("joined" / "left")
# --------------------------------------------------------------------------
@router.message(F.new_chat_members | F.left_chat_member)
async def clean_service(message: Message) -> None:
    if not config.CLEAN_SERVICE:
        return
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# owner commands (private chat, owner only)
# --------------------------------------------------------------------------
@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message) -> None:
    if not _is_owner_dm(message):
        return
    await cmd_help(message)


@router.message(Command("help"), F.chat.type == ChatType.PRIVATE)
async def cmd_help(message: Message) -> None:
    if not _is_owner_dm(message):
        return
    await message.answer(
        "<b>Owner commands</b>\n"
        "<blockquote>/groups - list admin groups\n"
        "/link &lt;name&gt; - approval links for matching groups\n"
        "/add &lt;amount&gt; &lt;account&gt; &lt;keyword&gt; - approve one request + confirm\n"
        "/remove &lt;username|id&gt; - decline + kick from all admin groups\n"
        "/pending - list pending join requests</blockquote>"
    )


@router.message(Command("groups"), F.chat.type == ChatType.PRIVATE)
async def cmd_groups(message: Message) -> None:
    if not _is_owner_dm(message):
        return
    groups = await db.list_admin_groups()
    if not groups:
        await message.answer("No admin groups recorded yet. Add me to a group as admin.")
        return
    lines = ["<b>Admin groups</b>"]
    for g in groups:
        lines.append(
            f"<blockquote>{_esc(g['title'])} ({_esc(g['short_form'])})\n"
            f"ID: <code>{g['chat_id']}</code></blockquote>"
        )
    await message.answer("\n".join(lines))


@router.message(Command("pending"), F.chat.type == ChatType.PRIVATE)
async def cmd_pending(message: Message) -> None:
    if not _is_owner_dm(message):
        return
    rows = await db.pending_requests()
    if not rows:
        await message.answer("No pending join requests.")
        return
    lines = ["<b>Pending join requests</b>"]
    for r in rows[:50]:
        handle = f"@{r['username']}" if r["username"] else f"id {r['user_id']}"
        lines.append(
            f"<blockquote>{_esc(r['full_name'])} {_esc(handle)}\n"
            f"User ID: <code>{r['user_id']}</code>\n"
            f"Group: <code>{r['chat_id']}</code></blockquote>"
        )
    await message.answer("\n".join(lines))


async def send_links(message: Message, bot: Bot, name: str) -> None:
    """Fetch approval invite links for admin groups matching `name`.

    `name == "all"` targets every group where the bot is an admin. Each link is
    stored so it can be revoked after one join (REVOKE_AFTER_JOIN).
    """
    name = name.strip()
    if name.lower() == "all":
        groups = await db.list_admin_groups()
        header = "all admin groups"
    else:
        groups = await db.find_groups_by_name(name)
        header = f'"{_esc(name)}"'

    if not groups:
        await message.answer(f"No admin group matches {header}.")
        return

    lines = [f"<b>Links for {header}</b>"]
    for g in groups:
        invite = await ensure_approval_link(bot, g["chat_id"], name=name[:32] or "owner")
        title = f"{_esc(g['title'])} ({_esc(g['short_form'])})"
        if invite:
            await db.add_link(g["chat_id"], invite, config.REVOKE_AFTER_JOIN)
            note = " (revokes after one join)" if config.REVOKE_AFTER_JOIN else ""
            lines.append(f"<blockquote>{title}</blockquote>\n{invite}{note}")
        else:
            lines.append(f"<blockquote>{title}</blockquote>\nCould not create a link (missing invite right).")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


@router.message(Command("link"), F.chat.type == ChatType.PRIVATE)
async def cmd_link(message: Message, command: CommandObject, bot: Bot) -> None:
    if not _is_owner_dm(message):
        return
    name = (command.args or "").strip()
    if not name:
        await message.answer(
            "Usage: <code>/link &lt;name&gt;</code>  e.g. <code>/link Lom</code> "
            "or <code>/link all</code>.\nYou can also just send the name."
        )
        return
    await send_links(message, bot, name)


@router.message(Command("add"), F.chat.type == ChatType.PRIVATE)
async def cmd_add(message: Message, command: CommandObject, bot: Bot) -> None:
    if not _is_owner_dm(message):
        return
    parts = (command.args or "").split()
    if len(parts) < 3:
        await message.answer(
            "Usage: <code>/add &lt;amount&gt; &lt;account&gt; &lt;keyword&gt;</code>\n"
            "The keyword identifies the pending user (username / id / name)."
        )
        return
    amount, account = parts[0], parts[1]
    keyword = " ".join(parts[2:])

    matches = await db.find_pending_by_keyword(keyword)
    if not matches:
        await message.answer(f"No pending join request matches \"{_esc(keyword)}\".")
        return
    if len({m["user_id"] for m in matches}) > 1:
        lines = ["Multiple pending requests match; be more specific:"]
        for m in matches[:10]:
            handle = f"@{m['username']}" if m["username"] else f"id {m['user_id']}"
            lines.append(f"<blockquote>{_esc(m['full_name'])} {_esc(handle)} "
                         f"(user <code>{m['user_id']}</code>)</blockquote>")
        await message.answer("\n".join(lines))
        return

    target = matches[0]
    chat_id, user_id = target["chat_id"], target["user_id"]

    try:
        await bot.approve_chat_join_request(chat_id, user_id)
    except TelegramBadRequest as e:
        await message.answer(f"Could not approve: {_esc(e.message)}")
        return
    await db.set_request_status(chat_id, user_id, "approved")

    group = await db.get_group(chat_id) or {}
    link = group.get("invite_link") or ""
    order = _order_id()
    await db.add_order(order, amount, account, keyword, user_id, chat_id, link)

    text = config.ADD_TEMPLATE.format(
        amount=_esc(amount), account=_esc(account), keyword=_esc(keyword),
        link=link or "(no link)", order=order,
        group=_esc(group.get("title", "")), short=_esc(group.get("short_form", "")),
        user=user_id, name=_esc(target.get("full_name", "")),
    )

    # Edit the original join-request log message into the filled template; if
    # that message is gone/too old, send a fresh confirmation instead.
    owner_msg_id = target.get("owner_msg_id")
    edited = False
    if owner_msg_id:
        try:
            await bot.edit_message_text(
                text, chat_id=config.OWNER_ID, message_id=owner_msg_id,
                disable_web_page_preview=True,
            )
            edited = True
        except TelegramBadRequest:
            edited = False
    if not edited:
        await message.answer(text, disable_web_page_preview=True)


@router.message(Command("remove"), F.chat.type == ChatType.PRIVATE)
async def cmd_remove(message: Message, command: CommandObject, bot: Bot) -> None:
    if not _is_owner_dm(message):
        return
    arg = (command.args or "").strip().lstrip("@")
    if not arg:
        await message.answer("Usage: <code>/remove &lt;username|id&gt;</code>")
        return

    user_id: int | None = None
    username: str | None = None
    if arg.isdigit():
        user_id = int(arg)
    else:
        username = arg
        # Bots cannot resolve arbitrary usernames; use what we've already seen.
        rows = await db.pending_requests()
        match = next((r for r in rows if (r["username"] or "").lower() == arg.lower()), None)
        if match:
            user_id = match["user_id"]

    if user_id is None:
        await message.answer(
            f"Cannot resolve \"@{_esc(arg)}\" to a user id. "
            "Use a numeric id, or the username of someone with a pending request."
        )
        return

    await db.add_removed(user_id, username)

    # Decline any pending requests for this user.
    for r in await db.pending_for_user(user_id):
        try:
            await bot.decline_chat_join_request(r["chat_id"], user_id)
        except Exception:  # noqa: BLE001
            pass
        await db.set_request_status(r["chat_id"], user_id, "declined")

    # Kick (ban then unban) from every admin group so they can rejoin later only
    # through a fresh approved request.
    kicked, failed = 0, 0
    for g in await db.list_admin_groups():
        try:
            await bot.ban_chat_member(g["chat_id"], user_id)
            await bot.unban_chat_member(g["chat_id"], user_id)
            kicked += 1
        except Exception:  # noqa: BLE001
            failed += 1

    await message.answer(
        f"<b>Removed</b> <code>{user_id}</code>\n"
        f"<blockquote>Kicked from: {kicked} group(s)\n"
        f"Failed: {failed}\nFuture join requests: auto-declined</blockquote>"
    )


# Plain text (no leading "/") from the owner in DM = a group name to fetch links
# for, e.g. sending "Lom" or "all". Registered last so real commands win first.
@router.message(F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def owner_name_lookup(message: Message, bot: Bot) -> None:
    if not _is_owner_dm(message):
        return
    await send_links(message, bot, message.text.strip())


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------
async def main() -> None:
    config.require()
    await db.init()

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    me = await bot.get_me()
    log.info("Running as @%s (id %s); owner %s", me.username, me.id, config.OWNER_ID)
    await notify_owner(bot, f"<b>@{_esc(me.username)} started.</b> Add me to a group as admin.")

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
