"""ChannelGuard admin bot — aiogram v3 application and handlers.

Run:  python -m bot           (from the repo root)
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
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

# Shared link store (repo root). The bot writes every link it generates here so
# the userbot can paste it into the payment "Thanks for paying" message.
try:
    import linkstore  # available because both run from the repo root
except Exception:  # noqa: BLE001
    linkstore = None


def _remember(link: str | None, title: str = "", short: str = "") -> None:
    if link and linkstore is not None:
        try:
            linkstore.save_link(link, title, short)
        except Exception:  # noqa: BLE001
            pass

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


async def deny(message: Message) -> None:
    """Tell a non-owner (or a mis-set OWNER_ID) exactly what's wrong, instead
    of silently ignoring them — the usual cause of 'commands don't work'."""
    uid = message.from_user.id if message.from_user else "?"
    await message.answer(
        "Not authorized.\n"
        f"<blockquote>Your id <code>{uid}</code>\n"
        f"Configured owner <code>{config.OWNER_ID}</code></blockquote>\n"
        "If these differ, set <code>OWNER_ID</code> in <code>bot/.env</code> "
        "to your id and restart."
    )


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


async def create_single_use_link(chat_id: int) -> Optional[str]:
    """Mint a fresh single-use (member_limit=1) invite link — only one user
    can join through it, no approval step."""
    try:
        inv = await bot.create_chat_invite_link(
            chat_id, name=config.LINK_TITLE, member_limit=1
        )
        return inv.invite_link
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.warning("create single-use link failed for %s: %s", chat_id, e)
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
    prev = await db.get_group(chat.id)
    existing = await db.all_groups()
    taken = [g["short_code"] for g in existing if g["chat_id"] != chat.id and g["short_code"]]
    # Keep a previously assigned short code AND link stable across re-adds /
    # re-promotions — don't churn a link the owner may have already shared.
    code = prev["short_code"] if prev and prev["short_code"] else unique_short_code(title, taken)
    link = prev["invite_link"] if prev and prev["invite_link"] else await create_join_link(chat.id)
    await db.upsert_group(
        chat.id, title, code, chat.type, getattr(chat, "username", None),
        link, is_admin=True,
    )
    await db.log_event("added", chat.id, detail=code)
    _remember(link, title, code)

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

    title = chat.title or str(chat.id)

    # (1) This user was reserved for this group (CP:USERID) -> approve ONLY them,
    # then revoke the link so it can't be reused.
    binding = await db.get_binding(chat.id, user.id)
    if binding and binding["status"] == "pending":
        try:
            await bot.approve_chat_join_request(chat.id, user.id)
            await db.set_binding_status(chat.id, user.id, "approved")
            await db.set_join_status(chat.id, user.id, "approved")
            await revoke_link(chat.id, binding["invite_link"])
            await db.log_event("bind_approved", chat.id, user.id)
            await tell_owner(
                f"Approved reserved user for <b>{esc(title)}</b>\n"
                f"<blockquote><code>{user.id}</code> — link revoked</blockquote>"
            )
        except Exception as e:  # noqa: BLE001
            await tell_owner(f"Auto-approve failed for <code>{user.id}</code>: {esc(e)}")
        return

    # (2) Someone else tried a link reserved for a specific user -> decline them.
    if used_link:
        reserved = await db.binding_by_link(used_link)
        if reserved and reserved["user_id"] != user.id:
            try:
                await bot.decline_chat_join_request(chat.id, user.id)
                await db.set_join_status(chat.id, user.id, "declined")
                await tell_owner(
                    f"Declined <code>{user.id}</code> — that link is reserved for "
                    f"<code>{reserved['user_id']}</code> in <b>{esc(title)}</b>."
                )
            except Exception:  # noqa: BLE001
                pass
            return

    uname = f"@{username}" if username else "no username"
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
# member joins — tie a buyer to their order link and burn the link
# --------------------------------------------------------------------------
_JOINED_STATUSES = {ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED}


@dp.chat_member()
async def on_chat_member(update: ChatMemberUpdated) -> None:
    old_status = update.old_chat_member.status
    new_status = update.new_chat_member.status
    # Only care about a real join (was outside -> now inside) via a link.
    if new_status not in _JOINED_STATUSES:
        return
    if old_status in _JOINED_STATUSES:
        return
    if not update.invite_link:
        return

    link_row = await db.find_order_link_by_invite(update.invite_link.invite_link)
    if not link_row:
        return

    user = update.new_chat_member.user
    order_id = link_row["order_id"]
    await db.set_order_link_joined(link_row["id"], user.id)
    await db.set_order_status(order_id, "joined")
    # Single-use link is spent — revoke it so it can never be reused.
    await revoke_link(update.chat.id, update.invite_link.invite_link)
    await db.set_order_link_revoked(link_row["id"])
    await db.log_event("order_joined", update.chat.id, user.id, order_id)

    uname = f"@{user.username}" if user.username else "no username"
    await tell_owner(
        f"Order <code>{esc(order_id)}</code> joined — "
        f"<b>{esc(update.chat.title or update.chat.id)}</b>\n"
        f"<blockquote>{esc(user.full_name)}   {esc(uname)}   "
        f"<code>{user.id}</code></blockquote>"
    )


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
    "<b>Orders</b>\n"
    "<code>/add &lt;amount&gt; &lt;account&gt; &lt;keyword&gt;</code> mint a single-use link (one buyer) with an order id\n"
    "<code>/revoke &lt;orderid&gt;</code> kill the order's link(s) and ban the buyer\n"
    "<code>/orders</code> recent orders\n"
    "<code>/tpl &lt;keyword&gt; [body]</code> set the order post format\n\n"
    "<b>Groups &amp; users</b>\n"
    "<code>/groups</code> registered groups + short codes\n"
    "<code>/list</code> saved templates\n"
    "<code>/pending</code> pending join requests\n"
    "<code>/remove &lt;keyword | @user | id&gt;</code> delete a template, or remove a user everywhere\n\n"
    "<b>Quick link</b>\n"
    "<blockquote>Send a short code / name / <code>all</code> to get the "
    "approval-required link(s).</blockquote>\n"
    "<b>Reserve for one buyer</b>\n"
    "<blockquote>Send <code>&lt;group&gt;:&lt;userid&gt;</code> "
    "(e.g. <code>cp:7406804576</code>): I make a link for that group and "
    "approve ONLY that user on join, then revoke it.</blockquote>\n"
    "<b>Tokens</b>\n"
    "<code>{link} {title} {short} {amount} {name} {keyword} {orderid}</code>"
)


@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """Works for anyone, anywhere — used to find/verify your OWNER_ID."""
    uid = message.from_user.id if message.from_user else "?"
    match = "yes" if uid == config.OWNER_ID else "NO"
    await message.answer(
        f"<blockquote>Your id <code>{uid}</code>\n"
        f"Owner id <code>{config.OWNER_ID}</code>\n"
        f"Owner match: <b>{match}</b></blockquote>"
    )


@dp.message(Command("start", "help"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message) -> None:
    if message.from_user and message.from_user.id == config.OWNER_ID:
        await message.answer(HELP)
    else:
        await deny(message)


@dp.message(Command("groups"), F.chat.type == ChatType.PRIVATE)
async def cmd_groups(message: Message) -> None:
    if not owner_only(message):
        return await deny(message)
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
    """Create a paid order: mint a single-use link per matched group, post it
    (with an ANI order id) here and to the payment channel."""
    if not owner_only(message):
        return await deny(message)
    args = (command.args or "").strip()
    parts = args.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Usage: <code>/add &lt;amount&gt; &lt;account&gt; &lt;keyword&gt;</code>\n"
            "<blockquote>keyword = a group short code / name, or <code>all</code>. "
            "Mints a single-use link only that buyer can use and posts it with "
            "an order id. Set the post format with "
            "<code>/tpl &lt;keyword&gt;</code>.</blockquote>"
        )
        return
    amount, account, keyword = parts[0], parts[1], parts[2]

    if keyword.strip().lower() == "all":
        groups = await db.all_groups(admin_only=True)
    else:
        groups = await db.find_groups(keyword)
    if not groups:
        await message.answer(
            f"Nothing matches <code>{esc(keyword)}</code>. "
            "Try <code>/groups</code> or <code>all</code>."
        )
        return

    tpl = await db.get_template(keyword.lower())
    body = tpl["body"] if tpl and tpl["body"] else config.ORDER_TEMPLATE

    order_id = await db.next_order_id(config.ORDER_PREFIX)
    await db.create_order(order_id, amount, account, keyword.lower())

    made = 0
    for g in groups:
        link = await create_single_use_link(g["chat_id"])
        if not link:
            await message.answer(
                f"<b>{esc(g['title'])}</b>\n"
                "<blockquote>No link — I need the Invite Users right "
                "there.</blockquote>"
            )
            continue
        await db.add_order_link(order_id, g["chat_id"], link)
        _remember(link, g["title"], g["short_code"])
        rendered = render_template(
            body,
            {
                "link": link,
                "title": g["title"],
                "short": g["short_code"],
                "amount": amount,
                "name": account,
                "keyword": keyword.lower(),
                "orderid": order_id,
            },
        )
        await message.answer(rendered)
        if config.PAYMENT_CHANNEL:
            try:
                await bot.send_message(config.PAYMENT_CHANNEL, rendered)
            except Exception as e:  # noqa: BLE001
                log.warning("payment channel post failed: %s", e)
        made += 1

    await db.log_event("order_add", detail=f"{order_id} keyword={keyword} links={made}")
    await message.answer(
        f"Order <code>{esc(order_id)}</code> — {made} single-use link(s)."
    )


@dp.message(Command("tpl"), F.chat.type == ChatType.PRIVATE)
async def cmd_tpl(message: Message, command: CommandObject) -> None:
    """Set the post body used for a keyword's order links.

    Usage: /tpl <keyword> [body...]  — or reply to a formatted message.
    """
    if not owner_only(message):
        return await deny(message)
    args = (command.args or "").strip()
    if not args:
        await message.answer(
            "Usage: <code>/tpl &lt;keyword&gt; [body]</code>\n"
            "<blockquote>Reply to a formatted message to store it verbatim. "
            "Tokens: <code>{link} {title} {short} {amount} {name} {keyword} "
            "{orderid}</code>.</blockquote>"
        )
        return
    parts = args.split(maxsplit=1)
    keyword = parts[0]
    inline_body = parts[1] if len(parts) > 1 else ""

    reply = message.reply_to_message
    if reply and (reply.html_text or reply.caption):
        body = reply.html_text or reply.caption or ""
    elif inline_body:
        body = inline_body
    else:
        await message.answer("Give a body inline or reply to a message.")
        return

    await db.upsert_template(keyword, "", "", body)
    await db.log_event("template_add", detail=keyword.lower())
    await message.answer(
        f"Template <code>{esc(keyword.lower())}</code> saved. "
        f"Used by <code>/add &lt;amount&gt; &lt;account&gt; {esc(keyword.lower())}</code>."
    )


@dp.message(Command("list"), F.chat.type == ChatType.PRIVATE)
async def cmd_list(message: Message) -> None:
    if not owner_only(message):
        return await deny(message)
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
        return await deny(message)
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
        return await deny(message)
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


@dp.message(Command("revoke"), F.chat.type == ChatType.PRIVATE)
async def cmd_revoke(message: Message, command: CommandObject) -> None:
    """Revoke an order's link(s) and ban whoever joined through them."""
    if not owner_only(message):
        return await deny(message)
    oid = (command.args or "").strip()
    if not oid:
        await message.answer("Usage: <code>/revoke &lt;orderid&gt;</code>")
        return

    order = await db.get_order(oid)
    if not order:
        await message.answer(f"No order <code>{esc(oid)}</code>.")
        return
    order_id = order["order_id"]

    links = await db.order_links(order_id)
    revoked = banned = 0
    for l in links:
        if not l["revoked"]:
            await revoke_link(l["chat_id"], l["invite_link"])
            await db.set_order_link_revoked(l["id"])
        revoked += 1
        if l["joined_user"]:
            try:
                await bot.ban_chat_member(l["chat_id"], l["joined_user"])
                banned += 1
            except Exception as e:  # noqa: BLE001
                log.warning("ban failed for %s in %s: %s",
                            l["joined_user"], l["chat_id"], e)
    await db.set_order_status(order_id, "revoked")
    await db.log_event("order_revoke", detail=f"{order_id} banned={banned}")
    await message.answer(
        f"Order <code>{esc(order_id)}</code> revoked — "
        f"{revoked} link(s), {banned} user(s) banned."
    )


@dp.message(Command("orders"), F.chat.type == ChatType.PRIVATE)
async def cmd_orders(message: Message) -> None:
    if not owner_only(message):
        return await deny(message)
    rows = await db.all_orders(limit=20)
    if not rows:
        await message.answer("No orders yet.")
        return
    lines = ["<b>Orders</b>"]
    for o in rows:
        lines.append(
            f"<code>{esc(o['order_id'])}</code> "
            f"<blockquote>{esc(o['status'])}   "
            f"amount <code>{esc(o['amount'])}</code>   "
            f"account <code>{esc(o['account_name'])}</code>   "
            f"key <code>{esc(o['keyword'])}</code></blockquote>"
        )
    await message.answer("\n".join(lines))


@dp.message(F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def on_lookup(message: Message) -> None:
    """Owner bare text:
    - <group>:<userid>  -> make a link for that group reserved for that ONE
      user (auto-approved on join, then revoked), e.g.  cp:7406804576
    - <group>           -> just fetch/show the link(s)."""
    if not owner_only(message):
        return await deny(message)
    text = message.text.strip()
    m = re.match(r"^(?P<q>.+?)\s*:\s*(?P<uid>\d{4,})$", text)
    if m:
        await bind_user_to_group(message, m.group("q").strip(), int(m.group("uid")))
        return
    await distribute(message, text)


async def bind_user_to_group(message: Message, query: str, user_id: int) -> None:
    """Mint an approval-required link for the matched group and reserve it for a
    single user id: only that user is auto-approved (then the link is revoked)."""
    if not query:
        await message.answer("Usage: <code>&lt;group&gt;:&lt;userid&gt;</code>  e.g. <code>cp:7406804576</code>")
        return
    groups = (await db.all_groups(admin_only=True)) if query.lower() == "all" \
        else await db.find_groups(query)
    if not groups:
        await message.answer(
            f"No group matches <code>{esc(query)}</code>. Try <code>/groups</code>."
        )
        return
    g = groups[0]
    link = await create_join_link(g["chat_id"])
    if not link:
        await message.answer(
            f"<b>{esc(g['title'])}</b>\n<blockquote>No link — I need the Invite "
            "Users right there.</blockquote>"
        )
        return
    await db.set_group_link(g["chat_id"], link)
    await db.add_binding(g["chat_id"], user_id, link)
    _remember(link, g["title"], g["short_code"])
    await db.log_event("bind", g["chat_id"], user_id, g["short_code"])
    await message.answer(
        f"<b>{esc(g['title'])}</b>\n"
        f"<blockquote>Reserved for <code>{user_id}</code> — only they are "
        f"approved on join, then the link is revoked</blockquote>\n"
        f"{esc(link)}"
    )


# Registered LAST: any /command that no specific handler above matched. This
# is why an unknown command (e.g. /setdone) now gets a reply instead of silence.
@dp.message(F.chat.type == ChatType.PRIVATE, F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    if not owner_only(message):
        return await deny(message)
    cmd = message.text.split()[0]
    await message.answer(
        f"Unknown command <code>{esc(cmd)}</code>. Send <code>/help</code> "
        "for the list."
    )


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
        _remember(link, g["title"], g["short_code"])
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
    # Drop any leftover webhook so long-polling actually receives updates.
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:  # noqa: BLE001
        log.debug("delete_webhook: %s", e)
    me = await bot.get_me()
    log.info("Started as @%s (id %s). OWNER_ID=%s", me.username, me.id, config.OWNER_ID)
    log.info("Commands are DM-only. If yours are ignored, DM the bot /id and "
             "check the owner match.")
    await tell_owner(
        f"<b>ChannelGuard online</b> as @{esc(me.username)}.\n"
        "<blockquote>Add me as admin to a group to begin. "
        "Send <code>/help</code> for commands.</blockquote>"
    )
    if linkstore is not None:
        asyncio.create_task(reservation_poller())
        log.info("Reservation poller started (userbot /add bridge).")


async def _fulfill_reservation(rid: str, query: str, user_id: int) -> None:
    """Turn a userbot reservation request into a real reserved link."""
    if not query or not user_id:
        linkstore.put_result(rid, "", "")
        return
    groups = (await db.all_groups(admin_only=True)) if query.lower() == "all" \
        else await db.find_groups(query)
    if not groups:
        linkstore.put_result(rid, "", "")
        await tell_owner(
            f"Reservation from userbot: no group matches <code>{esc(query)}</code> "
            f"for <code>{user_id}</code>."
        )
        return
    g = groups[0]
    link = await create_join_link(g["chat_id"])
    if not link:
        linkstore.put_result(rid, "", g["title"])
        await tell_owner(
            f"Reservation: <b>{esc(g['title'])}</b> — no link (I need the Invite "
            "Users right there)."
        )
        return
    await db.set_group_link(g["chat_id"], link)
    await db.add_binding(g["chat_id"], user_id, link)
    _remember(link, g["title"], g["short_code"])
    linkstore.put_result(rid, link, g["title"])
    await db.log_event("bind", g["chat_id"], user_id, g["short_code"])
    await tell_owner(
        f"Reserved <b>{esc(g['title'])}</b> for <code>{user_id}</code> "
        "(from userbot /add).\n"
        f"<blockquote>Only they are approved on join, then the link is "
        f"revoked</blockquote>\n{esc(link)}"
    )


async def reservation_poller() -> None:
    """Watch the shared file for userbot reservation requests and fulfill them."""
    seen: set[str] = set()
    while True:
        try:
            for req in linkstore.pending_requests():
                rid = req.get("id")
                if not rid or rid in seen or linkstore.has_result(rid):
                    if rid:
                        seen.add(rid)
                    continue
                seen.add(rid)
                await _fulfill_reservation(
                    rid, str(req.get("query", "")), int(req.get("user_id", 0) or 0)
                )
        except Exception as e:  # noqa: BLE001
            log.warning("reservation poller error: %s: %s", type(e).__name__, e)
        await asyncio.sleep(1.5)


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
