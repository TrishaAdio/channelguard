"""ChannelGuard admin bot — aiogram v3 application and handlers.

Run:  python -m bot           (from the repo root)
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import config, db
from .utils import render_template, truncate, unique_short_code

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("channelguard.bot")

bot = Bot(
    token=config.BOT_TOKEN or "MISSING",
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML, link_preview_is_disabled=True
    ),
)
dp = Dispatcher()

_GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def owner_only(message: Message) -> bool:
    return (
        message.from_user is not None
        and message.from_user.id == config.OWNER_ID
        and message.chat.type == ChatType.PRIVATE
    )


async def tell_owner(text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    """DM the owner, swallowing the 'owner never /started the bot' error."""
    try:
        await bot.send_message(config.OWNER_ID, text, reply_markup=reply_markup)
    except TelegramForbiddenError:
        log.warning("Owner %s has not started the bot yet — can't DM them.", config.OWNER_ID)
    except Exception as e:  # noqa: BLE001
        log.error("Failed to DM owner: %s: %s", type(e).__name__, e)


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


async def create_join_link(chat_id: int) -> Optional[str]:
    """Mint a fresh join-request (admin-approval) invite link."""
    try:
        inv = await bot.create_chat_invite_link(
            chat_id, name=config.LINK_TITLE, creates_join_request=True
        )
        return inv.invite_link
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.warning("create_chat_invite_link failed for %s: %s", chat_id, e)
        return None


async def revoke_link(chat_id: int, link: Optional[str]) -> None:
    if not link:
        return
    try:
        await bot.revoke_chat_invite_link(chat_id, link)
    except Exception as e:  # noqa: BLE001
        log.debug("revoke_chat_invite_link failed for %s: %s", chat_id, e)


async def get_or_create_link(chat_id: int) -> Optional[str]:
    """Reuse the group's stored link, minting one if none exists."""
    row = await db.get_group(chat_id)
    if row and row["invite_link"]:
        return row["invite_link"]
    link = await create_join_link(chat_id)
    if link:
        await db.set_group_link(chat_id, link)
    return link


async def rotate_link(chat_id: int) -> Optional[str]:
    """Revoke the current link and store a fresh one (used after a join)."""
    row = await db.get_group(chat_id)
    old = row["invite_link"] if row else None
    new = await create_join_link(chat_id)
    if new:
        await db.set_group_link(chat_id, new)
        await revoke_link(chat_id, old)
    return new


async def member_count(chat_id: int) -> Optional[int]:
    try:
        return await bot.get_chat_member_count(chat_id)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# onboarding — the bot's own membership changes
# --------------------------------------------------------------------------
@dp.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated) -> None:
    chat = update.chat
    if chat.type not in _GROUP_TYPES:
        return
    status = update.new_chat_member.status
    title = chat.title or chat.full_name or str(chat.id)

    if status == ChatMemberStatus.ADMINISTRATOR:
        await onboard_group(chat)
    elif status == ChatMemberStatus.MEMBER:
        await db.upsert_group(
            chat.id, title, "", chat.type, getattr(chat, "username", None),
            None, is_admin=False,
        )
        await db.log_event("added_no_admin", chat.id)
        await tell_owner(
            f"Added to <b>{esc(title)}</b> <code>{chat.id}</code> "
            f"but <b>not admin</b>.\n"
            "<blockquote>Promote with Invite Users, Delete Messages and "
            "Ban Users rights to arm it.</blockquote>"
        )
    elif status in (
        ChatMemberStatus.LEFT,
        ChatMemberStatus.KICKED,
        ChatMemberStatus.RESTRICTED,
    ):
        await db.set_group_admin(chat.id, False)
        await db.log_event("removed", chat.id)
        await tell_owner(f"Removed from <b>{esc(title)}</b> <code>{chat.id}</code>.")


async def onboard_group(chat) -> None:
    """Register a group the bot was just made admin of and DM the owner."""
    title = chat.title or chat.full_name or str(chat.id)
    existing = await db.all_groups()
    taken = [g["short_code"] for g in existing if g["chat_id"] != chat.id and g["short_code"]]
    code = unique_short_code(title, taken)

    link = await create_join_link(chat.id)
    await db.upsert_group(
        chat.id, title, code, chat.type, getattr(chat, "username", None),
        link, is_admin=True,
    )
    await db.log_event("added", chat.id, detail=code)

    count = await member_count(chat.id)
    lines = [
        f"<b>{esc(title)}</b>",
        f"<blockquote>Short <code>{esc(code)}</code>   "
        f"ID <code>{chat.id}</code>   "
        f"{esc(chat.type)}"
        + (f"   {count} members" if count is not None else "")
        + "</blockquote>",
    ]
    if link:
        lines.append(f"{esc(link)}")
    else:
        lines.append(
            "<blockquote>No link — grant the Invite Users right, then send "
            f"<code>{esc(code)}</code> here.</blockquote>"
        )
    await tell_owner("\n".join(lines))
    log.info("Onboarded %s (%s) code=%s", title, chat.id, code)


# --------------------------------------------------------------------------
# join requests
# --------------------------------------------------------------------------
@dp.chat_join_request()
async def on_join_request(req: ChatJoinRequest) -> None:
    chat = req.chat
    user = req.from_user
    full_name = user.full_name or str(user.id)
    username = user.username
    used_link = req.invite_link.invite_link if req.invite_link else None

    await db.add_join_request(chat.id, user.id, username, full_name, used_link)
    await db.log_event("join_request", chat.id, user.id)

    uname = f"@{username}" if username else "no username"
    title = chat.title or str(chat.id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="Approve", callback_data=f"jr:a:{chat.id}:{user.id}"
            ),
            InlineKeyboardButton(
                text="Decline", callback_data=f"jr:d:{chat.id}:{user.id}"
            ),
        ]]
    )
    await tell_owner(
        f"Join request — <b>{esc(title)}</b>\n"
        f"<blockquote>{esc(full_name)}   {esc(uname)}   "
        f"<code>{user.id}</code></blockquote>",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("jr:"))
async def on_join_decision(cb: CallbackQuery) -> None:
    if cb.from_user.id != config.OWNER_ID:
        await cb.answer("Not allowed.", show_alert=True)
        return
    try:
        _, action, chat_raw, user_raw = cb.data.split(":")
        chat_id, user_id = int(chat_raw), int(user_raw)
    except ValueError:
        await cb.answer("Bad request.", show_alert=True)
        return

    if action == "a":
        try:
            await bot.approve_chat_join_request(chat_id, user_id)
        except Exception as e:  # noqa: BLE001
            await cb.answer(f"Approve failed: {type(e).__name__}", show_alert=True)
            return
        await db.set_join_status(chat_id, user_id, "approved")
        await db.log_event("approved", chat_id, user_id)
        note = "Approved."
        if config.ROTATE_ON_JOIN:
            if await rotate_link(chat_id):
                note = "Approved. Link rotated."
        await _finish_callback(cb, note)
    elif action == "d":
        try:
            await bot.decline_chat_join_request(chat_id, user_id)
        except Exception as e:  # noqa: BLE001
            await cb.answer(f"Decline failed: {type(e).__name__}", show_alert=True)
            return
        await db.set_join_status(chat_id, user_id, "declined")
        await db.log_event("declined", chat_id, user_id)
        await _finish_callback(cb, "Declined.")
    else:
        await cb.answer("Unknown action.", show_alert=True)


async def _finish_callback(cb: CallbackQuery, note: str) -> None:
    await cb.answer(note)
    if cb.message:
        try:
            await cb.message.edit_text(f"{cb.message.html_text}\n<b>{esc(note)}</b>")
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# clean service — delete join/left system messages in groups
# --------------------------------------------------------------------------
@dp.message(
    (F.new_chat_members | F.left_chat_member)
    & F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
)
async def clean_service(message: Message) -> None:
    if not config.CLEAN_SERVICE:
        return
    try:
        await message.delete()
    except Exception as e:  # noqa: BLE001
        log.debug("clean_service delete failed in %s: %s", message.chat.id, e)


# --------------------------------------------------------------------------
# owner commands (private chat)
# --------------------------------------------------------------------------
HELP = (
    "<b>ChannelGuard</b>\n"
    "<blockquote>Add me as admin to a group with Invite Users, Delete "
    "Messages and Ban Users rights. I onboard it, mint an approval-required "
    "link, and report here.</blockquote>\n"
    "<b>Commands</b>\n"
    "<code>/groups</code> registered groups + short codes\n"
    "<code>/add &lt;amount&gt; &lt;account&gt; &lt;keyword&gt; [body]</code> save a link template\n"
    "<code>/list</code> saved templates\n"
    "<code>/pending</code> pending join requests\n"
    "<code>/remove &lt;keyword | @user | id&gt;</code> delete a template, or remove a user everywhere\n\n"
    "<b>Get links</b>\n"
    "<blockquote>Send a short code or name (e.g. <code>Lm</code>), a saved "
    "keyword, or <code>all</code> for every group. I reply with the "
    "approval-required link(s).</blockquote>\n"
    "<b>Template tokens</b>\n"
    "<code>{link} {title} {short} {amount} {name} {keyword}</code>"
)


@dp.message(Command("start", "help"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message) -> None:
    if message.from_user and message.from_user.id == config.OWNER_ID:
        await message.answer(HELP)
    else:
        await message.answer("This is a private admin bot.")


@dp.message(Command("groups"), F.chat.type == ChatType.PRIVATE)
async def cmd_groups(message: Message) -> None:
    if not owner_only(message):
        return
    groups = await db.all_groups()
    if not groups:
        await message.answer("No groups yet. Add me as admin to one.")
        return
    lines = ["<b>Groups</b>"]
    for g in groups:
        flag = "" if g["is_admin"] else " (not admin)"
        link = "linked" if g["invite_link"] else "no link"
        lines.append(
            f"<code>{esc(g['short_code'] or '-')}</code> "
            f"{esc(truncate(g['title']))} "
            f"<blockquote>{esc(g['chat_id'])}   {esc(g['chat_type'])}   "
            f"{link}{flag}</blockquote>"
        )
    await message.answer("\n".join(lines))


@dp.message(Command("add"), F.chat.type == ChatType.PRIVATE)
async def cmd_add(message: Message, command: CommandObject) -> None:
    if not owner_only(message):
        return
    args = (command.args or "").strip()
    if not args:
        await message.answer(
            "Usage: <code>/add &lt;amount&gt; &lt;account&gt; &lt;keyword&gt; [body]</code>\n"
            "<blockquote>Reply to a formatted message to use it as the body, "
            "or type the body inline. Body may use "
            "<code>{link} {amount} {name} {keyword}</code>.</blockquote>"
        )
        return

    parts = args.split(maxsplit=3)
    if len(parts) < 3:
        await message.answer("Need at least: amount, account, keyword.")
        return
    amount, account, keyword = parts[0], parts[1], parts[2]
    inline_body = parts[3] if len(parts) > 3 else ""

    # A reply supplies a rich (HTML) body verbatim; inline text is the fallback.
    reply = message.reply_to_message
    if reply and (reply.html_text or reply.caption):
        body = reply.html_text or reply.caption or ""
    elif inline_body:
        body = inline_body
    else:
        body = config.DEFAULT_TEMPLATE

    await db.upsert_template(keyword, amount, account, body)
    await db.log_event("template_add", detail=keyword.lower())
    await message.answer(
        f"Saved template <code>{esc(keyword.lower())}</code>\n"
        f"<blockquote>amount <code>{esc(amount)}</code>   "
        f"account <code>{esc(account)}</code></blockquote>\n"
        f"Send <code>{esc(keyword.lower())}</code> to use it."
    )


@dp.message(Command("list"), F.chat.type == ChatType.PRIVATE)
async def cmd_list(message: Message) -> None:
    if not owner_only(message):
        return
    rows = await db.all_templates()
    if not rows:
        await message.answer("No templates saved.")
        return
    lines = ["<b>Templates</b>"]
    for r in rows:
        lines.append(
            f"<code>{esc(r['keyword'])}</code> "
            f"<blockquote>amount <code>{esc(r['amount'])}</code>   "
            f"account <code>{esc(r['account_name'])}</code></blockquote>"
        )
    await message.answer("\n".join(lines))


@dp.message(Command("pending"), F.chat.type == ChatType.PRIVATE)
async def cmd_pending(message: Message) -> None:
    if not owner_only(message):
        return
    rows = await db.pending_requests()
    if not rows:
        await message.answer("No pending join requests.")
        return
    for r in rows:
        g = await db.get_group(r["chat_id"])
        title = g["title"] if g else str(r["chat_id"])
        uname = f"@{r['username']}" if r["username"] else "no username"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="Approve",
                    callback_data=f"jr:a:{r['chat_id']}:{r['user_id']}",
                ),
                InlineKeyboardButton(
                    text="Decline",
                    callback_data=f"jr:d:{r['chat_id']}:{r['user_id']}",
                ),
            ]]
        )
        await message.answer(
            f"<b>{esc(title)}</b>\n"
            f"<blockquote>{esc(r['full_name'])}   {esc(uname)}   "
            f"<code>{r['user_id']}</code></blockquote>",
            reply_markup=kb,
        )


@dp.message(Command("remove"), F.chat.type == ChatType.PRIVATE)
async def cmd_remove(message: Message, command: CommandObject) -> None:
    if not owner_only(message):
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Usage: <code>/remove &lt;keyword | @user | id&gt;</code>")
        return

    # 1) A saved template keyword?
    if await db.remove_template(arg):
        await db.log_event("template_remove", detail=arg.lower())
        await message.answer(f"Template <code>{esc(arg.lower())}</code> removed.")
        return

    # 2) Otherwise treat it as a user: decline pending requests + kick everywhere.
    await _remove_user(message, arg)


async def _remove_user(message: Message, ident: str) -> None:
    user_id: Optional[int] = None
    if ident.lstrip("-").isdigit():
        user_id = int(ident)
        pend = await db.pending_requests(user_id)
    else:
        pend = await db.find_pending_by_username(ident)
        if pend:
            user_id = pend[0]["user_id"]

    if user_id is None:
        await message.answer(
            "No matching template or known user. For a username the user must "
            "have a pending/known request; otherwise use a numeric id."
        )
        return

    declined = 0
    for r in pend:
        try:
            await bot.decline_chat_join_request(r["chat_id"], user_id)
            declined += 1
        except Exception:  # noqa: BLE001
            pass
        await db.set_join_status(r["chat_id"], user_id, "declined")

    kicked = 0
    for g in await db.all_groups(admin_only=True):
        try:
            await bot.ban_chat_member(g["chat_id"], user_id)
            await bot.unban_chat_member(g["chat_id"], user_id, only_if_banned=True)
            kicked += 1
        except Exception:  # noqa: BLE001
            pass
    await db.log_event("remove_user", user_id=user_id,
                       detail=f"declined={declined} kicked={kicked}")
    await message.answer(
        f"User <code>{user_id}</code>: declined {declined} request(s), "
        f"removed from {kicked} group(s)."
    )


@dp.message(F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def on_lookup(message: Message) -> None:
    """Bare text from the owner = a group name / short code / keyword lookup."""
    if not owner_only(message):
        return
    await distribute(message, message.text.strip())


async def distribute(message: Message, query: str) -> None:
    if not query:
        return
    tpl = await db.get_template(query.lower())
    selector = query
    amount = account = ""
    body = config.DEFAULT_TEMPLATE
    if tpl:
        body = tpl["body"] or config.DEFAULT_TEMPLATE
        amount, account = tpl["amount"], tpl["account_name"]
        selector = tpl["keyword"]

    if selector.strip().lower() == "all":
        groups = await db.all_groups(admin_only=True)
    else:
        groups = await db.find_groups(selector)

    if not groups:
        await message.answer(
            f"Nothing matches <code>{esc(query)}</code>. "
            "Try <code>/groups</code> or <code>all</code>."
        )
        return

    sent = 0
    for g in groups:
        link = await get_or_create_link(g["chat_id"])
        if not link:
            await message.answer(
                f"<b>{esc(g['title'])}</b>\n"
                "<blockquote>No link — I need the Invite Users right "
                "there.</blockquote>"
            )
            continue
        rendered = render_template(
            body,
            {
                "link": link,
                "title": g["title"],
                "short": g["short_code"],
                "amount": amount,
                "name": account,
                "keyword": query.lower(),
            },
        )
        await message.answer(rendered)
        sent += 1
    await db.log_event("distribute", detail=f"{query} -> {sent}")


# --------------------------------------------------------------------------
# global error trap — the bot must never die on a single bad update
# --------------------------------------------------------------------------
@dp.errors()
async def on_error(event) -> bool:
    log.exception("Unhandled update error: %s", getattr(event, "exception", event))
    return True  # mark handled so polling continues


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
async def on_startup() -> None:
    await db.init()
    me = await bot.get_me()
    log.info("Started as @%s (id %s). Owner=%s", me.username, me.id, config.OWNER_ID)
    await tell_owner(
        f"<b>ChannelGuard online</b> as @{esc(me.username)}.\n"
        "<blockquote>Add me as admin to a group to begin. "
        "Send <code>/help</code> for commands.</blockquote>"
    )


async def on_shutdown() -> None:
    await db.close()
    await bot.session.close()


async def run() -> None:
    config.require()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    log.info("Polling...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit) as e:
        if isinstance(e, SystemExit) and e.code not in (0, None):
            raise
        print("\nStopped.")


if __name__ == "__main__":
    main()
