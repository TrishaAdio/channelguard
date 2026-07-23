"""ChannelGuard admin bot — aiogram v3 application and handlers.

Run:  python -m bot           (from the repo root)
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from decimal import Decimal, InvalidOperation
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
from .utils import fold_fonts, render_template, truncate, unique_short_code

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

# Validate before aiogram constructs a client; a placeholder token otherwise
# raises an unrelated TokenValidationError and hides the missing setting.
config.require()
bot = Bot(
    token=config.BOT_TOKEN,
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


async def tell_owner(
    text: str, reply_markup: Optional[InlineKeyboardMarkup] = None
) -> None:
    """DM the owner, swallowing the 'owner never /started the bot' error."""
    try:
        await bot.send_message(config.OWNER_ID, text, reply_markup=reply_markup)
    except TelegramForbiddenError:
        log.warning(
            "Owner %s has not started the bot yet — can't DM them.", config.OWNER_ID
        )
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


async def revoke_link(chat_id: int, link: Optional[str]) -> bool:
    """Revoke an invite link and report whether it is safely unusable."""
    if not link:
        return True
    try:
        await bot.revoke_chat_invite_link(chat_id, link)
        return True
    except TelegramBadRequest as e:
        detail = str(e).upper()
        if "INVITE_HASH_EXPIRED" in detail or "INVITE HASH EXPIRED" in detail:
            return True
        log.warning("revoke_chat_invite_link failed for %s: %s", chat_id, e)
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("revoke_chat_invite_link failed for %s: %s", chat_id, e)
        return False


async def revoke_or_journal(chat_id: int, link: Optional[str]) -> bool:
    """Revoke now or durably queue the link for background cleanup."""
    if await revoke_link(chat_id, link):
        return True
    if link and linkstore is not None:
        try:
            linkstore.queue_revoke(chat_id, link)
        except Exception as error:  # noqa: BLE001
            log.error("Could not journal failed revocation for %s: %s", chat_id, error)
    return False


async def remove_buyer(chat_id: int, user_id: Optional[int]) -> bool:
    """Kick a buyer without leaving a permanent ban."""
    if not user_id:
        return True
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        return True
    except Exception as error:  # noqa: BLE001
        log.warning("buyer removal failed for %s in %s: %s", user_id, chat_id, error)
        return False


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
    """Revoke the current general link before replacing it."""
    row = await db.get_group(chat_id)
    old = row["invite_link"] if row else None
    if old and not await revoke_link(chat_id, old):
        return None
    new = await create_join_link(chat_id)
    if new:
        await db.set_group_link(chat_id, new)
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
            chat.id,
            title,
            "",
            chat.type,
            getattr(chat, "username", None),
            None,
            is_admin=False,
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
    taken = [
        g["short_code"] for g in existing if g["chat_id"] != chat.id and g["short_code"]
    ]
    # Keep a previously assigned short code AND link stable across re-adds /
    # re-promotions — don't churn a link the owner may have already shared.
    code = (
        prev["short_code"]
        if prev and prev["short_code"]
        else unique_short_code(title, taken)
    )
    link = (
        prev["invite_link"]
        if prev and prev["invite_link"]
        else await create_join_link(chat.id)
    )
    await db.upsert_group(
        chat.id,
        title,
        code,
        chat.type,
        getattr(chat, "username", None),
        link,
        is_admin=True,
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

    # Auto-approval requires an exact chat + user + invite-link reservation.
    # A buyer using a general link (or another reservation) is never approved.
    reservation = None
    if used_link:
        reservation = await db.reservation_for_join(chat.id, user.id, used_link)
    if reservation:
        # Persist before Telegram approval. If the process dies after Telegram
        # accepts the approval, the retry loop sees `approving` and burns the
        # link rather than leaving it reusable.
        acquired = await db.claim_reservation_status(
            reservation["id"], {"pending"}, "approving"
        )
        if not acquired:
            return
        try:
            await bot.approve_chat_join_request(chat.id, user.id)
        except Exception as e:  # noqa: BLE001
            # Telegram failures can be ambiguous. Cancellation may also have
            # won while approval was in flight, so remove the user defensively.
            cancellation_won = not await db.claim_reservation_status(
                reservation["id"], {"approving"}, "approved_revoke_pending",
                f"approve uncertain: {type(e).__name__}: {e}",
            )
            await db.set_order_link_joined_by_invite(used_link, user.id)
            if reservation["order_id"]:
                await db.set_order_status(
                    reservation["order_id"],
                    "revoke_pending" if cancellation_won else "joined_revoke_pending",
                )
            if cancellation_won:
                if await remove_buyer(chat.id, user.id):
                    await db.set_order_link_buyer_removed_by_invite(used_link)
            if await revoke_link(chat.id, used_link):
                await db.set_order_link_revoked_by_invite(used_link)
                terminal = "cancelled" if cancellation_won else "completed"
                await db.claim_reservation_status(
                    reservation["id"],
                    {"cancel_requested" if cancellation_won else "approved_revoke_pending"},
                    terminal,
                )
            elif cancellation_won:
                await db.set_reservation_status(
                    reservation["id"],
                    "cancel_revoke_pending",
                    "uncertain approval raced cancellation",
                )
            if reservation["order_id"]:
                await db.reconcile_order_status(reservation["order_id"])
            await tell_owner(
                f"Auto-approve failed for <code>{user.id}</code>: {esc(e)}"
            )
            return

        await db.set_join_status(chat.id, user.id, "approved")
        await db.set_order_link_joined_by_invite(used_link, user.id)
        if reservation["order_id"]:
            await db.set_order_status(reservation["order_id"], "joined")
        still_valid = await db.claim_reservation_status(
            reservation["id"], {"approving"}, "approved_revoke_pending"
        )
        if not still_valid:
            current = await db.get_reservation(reservation["id"])
            cancellation_won = current and current["status"] in {
                "cancelling", "cancel_requested", "cancel_revoke_pending",
            }
            if not cancellation_won:
                # A stale-approval recovery worker already fenced the link.
                await tell_owner(
                    f"Approved reserved user for <b>{esc(title)}</b>\n"
                    f"<blockquote><code>{user.id}</code> — revocation handled</blockquote>"
                )
                return
            # Payment cancellation won while Telegram approval was in flight.
            removed = await remove_buyer(chat.id, user.id)
            if removed:
                await db.set_order_link_buyer_removed_by_invite(used_link)
            if await revoke_link(chat.id, used_link):
                await db.set_order_link_revoked_by_invite(used_link)
                await db.set_reservation_status(reservation["id"], "cancelled")
            else:
                await db.set_reservation_status(
                    reservation["id"],
                    "cancel_revoke_pending",
                    "approval raced cancellation; revoke retry scheduled",
                )
            await db.set_join_status(chat.id, user.id, "declined")
            if reservation["order_id"]:
                await db.set_order_status(
                    reservation["order_id"], "revoke_pending"
                )
                await db.reconcile_order_status(reservation["order_id"])
            return
        revoked = await revoke_link(chat.id, used_link)
        if revoked:
            await db.set_order_link_revoked_by_invite(used_link)
            await db.claim_reservation_status(
                reservation["id"], {"approved_revoke_pending"}, "completed"
            )
            await db.log_event("reservation_completed", chat.id, user.id)
            state = "link revoked"
        else:
            if reservation["order_id"]:
                await db.set_order_status(
                    reservation["order_id"], "joined_revoke_pending"
                )
            await db.claim_reservation_status(
                reservation["id"],
                {"approved_revoke_pending"},
                "approved_revoke_pending",
                "revoke failed; retry scheduled",
            )
            await db.log_event("reservation_revoke_pending", chat.id, user.id)
            state = "revocation pending"
        await tell_owner(
            f"Approved reserved user for <b>{esc(title)}</b>\n"
            f"<blockquote><code>{user.id}</code> — {state}</blockquote>"
        )
        return

    # Active reservation links never fall through to manual owner approval.
    if used_link:
        reserved = await db.active_reservation_by_link(chat.id, used_link)
        cancellation_states = {
            "cancelling", "cancel_requested", "cancel_revoke_pending",
            "expiring", "expire_revoke_pending",
        }
        should_decline = reserved and (
            reserved["user_id"] != user.id
            or reserved["status"] in cancellation_states
        )
        if should_decline:
            try:
                await bot.decline_chat_join_request(chat.id, user.id)
                await db.set_join_status(chat.id, user.id, "declined")
                await tell_owner(
                    f"Declined <code>{user.id}</code> for <b>{esc(title)}</b>."
                )
            except Exception as e:  # noqa: BLE001
                log.warning("reserved-link decline failed: %s", e)
            return
        if reserved:
            return

    uname = f"@{username}" if username else "no username"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Approve", callback_data=f"jr:a:{chat.id}:{user.id}"
                ),
                InlineKeyboardButton(
                    text="Decline", callback_data=f"jr:d:{chat.id}:{user.id}"
                ),
            ]
        ]
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
    # Single-use link is spent. Keep it pending in SQLite until Telegram
    # confirms revocation so the retry loop can finish ambiguous failures.
    if await revoke_link(update.chat.id, update.invite_link.invite_link):
        await db.set_order_link_revoked(link_row["id"])
        revoke_state = "link revoked"
    else:
        await db.set_order_status(order_id, "joined_revoke_pending")
        revoke_state = "revocation pending"
    await db.log_event("order_joined", update.chat.id, user.id, order_id)

    uname = f"@{user.username}" if user.username else "no username"
    await tell_owner(
        f"Order <code>{esc(order_id)}</code> joined — "
        f"<b>{esc(update.chat.title or update.chat.id)}</b>\n"
        f"<blockquote>{esc(user.full_name)}   {esc(uname)}   "
        f"<code>{user.id}</code>   {revoke_state}</blockquote>"
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
    "<code>/doctor</code> diagnose token, admin rights, link creation, shared store\n"
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


@dp.message(Command("doctor"), F.chat.type == ChatType.PRIVATE)
async def cmd_doctor(message: Message) -> None:
    """Self-diagnosis: proves (or disproves) each thing that usually breaks —
    owner match, the shared file bridge, and — per group — the bot's admin
    status and whether it can actually create the approval link."""
    if not owner_only(message):
        return await deny(message)
    me = await bot.get_me()
    out = [
        "<b>Doctor</b>",
        f"<blockquote>Bot @{esc(me.username)} <code>{me.id}</code>\n"
        f"OWNER_ID <code>{config.OWNER_ID}</code> "
        f"(you: <code>{message.from_user.id}</code>)</blockquote>",
    ]

    # Shared link store (the userbot bridge).
    if linkstore is None:
        out.append(
            "Shared store: <b>MISSING</b> — start the bot from the repo "
            "root (<code>python -m bot</code>) so it can import linkstore."
        )
    else:
        try:
            linkstore.request_link("__doctor__", 1)
            out.append(
                f"Shared store: <b>OK</b> <code>{esc(str(linkstore.STORE))}</code>"
            )
        except Exception as e:  # noqa: BLE001
            out.append(
                f"Shared store: <b>WRITE FAILED</b> {esc(type(e).__name__)}: {esc(e)}"
            )

    groups = await db.all_groups()
    if not groups:
        out.append(
            "No groups known yet. Add me to a group as admin (while I'm "
            "running) so I receive the event."
        )
    for g in groups[:15]:
        label = f"<code>{esc(g['short_code'] or '-')}</code> {esc(truncate(g['title'], 24))}"
        try:
            mem = await bot.get_chat_member(g["chat_id"], me.id)
            status = str(mem.status)
            can_invite = getattr(mem, "can_invite_users", None)
        except Exception as e:  # noqa: BLE001
            out.append(
                f"{label} — can't read my membership: {esc(type(e).__name__)}: {esc(e)}"
            )
            continue
        # The real test: can I actually mint an approval link here?
        try:
            inv = await bot.create_chat_invite_link(
                g["chat_id"], name="doctor", creates_join_request=True
            )
            await revoke_link(g["chat_id"], inv.invite_link)
            link_note = "<b>link OK</b>"
        except Exception as e:  # noqa: BLE001
            link_note = f"<b>link FAIL</b> {esc(type(e).__name__)}: {esc(e)}"
        invite_flag = (
            ""
            if can_invite is None
            else (" can_invite" if can_invite else " <b>NO invite right</b>")
        )
        out.append(f"{label} — {esc(status)}{invite_flag} — {link_note}")

    await message.answer("\n".join(out)[:4000])


async def _compensate_order(order_id: str) -> None:
    """Revoke every link from an order whose creation did not complete."""
    pending = False
    for order_link in await db.order_links(order_id):
        if order_link["revoked"]:
            continue
        if await revoke_link(order_link["chat_id"], order_link["invite_link"]):
            await db.set_order_link_revoked(order_link["id"])
        else:
            pending = True
    await db.set_order_status(order_id, "revoke_pending" if pending else "failed")
    if not pending:
        await db.delete_order_if_empty(order_id)


def _order_header(
    order_id: str, amount: str, account: str, keyword: str
) -> str:
    return (
        f"<b>Order <code>{esc(order_id)}</code></b>\n"
        f"<blockquote>Amount <code>{esc(amount)}</code>   "
        f"Account <code>{esc(account)}</code>   "
        f"Key <code>{esc(keyword)}</code></blockquote>"
    )


def _ensure_order_render(
    rendered: str,
    *,
    order_id: str,
    amount: str,
    account: str,
    group,
    link: str,
) -> str:
    """Keep required values even when a custom template omits/breaks tokens."""
    rendered = rendered or ""
    missing = []
    if order_id not in rendered:
        missing.append(f"Order <code>{esc(order_id)}</code>")
    if amount not in rendered:
        missing.append(f"Amount <code>{esc(amount)}</code>")
    if account and account not in rendered:
        missing.append(f"Account <code>{esc(account)}</code>")
    if link not in rendered:
        missing.append(esc(link))
    if missing:
        rendered += ("\n" if rendered else "") + "<blockquote>" + "   ".join(
            missing
        ) + "</blockquote>"
    # Never let one oversized/broken custom template hide the successful link.
    if len(rendered) > 3900:
        rendered = (
            f"<b>{esc(group['title'])}</b>\n"
            f"<blockquote>Order <code>{esc(order_id)}</code>   "
            f"Amount <code>{esc(amount)}</code>   "
            f"Account <code>{esc(account)}</code></blockquote>\n"
            f"{esc(link)}"
        )
    return rendered


def _order_response_blocks(
    order_id: str,
    amount: str,
    account: str,
    keyword: str,
    generated: list,
    failures: list,
) -> list[str]:
    blocks = [_order_header(order_id, amount, account, keyword)]
    blocks.extend(item[3] for item in generated)
    if failures:
        detail = "; ".join(
            f"{esc(item['title'])}: {esc(item['reason'])}" for item in failures
        )
        blocks.append(
            f"<b>Unavailable</b>\n"
            f"<blockquote>Order <code>{esc(order_id)}</code> — "
            f"{detail}</blockquote>"
        )
    return blocks


async def _send_html_blocks(send, blocks: list[str]) -> None:
    """Send complete HTML blocks without cutting tags at Telegram's limit."""
    chunks = []
    current = ""
    for block in blocks:
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= 4000:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = block
    if current:
        chunks.append(current)
    for chunk in chunks:
        await send(chunk)


@dp.message(Command("add"), F.chat.type == ChatType.PRIVATE)
async def cmd_add(message: Message, command: CommandObject) -> None:
    """Create one order and return its generated links once."""
    if not owner_only(message):
        return await deny(message)
    parts = (command.args or "").strip().split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Usage: <code>/add &lt;amount&gt; &lt;account&gt; &lt;keyword&gt;</code>"
        )
        return
    amount, account, keyword = parts
    try:
        parsed_amount = Decimal(amount.replace(",", ""))
        if not parsed_amount.is_finite() or parsed_amount <= 0:
            raise InvalidOperation
    except InvalidOperation:
        await message.answer("Amount must be a positive number.")
        return

    command_key = f"bot:{message.chat.id}:{message.message_id}"
    existing_order = await db.get_order_by_command_key(command_key)
    if existing_order is not None and (
        existing_order["response_html"] or existing_order["status"] != "open"
    ):
        response = existing_order["response_html"] or (
            _order_header(
                existing_order["order_id"],
                existing_order["amount"],
                existing_order["account_name"],
                existing_order["keyword"],
            )
            + f"\n\nStatus: <code>{esc(existing_order['status'])}</code>"
        )
        await _send_html_blocks(message.answer, response.split("\n\n"))
        return

    groups = (
        await db.all_groups(admin_only=True)
        if keyword.strip().lower() == "all"
        else await db.find_groups(keyword)
    )
    if not groups:
        await message.answer(f"Nothing matches <code>{esc(keyword)}</code>.")
        return

    tpl = await db.get_template(keyword.lower())
    body = tpl["body"] if tpl and tpl["body"] else config.ORDER_TEMPLATE
    order_id = (
        existing_order["order_id"]
        if existing_order is not None
        else await db.create_next_order(
            config.ORDER_PREFIX,
            amount,
            account,
            keyword.lower(),
            command_key=command_key,
            source="bot",
        )
    )
    existing_links = {
        row["chat_id"]: row for row in await db.order_links(order_id)
    }

    generated = []
    failures = []
    try:
        for group in groups:
            existing_link = existing_links.get(group["chat_id"])
            if existing_link is not None:
                link = existing_link["invite_link"]
                link_id = existing_link["id"]
            else:
                link = await create_single_use_link(group["chat_id"])
                if not link:
                    failures.append(
                        {
                            "title": group["title"],
                            "reason": "could not create invite link",
                        }
                    )
                    continue
                try:
                    link_id = await db.add_order_link(
                        order_id, group["chat_id"], link
                    )
                except Exception:
                    if not await revoke_link(group["chat_id"], link):
                        if linkstore is not None:
                            linkstore.queue_revoke(group["chat_id"], link)
                        log.error(
                            "Queued untracked invite for revocation: chat=%s",
                            group["chat_id"],
                        )
                    raise
            rendered = render_template(
                body,
                {
                    "link": link,
                    "title": group["title"],
                    "short": group["short_code"],
                    "amount": amount,
                    "name": account,
                    "keyword": keyword.lower(),
                    "orderid": order_id,
                },
            )
            rendered = _ensure_order_render(
                rendered,
                order_id=order_id,
                amount=amount,
                account=account,
                group=group,
                link=link,
            )
            generated.append((group, link_id, link, rendered))
    except Exception:
        await _compensate_order(order_id)
        raise

    if not generated:
        await db.set_order_status(order_id, "failed")
        await db.delete_order_if_empty(order_id)
        detail = "; ".join(
            f"{esc(item['title'])}: {esc(item['reason'])}" for item in failures
        )
        await message.answer(
            _order_header(order_id, amount, account, keyword)
            + "\n\n<b>Unavailable</b>\n<blockquote>"
            + (detail or "No invite links could be created.")
            + "</blockquote>"
        )
        return

    blocks = _order_response_blocks(
        order_id, amount, account, keyword, generated, failures
    )
    response = "\n\n".join(blocks)
    await db.set_order_response(order_id, response)
    try:
        await _send_html_blocks(message.answer, blocks)
    except Exception:
        await _compensate_order(order_id)
        raise

    if config.PAYMENT_CHANNEL:
        try:
            await _send_html_blocks(
                lambda text: bot.send_message(config.PAYMENT_CHANNEL, text),
                blocks,
            )
        except Exception as error:  # noqa: BLE001
            log.warning("payment channel post failed: %s", error)

    await db.log_event(
        "order_add", detail=f"{order_id} keyword={keyword} links={len(generated)}"
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
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Approve",
                        callback_data=f"jr:a:{r['chat_id']}:{r['user_id']}",
                    ),
                    InlineKeyboardButton(
                        text="Decline",
                        callback_data=f"jr:d:{r['chat_id']}:{r['user_id']}",
                    ),
                ]
            ]
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
    await db.log_event(
        "remove_user", user_id=user_id, detail=f"declined={declined} kicked={kicked}"
    )
    await message.answer(
        f"User <code>{user_id}</code>: declined {declined} request(s), "
        f"removed from {kicked} group(s)."
    )


@dp.message(Command("revoke"), F.chat.type == ChatType.PRIVATE)
async def cmd_revoke(message: Message, command: CommandObject) -> None:
    """Revoke every order link and safely remove every known buyer."""
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

    await db.set_order_status(order_id, "revoke_pending")
    reservations = await db.reservations_for_order(order_id)
    for request_id in {
        str(row["request_id"]) for row in reservations if row["request_id"]
    }:
        if linkstore is not None:
            try:
                linkstore.cancel_request(request_id, force=True)
            except Exception as error:  # noqa: BLE001
                log.warning("bridge cancellation failed for %s: %s", request_id, error)
        await _cancel_request_reservations(request_id)

    links = await db.order_links(order_id)
    revoked = pending = removed = 0
    for order_link in links:
        cleanup_pending = False
        if order_link["joined_user"] and not order_link["buyer_removed"]:
            if await remove_buyer(
                order_link["chat_id"], order_link["joined_user"]
            ):
                await db.set_order_link_buyer_removed(order_link["id"])
                removed += 1
            else:
                cleanup_pending = True
        if not order_link["revoked"]:
            if await revoke_link(order_link["chat_id"], order_link["invite_link"]):
                await db.set_order_link_revoked(order_link["id"])
                revoked += 1
            else:
                cleanup_pending = True
        else:
            revoked += 1
        if cleanup_pending:
            pending += 1
    await db.reconcile_order_status(order_id)
    if pending:
        await db.set_order_status(order_id, "revoke_pending")
        status = "revoke_pending"
    else:
        await db.set_order_status(order_id, "revoked")
        status = "revoked"
    await db.log_event(
        "order_revoke",
        detail=(
            f"{order_id} revoked={revoked} pending={pending} "
            f"removed={removed} status={status}"
        ),
    )
    await message.answer(
        f"Order <code>{esc(order_id)}</code> — "
        f"{revoked}/{len(links)} link(s) revoked, "
        f"{removed} buyer removal(s), {pending} cleanup pending. "
        f"Status: <code>{esc(status)}</code>."
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
        await message.answer(
            "Usage: <code>&lt;group&gt;:&lt;userid&gt;</code>  e.g. <code>cp:7406804576</code>"
        )
        return
    groups = (
        (await db.all_groups(admin_only=True))
        if query.lower() == "all"
        else await db.find_groups(query)
    )
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
    request_id = f"manual-{message.chat.id}-{message.message_id}"
    await db.add_reservation(request_id, query, g["chat_id"], user_id, link)
    await db.log_event("reservation_created", g["chat_id"], user_id, query)
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
# lifecycle and userbot reservation bridge
# --------------------------------------------------------------------------
_background_tasks: set[asyncio.Task] = set()


def _start_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def finished(done: asyncio.Task) -> None:
        _background_tasks.discard(done)
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            log.error(
                "Background task %s stopped: %s: %s",
                done.get_name(),
                type(error).__name__,
                error,
            )

    task.add_done_callback(finished)


async def on_startup() -> None:
    await db.init()
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:  # noqa: BLE001
        log.debug("delete_webhook: %s", e)
    me = await bot.get_me()
    log.info("Started as @%s (id %s). OWNER_ID=%s", me.username, me.id, config.OWNER_ID)
    log.info(
        "Commands are DM-only. If yours are ignored, DM the bot /id and "
        "check the owner match."
    )
    await tell_owner(
        f"<b>ChannelGuard online</b> as @{esc(me.username)}.\n"
        "<blockquote>Add me as admin to a group to begin. "
        "Send <code>/help</code> for commands.</blockquote>"
    )
    if linkstore is not None:
        _start_background(reservation_poller())
        log.info("Reservation poller started (userbot /add bridge).")
    _start_background(revocation_retry_loop())


async def _groups_for_keyword(query: str):
    """Resolve one keyword deterministically; reject ambiguous substring hits."""
    query = query.strip()
    if query.lower() == "all":
        return await db.all_groups(admin_only=True), ""
    matches = await db.find_groups(query)
    if not matches:
        return [], "no matching admin group"
    folded = fold_fonts(query).casefold()
    exact = [
        group
        for group in matches
        if fold_fonts(group["short_code"] or "").casefold() == folded
    ]
    if len(exact) == 1:
        return exact, ""
    if len(matches) == 1:
        return matches, ""
    names = ", ".join(str(group["title"]) for group in matches[:4])
    return [], f"ambiguous match: {names}"


async def _cancel_request_reservations(rid: str) -> None:
    """Fence and revoke links minted for a cancelled payment request."""
    reservations = await db.reservations_for_request(rid)
    order_ids = {
        str(row["order_id"]) for row in reservations if row["order_id"]
    }
    bridge_order = await db.get_order_by_request_id(rid)
    if bridge_order is not None:
        order_ids.add(str(bridge_order["order_id"]))
    for order_id in order_ids:
        await db.set_order_status(order_id, "revoke_pending")

    for reservation in reservations:
        status = reservation["status"]
        if status == "pending":
            acquired = await db.claim_reservation_status(
                reservation["id"], {"pending"}, "cancelling"
            )
        elif status == "cancelling":
            acquired = await db.claim_reservation_status(
                reservation["id"],
                {"cancelling"},
                "cancel_revoke_pending",
                "resumed interrupted cancellation",
            )
        elif status in {"cancel_requested", "cancel_revoke_pending"}:
            acquired = True
        elif status in {"approving", "approved_revoke_pending", "completed"}:
            acquired = await db.claim_reservation_status(
                reservation["id"],
                {status},
                "cancel_requested",
                "payment cancelled after reservation approval began",
            )
        else:
            continue
        if not acquired:
            continue

        removed = True
        if status not in {"pending", "cancelling"}:
            await db.set_order_link_joined_by_invite(
                reservation["invite_link"], reservation["user_id"]
            )
            removed = await remove_buyer(
                reservation["chat_id"], reservation["user_id"]
            )
            if removed:
                await db.set_order_link_buyer_removed_by_invite(
                    reservation["invite_link"]
                )

        if await revoke_link(reservation["chat_id"], reservation["invite_link"]):
            await db.set_order_link_revoked_by_invite(
                reservation["invite_link"]
            )
            await db.set_reservation_status(reservation["id"], "cancelled")
        else:
            await db.set_reservation_status(
                reservation["id"],
                "cancel_revoke_pending",
                "cancelled request; revoke retry scheduled",
            )
        if not removed:
            await db.set_reservation_status(
                reservation["id"],
                "cancel_revoke_pending",
                "cancelled request; buyer removal retry scheduled",
            )

    for order_id in order_ids:
        await db.reconcile_order_status(order_id)
        await db.delete_order_if_empty(order_id)

    remaining_reservations = await db.reservations_for_request(rid)
    terminal = all(
        row["status"] == "cancelled" for row in remaining_reservations
    )
    if terminal:
        for order_id in order_ids:
            links = await db.order_links(order_id)
            if any(
                not row["revoked"]
                or (row["joined_user"] and not row["buyer_removed"])
                for row in links
            ):
                terminal = False
                break
    if terminal and linkstore is not None:
        try:
            linkstore.complete_cancellation(rid)
        except Exception as error:  # noqa: BLE001
            log.warning("cancellation acknowledgement failed for %s: %s", rid, error)


async def _fulfill_reservation(
    rid: str, queries, user_id: int, lease_token: str, metadata=None
) -> None:
    """Have the bot resolve every keyword and mint buyer-bound join links."""
    clean_queries = []
    seen_queries = set()
    for raw in queries or []:
        query = str(raw).strip()
        key = query.casefold()
        if query and key not in seen_queries:
            seen_queries.add(key)
            clean_queries.append(query)
    if not clean_queries or not user_id:
        linkstore.put_result(
            rid,
            [],
            [{"keyword": "request", "reason": "invalid request"}],
            lease_token,
        )
        return

    metadata = dict(metadata or {})
    order_id = str(metadata.get("order_id") or "").strip()
    amount = str(metadata.get("amount") or "").strip()
    account_name = str(metadata.get("account_name") or "").strip()
    keyword_text = str(
        metadata.get("keyword") or " ".join(clean_queries)
    ).strip()
    if not order_id:
        order_id = await db.create_next_order(
            config.ORDER_PREFIX,
            amount,
            account_name,
            keyword_text,
            command_key=f"bridge:{rid}",
            source="bridge",
            buyer_id=user_id,
            request_id=rid,
        )
    else:
        created = await db.register_order(
            order_id,
            amount,
            account_name,
            keyword_text,
            command_key=f"bridge:{rid}",
            source="bridge",
            buyer_id=user_id,
            request_id=rid,
        )
        if not created:
            existing_order = await db.get_order(order_id)
            if (
                existing_order is None
                or existing_order["request_id"] not in ("", rid)
            ):
                linkstore.put_result(
                    rid,
                    [],
                    [{
                        "keyword": "order",
                        "reason": f"order id {order_id} is already in use",
                    }],
                    lease_token,
                )
                return

    entries = []
    failures = []
    selected_chats = set()
    for query in clean_queries:
        if linkstore.is_request_cancelled(rid):
            await _cancel_request_reservations(rid)
            return
        if not linkstore.renew_request(rid, lease_token):
            return
        groups, reason = await _groups_for_keyword(query)
        if not groups:
            failures.append({"keyword": query, "reason": reason})
            continue

        made_for_query = 0
        for group in groups:
            chat_id = group["chat_id"]
            if linkstore.is_request_cancelled(rid):
                await _cancel_request_reservations(rid)
                return
            if not linkstore.renew_request(rid, lease_token):
                return
            if chat_id in selected_chats:
                continue
            selected_chats.add(chat_id)

            existing = await db.reservation_for_request_group(rid, query, chat_id)
            if existing:
                await db.set_reservation_order(existing["id"], order_id)
                if existing["status"] == "pending":
                    link = existing["invite_link"]
                else:
                    failures.append(
                        {
                            "keyword": query,
                            "reason": f"reservation is {existing['status']}",
                        }
                    )
                    continue
            else:
                link = await create_join_link(chat_id)
                if not link:
                    failures.append(
                        {
                            "keyword": query,
                            "reason": f"could not create link for {group['title']}",
                        }
                    )
                    continue
                if linkstore.is_request_cancelled(rid):
                    await revoke_or_journal(chat_id, link)
                    await _cancel_request_reservations(rid)
                    return
                if not linkstore.renew_request(rid, lease_token):
                    await revoke_or_journal(chat_id, link)
                    return
                try:
                    inserted = await db.add_reservation(
                        rid, query, chat_id, user_id, link, order_id
                    )
                except Exception:
                    await revoke_or_journal(chat_id, link)
                    raise
                if not inserted:
                    # A lease successor/predecessor won the insert race. Revoke
                    # only this worker's duplicate and converge on the stored
                    # link; never overwrite or revoke the winner's reservation.
                    await revoke_or_journal(chat_id, link)
                    existing = await db.reservation_for_request_group(
                        rid, query, chat_id
                    )
                    if not existing or existing["status"] != "pending":
                        failures.append(
                            {
                                "keyword": query,
                                "reason": "reservation race could not be reconciled",
                            }
                        )
                        continue
                    link = existing["invite_link"]
                else:
                    await db.log_event("reservation_created", chat_id, user_id, query)

            await db.add_order_link(order_id, chat_id, link)
            entries.append(
                {
                    "link": link,
                    "title": group["title"],
                    "keyword": query,
                }
            )
            made_for_query += 1

        if not made_for_query and not any(
            failure["keyword"] == query for failure in failures
        ):
            failures.append({"keyword": query, "reason": "duplicate group"})

    if linkstore.is_request_cancelled(rid):
        await _cancel_request_reservations(rid)
        return
    if not linkstore.renew_request(rid, lease_token):
        return
    if not linkstore.put_result(rid, entries, failures, lease_token):
        if linkstore.is_request_cancelled(rid):
            await _cancel_request_reservations(rid)
        return
    if not entries:
        await db.set_order_status(order_id, "failed")
        await db.delete_order_if_empty(order_id)
    if entries:
        names = ", ".join(esc(entry["title"]) for entry in entries)
        await tell_owner(
            f"Reserved {len(entries)} group(s) for <code>{user_id}</code>: {names}"
        )
    if failures:
        failed = ", ".join(esc(item["keyword"]) for item in failures)
        await tell_owner(f"Reservation failed for: <code>{failed}</code>")


async def reservation_poller() -> None:
    """Lease bridge requests, retry transient failures, and publish results."""
    while True:
        try:
            # Cancellation is durable in the bridge store. Cleaning it here as
            # well as in the active worker closes the crash gap where a worker
            # dies after persisting a reservation but before observing cancel.
            for rid in linkstore.cancelled_request_ids():
                await _cancel_request_reservations(rid)
            for req in linkstore.claim_requests():
                rid = str(req.get("id", ""))
                lease_token = str(req.get("lease_token", ""))
                if not rid or not lease_token:
                    continue
                if linkstore.has_result(rid):
                    linkstore.complete_request(rid, lease_token)
                    continue
                queries = req.get("queries") or [req.get("query", "")]
                try:
                    await _fulfill_reservation(
                        rid,
                        queries,
                        int(req.get("user_id", 0) or 0),
                        lease_token,
                        req.get("metadata"),
                    )
                except Exception as e:  # noqa: BLE001
                    linkstore.release_request(
                        rid, lease_token, f"{type(e).__name__}: {e}"
                    )
                    log.warning(
                        "reservation %s failed; queued for retry: %s: %s",
                        rid,
                        type(e).__name__,
                        e,
                    )
        except Exception as e:  # noqa: BLE001
            log.warning("reservation poller error: %s: %s", type(e).__name__, e)
        await asyncio.sleep(1.5)


async def revocation_retry_loop() -> None:
    """Retry failed revocations and expire unused buyer reservations."""
    while True:
        try:
            if linkstore is not None:
                for item in linkstore.pending_revokes():
                    if await revoke_link(item["chat_id"], item["link"]):
                        linkstore.complete_revoke(item["chat_id"], item["link"])

            cutoff = time.time() - config.RESERVATION_TTL_MINUTES * 60
            for reservation in await db.expired_pending_reservations(cutoff):
                acquired = await db.claim_reservation_status(
                    reservation["id"], {"pending"}, "expiring"
                )
                if not acquired:
                    continue
                if await revoke_link(
                    reservation["chat_id"], reservation["invite_link"]
                ):
                    await db.set_order_link_revoked_by_invite(
                        reservation["invite_link"]
                    )
                    await db.set_reservation_status(reservation["id"], "expired")
                    if reservation["order_id"]:
                        await db.reconcile_order_expiry(
                            reservation["order_id"]
                        )
                    await db.log_event(
                        "reservation_expired",
                        reservation["chat_id"],
                        reservation["user_id"],
                    )
                else:
                    await db.set_reservation_status(
                        reservation["id"],
                        "expire_revoke_pending",
                        "expiry revoke failed; retry scheduled",
                    )

            for reservation in await db.pending_reservation_revocations(
                time.time() - 300
            ):
                if reservation["status"] == "approving":
                    claimed = await db.claim_reservation_status(
                        reservation["id"],
                        {"approving"},
                        "approved_revoke_pending",
                        "stale approval recovery",
                    )
                    if not claimed:
                        continue
                    reservation = await db.get_reservation(reservation["id"])
                    if reservation is None:
                        continue
                if reservation["status"] == "approved_revoke_pending":
                    await db.set_order_link_joined_by_invite(
                        reservation["invite_link"], reservation["user_id"]
                    )
                    if reservation["order_id"]:
                        await db.set_order_status(
                            reservation["order_id"], "joined_revoke_pending"
                        )
                cancellation = reservation["status"] in {
                    "cancel_requested", "cancel_revoke_pending"
                }
                removed = True
                if cancellation:
                    await db.set_order_link_joined_by_invite(
                        reservation["invite_link"], reservation["user_id"]
                    )
                    removed = await remove_buyer(
                        reservation["chat_id"], reservation["user_id"]
                    )
                    if removed:
                        await db.set_order_link_buyer_removed_by_invite(
                            reservation["invite_link"]
                        )
                if await revoke_link(
                    reservation["chat_id"], reservation["invite_link"]
                ):
                    await db.set_order_link_revoked_by_invite(
                        reservation["invite_link"]
                    )
                    if cancellation and not removed:
                        await db.set_reservation_status(
                            reservation["id"],
                            "cancel_revoke_pending",
                            "buyer removal retry scheduled",
                        )
                        continue
                    terminal = (
                        "cancelled" if cancellation
                        else "expired" if reservation["status"] == "expire_revoke_pending"
                        else "completed"
                    )
                    changed = await db.claim_reservation_status(
                        reservation["id"], {reservation["status"]}, terminal
                    )
                    if changed:
                        await db.log_event(
                            f"reservation_{terminal}",
                            reservation["chat_id"],
                            reservation["user_id"],
                        )
                    if reservation["order_id"]:
                        if terminal == "expired":
                            await db.reconcile_order_expiry(
                                reservation["order_id"]
                            )
                        else:
                            await db.reconcile_order_status(
                                reservation["order_id"]
                            )

            for order_link in await db.pending_order_link_revocations():
                removed = True
                if (
                    order_link["joined_user"]
                    and not order_link["buyer_removed"]
                ):
                    removed = await remove_buyer(
                        order_link["chat_id"], order_link["joined_user"]
                    )
                    if removed:
                        await db.set_order_link_buyer_removed(order_link["id"])
                revoked = bool(order_link["revoked"])
                if not revoked:
                    revoked = await revoke_link(
                        order_link["chat_id"], order_link["invite_link"]
                    )
                if revoked and not order_link["revoked"]:
                    await db.set_order_link_revoked(order_link["id"])
                if revoked and removed:
                    await db.reconcile_order_status(order_link["order_id"])
        except Exception as e:  # noqa: BLE001
            log.warning("revocation retry error: %s: %s", type(e).__name__, e)
        await asyncio.sleep(20)


async def on_shutdown() -> None:
    for task in list(_background_tasks):
        task.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)
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
