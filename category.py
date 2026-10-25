"""Once-per-chat guard for the Business quick reply /category (quickreply.py).

Rules, per chat:
  * The first /category is allowed and remembered.
  * /category again later (more than RESET_WINDOW after it) is a duplicate:
    the new copy is deleted for both sides.
  * /category twice within RESET_WINDOW is a deliberate "undo": BOTH copies
    are deleted and the chat is reset, so the next /category is allowed.

Foreign chats (the user's country isn't India): /category is always
deleted and the /forgein quick reply is sent instead, once per chat.

A quick reply sent from the Telegram app reaches us as ordinary outgoing
messages, so a use is recognised by matching their text/media against the
shortcut's messages (re-read every REFRESH seconds). A multi-message quick
reply counts as one use. State lives in ``data/category.json``.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from telethon.tl.functions.messages import GetQuickRepliesRequest, GetQuickReplyMessagesRequest

RESET_WINDOW = 60      # seconds: two uses this close = delete both + reset
GROUP_WINDOW = 8       # seconds: late messages of one quick-reply send
SIBLING_WINDOW = 5     # seconds around the trigger that belong to the same send
SETTLE = 1.5           # seconds to wait for the rest of a quick reply to arrive
REFRESH = 60           # seconds between re-reads of the shortcut content


def _norm(text) -> str:
    return " ".join((text or "").split())


def _media_key(message):
    media = getattr(message, "media", None)
    for attr in ("photo", "document"):
        obj = getattr(media, attr, None)
        if obj is not None and getattr(obj, "id", None):
            return f"{attr}:{obj.id}"
    return None


def fingerprints(message) -> set:
    keys = set()
    text = _norm(getattr(message, "message", ""))
    if text:
        keys.add("t:" + text)
    media = _media_key(message)
    if media:
        keys.add("m:" + media)
    return keys


class CategoryGuard:
    def __init__(self, client, shortcut: str, path: Path, *, clock=time.time,
                 is_foreign=None, send_foreign=None):
        """``is_foreign(chat_id) -> bool`` and ``send_foreign(chat_id)`` turn on
        the foreign redirect (both async)."""
        self.is_foreign = is_foreign
        self.send_foreign = send_foreign
        self.client = client
        self.shortcut = shortcut.lstrip("/")
        self.path = Path(path)
        self.clock = clock
        self.lock = asyncio.Lock()
        self.prints: list[set] = []        # one fingerprint set per shortcut message
        self.loaded_at = 0.0
        try:
            self.chats = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.chats = {}                # {chat_id: {"ts": first use, "ids": [msg ids]}}
        self.groups: dict = {}             # {chat_id: {"ts", "ids", "action"}} last send
        self.ignore: set = set()           # ids of messages we sent ourselves (/forgein)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.chats), encoding="utf-8")
        tmp.replace(self.path)

    async def refresh(self, force=False) -> bool:
        """Re-read the shortcut's messages. False if the shortcut doesn't exist."""
        if not force and self.prints and self.clock() - self.loaded_at < REFRESH:
            return True
        res = await self.client(GetQuickRepliesRequest(hash=0))
        target = next((q for q in getattr(res, "quick_replies", []) or []
                       if getattr(q, "shortcut", "").lower() == self.shortcut.lower()), None)
        if target is None:
            self.prints = []
            return False
        msgs = await self.client(GetQuickReplyMessagesRequest(
            shortcut_id=target.shortcut_id, hash=0, id=None))
        ordered = sorted(getattr(msgs, "messages", []) or [], key=lambda m: m.id)
        self.prints = [p for p in (fingerprints(m) for m in ordered) if p]
        self.loaded_at = self.clock()
        return True

    def _match(self, message) -> int | None:
        keys = fingerprints(message)
        for index, prints in enumerate(self.prints):
            if keys & prints:
                return index
        return None

    async def _siblings(self, chat_id, message) -> list[int]:
        """Ids of every message of this quick-reply send (all of them, even
        ones whose text/media don't match: e.g. a re-uploaded photo).

        A quick reply's messages are sent together, so they are our outgoing
        messages in this chat within SIBLING_WINDOW seconds of the trigger.
        """
        ids = {message.id}
        date = getattr(message, "date", None)
        if date is None:
            return sorted(ids)
        await asyncio.sleep(SETTLE)  # let the rest of the quick reply arrive
        try:
            recent = await self.client.get_messages(chat_id, limit=max(10, 3 * len(self.prints)))
        except Exception:  # noqa: BLE001
            return sorted(ids)
        for m in recent:
            if (m is not None and getattr(m, "out", False) and m.id not in self.ignore
                    and getattr(m, "date", None) is not None
                    and abs((m.date - date).total_seconds()) <= SIBLING_WINDOW):
                ids.add(m.id)
        return sorted(ids)

    async def on_outgoing(self, chat_id: int, message) -> str | None:
        """Handle one outgoing message. Returns what happened, or None.

        One quick-reply send = ONE use, however many messages it has: the
        first matching message decides, all its messages are handled
        together, and later messages of the same send are ignored.
        """
        if message.id in self.ignore:
            return None
        async with self.lock:
            if message.id in self.ignore:      # sent by us while we held the lock
                return None
            key = str(chat_id)
            group = self.groups.get(key)
            if group and message.id in group["ids"]:
                return None                       # already handled with its group
            if not fingerprints(message):
                return None
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 - keep the last known content
                pass
            if self._match(message) is None:
                return None
            now = self.clock()
            if group and now - group["ts"] <= GROUP_WINDOW:
                # another message of the send we just handled (arrived late)
                group["ids"].add(message.id)
                if group["action"] == "allowed":
                    self.chats[key]["ids"].append(message.id)
                    self.save()
                else:
                    await self._delete(chat_id, [message.id])
                return None
            ids = await self._siblings(chat_id, message)
            record = self.chats.get(key)
            foreign = False
            if self.is_foreign is not None:
                try:
                    foreign = bool(await self.is_foreign(chat_id))
                except Exception:  # noqa: BLE001 - unknown country: normal rules
                    foreign = False
            if foreign:
                await self._delete(chat_id, ids)
                fkey = "f:" + key
                action = "foreign-kept"
                if fkey not in self.chats and self.send_foreign is not None:
                    sent = await self.send_foreign(chat_id) or []
                    self.ignore.update(sent)
                    self.chats[fkey] = {"ts": now, "ids": list(sent)}
                    self.save()
                    action = "foreign"
            elif record is None:
                self.chats[key] = {"ts": now, "ids": ids}
                self.save()
                action = "allowed"
            elif now - record["ts"] <= RESET_WINDOW:
                await self._delete(chat_id, list(record["ids"]) + ids)
                del self.chats[key]
                self.save()
                action = "reset"
            else:
                await self._delete(chat_id, ids)
                action = "duplicate"
            self.groups[key] = {"ts": now, "ids": set(ids), "action": action}
            return action

    async def _delete(self, chat_id, ids) -> None:
        ids = [i for i in dict.fromkeys(ids) if i]
        if ids:
            await self.client.delete_messages(chat_id, ids, revoke=True)

    def forget(self, chat_id: int) -> bool:
        removed = self.chats.pop(str(chat_id), None) is not None
        removed = (self.chats.pop("f:" + str(chat_id), None) is not None) or removed
        if removed:
            self.save()
        return removed
