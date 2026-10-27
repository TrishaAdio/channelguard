"""Timed auto delete for quick replies (quickreply.py). Hardcoded, ON by default.

  * /category and /foreign (/forgein) quick replies: every message they send
    (long texts full of links, zips, files, photos, albums) is deleted for both
    sides a random 1-2 hours later. That includes the /forgein the bot sends
    itself in foreign chats.
  * After /add: any quick reply you send in that chat in the next 10 minutes
    is deleted 30 minutes later. The /add receipt itself is never touched.

The only control is ``AUTODEL ON`` / ``AUTODEL OFF`` (any case, optional . or
/ in front), sent in any chat. In a customer's DM the command deletes itself
and the confirmation goes to Saved Messages.

Telegram does not mark a message as coming from a quick reply, so a send is
recognised by matching its text or media against the stored quick replies,
the same way category.py does. Texts shorter than MIN_TEXT never match on
their own, so a quick reply that only says "ok" can't take every "ok" you type
with it. An album counts as a whole once one item matches.

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
from telethon.tl.functions.messages import GetQuickRepliesRequest, GetQuickReplyMessagesRequest

import category
import ui

# --- hardcoded rules -------------------------------------------------------
QR_SHORTCUTS = ("category", "foreign", "forgein")
QR_MIN, QR_MAX = 60 * 60, 120 * 60   # /category, /foreign: deleted after 1-2 h
ADD_DELAY = 30 * 60                  # quick replies after /add: deleted after 30 min
ADD_WINDOW = 10 * 60                 # ...if sent within 10 min of the /add
MIN_TEXT = 12                        # shorter texts never match on text alone
REFRESH = 300                        # re-read the quick replies at least this often
MISS_REFRESH = 30                    # an unmatched send re-reads at most this often
FULL_REFRESH = 1800                  # re-read every shortcut's messages this often
MAX_TRIES = 6                        # a deletion that keeps failing is then dropped
BATCH = 100                          # Telegram deletes at most 100 per request

COMMAND_RE = re.compile(r"^[./]?autodel\s+(on|off)$", re.IGNORECASE)


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
        self.pending: list[dict] = []        # {"chat", "ids", "due", "tries", "why"}
        self.prints: dict[str, list[set]] = {}   # shortcut -> fingerprints per message
        self.signature: dict[str, tuple] = {}    # shortcut -> (id, top_message, count)
        self.add_until: dict[int, float] = {}    # chat -> end of its /add window
        self.albums: dict[int, str] = {}         # grouped_id -> "qr" | "add"
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
        data = {"enabled": self.enabled, "pending": self.pending}
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
        """/add was sent here: quick replies for the next ADD_WINDOW go after 30 min."""
        self.add_until[int(chat_id)] = self.clock() + ADD_WINDOW

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

    def _in_add_window(self, chat_id: int) -> bool:
        return self.add_until.get(int(chat_id), 0) > self.clock()

    def _classify(self, chat_id: int, message) -> str | None:
        group = getattr(message, "grouped_id", None)
        if group and group in self.albums:
            return self.albums[group]
        name = self._match(message)
        if name is None:
            return None
        if self._in_add_window(chat_id):
            return "add"
        if name in QR_SHORTCUTS:
            return "qr"
        return None

    # -- scheduling and deleting -------------------------------------------
    def schedule(self, chat_id: int, ids, why: str = "qr") -> None:
        ids = [int(i) for i in ids if i]
        if not ids or not self.enabled:
            return
        delay = ADD_DELAY if why == "add" else random.uniform(QR_MIN, QR_MAX)
        self.pending.append({"chat": int(chat_id), "ids": ids, "due": self.clock() + delay,
                             "tries": 0, "why": why})
        self.save()
        self.wake.set()
        minutes = round(delay / 60)
        print(ui.yellow(f"[autodel] {chat_id}: ") + f"{len(ids)} message(s) go in {minutes} min",
              flush=True)

    def _drop(self, item: dict) -> None:
        try:
            self.pending.remove(item)
        except ValueError:
            pass

    async def delete_due(self) -> int:
        async with self.delete_lock:
            now = self.clock()
            due = [item for item in self.pending if item["due"] <= now]
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
                upcoming = min((item["due"] for item in self.pending), default=self.clock() + 60)
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
