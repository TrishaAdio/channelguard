"""Timed auto delete for quick replies (quickreply.py). Hardcoded, ON by default.

  * /category and /foreign (/forgein) quick replies: every message they send
    (long texts full of links, zips, files, photos, albums) is deleted for both
    sides a random 60-70 minutes after THE CUSTOMER HAS SEEN IT. Until the
    message is read the clock does not start, so an unread link list stays.
    That includes the /forgein the bot sends itself in foreign chats.
  * In a chat where you sent /add: the first links or files you send after it
    go 3 minutes later, and every later link or file in that chat goes 50
    minutes later. These go on time whether the customer read them or not.
    The /add message itself (the receipt) is never touched.

The only control is ``AUTODEL ON`` / ``AUTODEL OFF`` (any case, optional . or
/ in front), sent in any chat. In a customer's DM the command deletes itself
and the confirmation goes to Saved Messages.

Telegram does not mark a message as coming from a quick reply, so a send is
recognised by matching its text or media against the stored quick replies,
the same way category.py does. Texts shorter than MIN_TEXT never match on
their own, so a quick reply that only says "ok" can't take every "ok" you type
with it. An album counts as a whole once one item matches. After /add it is
the content that counts instead: anything carrying a link or a file.

Reads arrive as outbox read events. Those are missed while the bot is down, so
every chat still waiting is also asked about directly (read_outbox_max_id)
every READ_SYNC seconds, which catches a read that happened in between.

Pending deletions are saved to data/autodel.json, so a restart loses nothing:
whatever fell due while the bot was down goes as soon as it is back. Nothing in
here can break the rest of quickreply.py; every failure is logged, retried a
few times, then dropped.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from pathlib import Path

from telethon.errors import FloodWaitError, MessageDeleteForbiddenError
from telethon.tl import types as tl_types
from telethon.tl.functions.messages import (
    GetPeerDialogsRequest,
    GetQuickRepliesRequest,
    GetQuickReplyMessagesRequest,
)

import category
import ui

# --- hardcoded rules -------------------------------------------------------
QR_SHORTCUTS = ("category", "foreign", "forgein")
QR_MIN, QR_MAX = 60 * 60, 70 * 60    # /category, /foreign: 60-70 min AFTER being read
ADD_FIRST = 3 * 60                   # first links/files after /add: 3 min
ADD_LATER = 50 * 60                  # every later link/file in that chat: 50 min
ADD_BATCH = 15                       # sends this close together count as one
MIN_TEXT = 12                        # shorter texts never match on text alone
READ_SYNC = 120                      # ask Telegram about unread sends this often
REFRESH = 300                        # re-read the quick replies at least this often
MISS_REFRESH = 30                    # an unmatched send re-reads at most this often
FULL_REFRESH = 1800                  # re-read every shortcut's messages this often
MAX_TRIES = 6                        # a deletion that keeps failing is then dropped
BATCH = 100                          # Telegram deletes at most 100 per request

COMMAND_RE = re.compile(r"^[./]?autodel\s+(on|off)$", re.IGNORECASE)


LINK_RE = re.compile(r"(?:https?://|tg://|t\.me/|telegram\.me/)", re.IGNORECASE)
LINK_ENTITIES = (tl_types.MessageEntityUrl, tl_types.MessageEntityTextUrl)


def has_link_or_file(message) -> bool:
    """Whether a message carries a link or a file: what /add mode watches."""
    if getattr(getattr(message, "media", None), "document", None) is not None:
        return True
    if LINK_RE.search(getattr(message, "message", "") or ""):
        return True
    return any(isinstance(e, LINK_ENTITIES) for e in getattr(message, "entities", None) or [])


def _usable(keys: set) -> set:
    """Drop text fingerprints too short to identify a quick reply."""
    return {k for k in keys if not k.startswith("t:") or len(k) - 2 >= MIN_TEXT}


class AutoDelete:
    def __init__(self, client, path: Path, *, clock=time.time, on_deleted=None):
        """``on_deleted(chat_id, ids)`` (async) runs after each successful delete."""
        self.client = client
        self.path = Path(path)
        self.clock = clock
        self.on_deleted = on_deleted
        self.enabled = True
        # {"chat", "ids", "due", "tries", "why"}; due 0 = waiting to be read
        self.pending: list[dict] = []
        self.prints: dict[str, list[set]] = {}   # shortcut -> fingerprints per message
        self.signature: dict[str, tuple] = {}    # shortcut -> (id, top_message, count)
        self.add_chats: dict[int, float] = {}    # chat -> when its first send after /add went
        self.albums: dict[int, str] = {}         # grouped_id -> the rule that claimed it
        self.read_sync_at = 0.0
        self.read_at = 0.0
        self.full_at = 0.0
        self.refresh_lock = asyncio.Lock()
        self.delete_lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.tasks: set = set()
        self._load()

    # -- state -------------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as error:  # noqa: BLE001  a bad file must not stop startup
            broken = self.path.with_name(self.path.name + ".broken")
            try:
                self.path.replace(broken)
            except OSError:
                pass
            ui.warn(f"[autodel] {self.path.name} was unreadable ({error}); kept as {broken.name}")
            return
        self.enabled = bool(data.get("enabled", True))
        for chat, first_at in (data.get("add_chats") or {}).items():
            try:
                self.add_chats[int(chat)] = float(first_at)
            except (TypeError, ValueError):
                continue
        for item in data.get("pending") or []:
            try:
                self.pending.append({
                    "chat": int(item["chat"]),
                    "ids": [int(i) for i in item["ids"]],
                    "due": float(item["due"]),
                    "tries": int(item.get("tries", 0)),
                    "why": str(item.get("why", "qr")),
                })
            except (KeyError, TypeError, ValueError):
                continue

    def save(self) -> None:
        data = {"enabled": self.enabled, "pending": self.pending,
                "add_chats": {str(k): v for k, v in self.add_chats.items()}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        except Exception as error:  # noqa: BLE001
            ui.error(f"[autodel] could not save {self.path.name}: {error}")

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.save()

    @property
    def count(self) -> int:
        return sum(len(item["ids"]) for item in self.pending)

    @property
    def unread_count(self) -> int:
        """How many queued messages are still waiting to be seen."""
        return sum(len(item["ids"]) for item in self.pending if not item["due"])

    # -- quick reply index -------------------------------------------------
    async def refresh(self, force: bool = False) -> None:
        """Re-read the quick replies; only shortcuts that changed are re-fetched."""
        async with self.refresh_lock:
            now = self.clock()
            full = force or now - self.full_at >= FULL_REFRESH
            res = await self.client(GetQuickRepliesRequest(hash=0))
            replies = getattr(res, "quick_replies", None)
            if replies is None:
                self.read_at = now
                return
            prints: dict[str, list[set]] = {}
            signature: dict[str, tuple] = {}
            for reply in replies:
                name = (getattr(reply, "shortcut", "") or "").lower()
                sig = (reply.shortcut_id, getattr(reply, "top_message", 0), getattr(reply, "count", 0))
                signature[name] = sig
                if not full and self.signature.get(name) == sig and name in self.prints:
                    prints[name] = self.prints[name]
                    continue
                msgs = await self.client(GetQuickReplyMessagesRequest(
                    shortcut_id=reply.shortcut_id, hash=0, id=None))
                ordered = sorted(getattr(msgs, "messages", None) or [], key=lambda m: m.id)
                prints[name] = [_usable(category.fingerprints(m)) for m in ordered]
            self.prints, self.signature, self.read_at = prints, signature, now
            if full:
                self.full_at = now

    def _match(self, message) -> str | None:
        """Name of the quick reply this message came from, or None."""
        keys = _usable(category.fingerprints(message))
        if not keys:
            return None
        for name, per_message in self.prints.items():
            if any(keys & shortcut_keys for shortcut_keys in per_message):
                return name
        return None

    # -- watching outgoing messages ---------------------------------------
    def note_add(self, chat_id: int) -> None:
        """/add was sent here: from now on links and files in this chat go on a timer."""
        self.add_chats.setdefault(int(chat_id), 0.0)
        self.save()

    def note_read(self, chat_id: int, max_id: int) -> int:
        """The customer read up to max_id: start the clock on what they saw."""
        started = 0
        for item in self.pending:
            if item["chat"] != int(chat_id) or item["due"] or not item["ids"]:
                continue
            if max(item["ids"]) > int(max_id):
                continue  # not seen yet
            item["due"] = self.clock() + random.uniform(QR_MIN, QR_MAX)
            started += len(item["ids"])
            print(ui.yellow(f"[autodel] {chat_id}: ")
                  + f"seen, {len(item['ids'])} message(s) go in "
                    f"{round((item['due'] - self.clock()) / 60)} min", flush=True)
        if started:
            self.save()
            self.wake.set()
        return started

    def watch(self, event) -> None:
        """Check an outgoing message without delaying anything else."""
        if not self.enabled:
            return
        task = asyncio.create_task(self._watch(event.chat_id, event.message))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _watch(self, chat_id: int, message) -> None:
        try:
            why = self._classify(chat_id, message)
            # Unknown content may be a quick reply that was just created or
            # edited: re-read (cheap, unchanged shortcuts are not re-fetched).
            if why is None and self.clock() - self.read_at >= (MISS_REFRESH if self.prints else 0):
                await self.refresh()
                why = self._classify(chat_id, message)
            if why is None:
                return
            group = getattr(message, "grouped_id", None)
            if group:
                self.albums[group] = why
                if len(self.albums) > 500:
                    self.albums.pop(next(iter(self.albums)))
            self.schedule(chat_id, [message.id], why)
        except Exception as error:  # noqa: BLE001
            ui.error(f"[autodel] {chat_id}: {type(error).__name__}: {error}")

    def _classify(self, chat_id: int, message) -> str | None:
        """Which rule claims this message: "add-first", "add-later" or "qr"."""
        group = getattr(message, "grouped_id", None)
        if group and group in self.albums:
            return self.albums[group]
        chat_id = int(chat_id)
        if chat_id in self.add_chats:
            # After /add the content decides, not the quick reply it came from.
            if not has_link_or_file(message):
                return None
            first_at = self.add_chats.get(chat_id) or 0.0
            now = self.clock()
            if not first_at:
                self.add_chats[chat_id] = now
                self.save()
                return "add-first"
            # The rest of the same send (album or back-to-back) goes with it.
            return "add-first" if now - first_at <= ADD_BATCH else "add-later"
        return "qr" if self._match(message) in QR_SHORTCUTS else None

    # -- scheduling and deleting -------------------------------------------
    def schedule(self, chat_id: int, ids, why: str = "qr") -> None:
        """Queue messages. "qr" waits to be read first; the /add rules do not."""
        ids = [int(i) for i in ids if i]
        if not ids or not self.enabled:
            return
        delay = {"add-first": ADD_FIRST, "add-later": ADD_LATER}.get(why)
        due = 0.0 if delay is None else self.clock() + delay
        self.pending.append({"chat": int(chat_id), "ids": ids, "due": due,
                             "tries": 0, "why": why})
        self.save()
        self.wake.set()
        when = f"in {round(delay / 60)} min" if delay is not None else (
            f"{QR_MIN // 60}-{QR_MAX // 60} min after the customer sees it")
        print(ui.yellow(f"[autodel] {chat_id}: ") + f"{len(ids)} message(s) go {when}",
              flush=True)

    def _drop(self, item: dict) -> None:
        try:
            self.pending.remove(item)
        except ValueError:
            pass

    async def delete_due(self) -> int:
        async with self.delete_lock:
            now = self.clock()
            due = [item for item in self.pending if item["due"] and item["due"] <= now]
            by_chat: dict[int, list[dict]] = {}
            for item in due:
                by_chat.setdefault(item["chat"], []).append(item)
            deleted = 0
            for chat_id, items in by_chat.items():
                ids = sorted({i for item in items for i in item["ids"]})
                try:
                    for start in range(0, len(ids), BATCH):
                        await self._delete(chat_id, ids[start:start + BATCH])
                except FloodWaitError as error:
                    ui.warn(f"[autodel] Telegram asks to wait {error.seconds}s; retrying then")
                    for item in items:
                        item["due"] = self.clock() + error.seconds + 1
                    continue
                except MessageDeleteForbiddenError:
                    ui.warn(f"[autodel] {chat_id}: Telegram won't allow deleting these; dropped")
                    for item in items:
                        self._drop(item)
                    continue
                except Exception as error:  # noqa: BLE001
                    for item in items:
                        item["tries"] += 1
                        if item["tries"] >= MAX_TRIES:
                            ui.error(f"[autodel] {chat_id}: gave up on {item['ids']} ({error})")
                            self._drop(item)
                        else:
                            item["due"] = self.clock() + 120 * item["tries"]
                            ui.warn(f"[autodel] {chat_id}: delete failed ({error}); "
                                    f"retry {item['tries']}/{MAX_TRIES - 1}")
                    continue
                for item in items:
                    self._drop(item)
                deleted += len(ids)
                print(ui.green(f"[autodel] {chat_id}: ") + f"deleted {len(ids)} message(s)", flush=True)
                if self.on_deleted is not None:
                    try:
                        await self.on_deleted(chat_id, ids)
                    except Exception as error:  # noqa: BLE001
                        ui.warn(f"[autodel] {chat_id}: after-delete hook failed ({error})")
            if due:
                self.save()
            return deleted

    async def sync_reads(self) -> int:
        """Ask Telegram which waiting sends have been read since we last looked.

        Read events only arrive while the bot is connected, so a customer who
        opened the chat while it was down would otherwise wait forever.
        """
        chats = {item["chat"] for item in self.pending if not item["due"]}
        if not chats:
            return 0
        started = 0
        for chat_id in sorted(chats):
            try:
                peer = await self.client.get_input_entity(chat_id)
                res = await self.client(GetPeerDialogsRequest(
                    peers=[tl_types.InputDialogPeer(peer=peer)]))
            except FloodWaitError:
                raise
            except Exception as error:  # noqa: BLE001  a gone chat must not stop the rest
                ui.warn(f"[autodel] {chat_id}: couldn't check if it was read ({error})")
                continue
            for dialog in getattr(res, "dialogs", None) or []:
                max_id = getattr(dialog, "read_outbox_max_id", 0) or 0
                if max_id:
                    started += self.note_read(chat_id, max_id)
        return started

    async def _delete(self, chat_id: int, ids: list) -> None:
        try:
            await self.client.delete_messages(chat_id, ids, revoke=True)
        except ValueError:
            # The chat isn't in the session cache (e.g. a fresh session after a
            # restart): load the dialogs once so it can be found, then retry.
            await self.client.get_dialogs()
            await self.client.delete_messages(chat_id, ids, revoke=True)

    # -- background loop ---------------------------------------------------
    async def run(self) -> None:
        """Delete what is due and keep the quick reply index fresh, forever."""
        while True:
            try:
                await self.delete_due()
                if self.enabled and self.clock() - self.read_at >= REFRESH:
                    try:
                        await self.refresh()
                    except FloodWaitError as error:
                        self.read_at = self.clock() + error.seconds
                    except Exception as error:  # noqa: BLE001
                        self.read_at = self.clock()
                        ui.warn(f"[autodel] couldn't read quick replies ({error})")
                if self.clock() - self.read_sync_at >= READ_SYNC:
                    try:
                        await self.sync_reads()
                    except FloodWaitError as error:
                        self.read_sync_at = self.clock() + error.seconds
                    else:
                        self.read_sync_at = self.clock()
                upcoming = min((item["due"] for item in self.pending if item["due"]),
                               default=self.clock() + 60)
                wait = min(max(upcoming - self.clock(), 1.0), 60.0)
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                ui.error(f"[autodel] loop: {type(error).__name__}: {error}; carrying on")
                await asyncio.sleep(30)
