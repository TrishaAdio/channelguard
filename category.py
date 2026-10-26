"""Once-per-chat guard for the Business quick reply /category (quickreply.py).

Rules, per chat:
  * The first /category is allowed and remembered.
  * /category again later (more than RESET_WINDOW after it) is a duplicate:
    the new copy is deleted for both sides.
  * /category twice within RESET_WINDOW: only the 2nd copy is deleted (the
    first always stays), and the next /category after that is allowed.

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
ATTACH_WINDOW = 15     # seconds: the rest of a send must arrive within this
REFRESH = 60           # seconds between re-reads of the shortcut content
MISS_REFRESH = 5       # an unmatched message forces a re-read at most this often


def _norm(text) -> str:
    return " ".join((text or "").split())


def _media_keys(message) -> set:
    """Media identity that survives a re-send: the id when Telegram keeps it,
    plus the content shape (photo resolution + bytes, file size + type),
    because a quick reply that was edited/re-created is sent with new ids."""
    media = getattr(message, "media", None)
    keys = set()
    photo = getattr(media, "photo", None)
    if photo is not None:
        if getattr(photo, "id", None):
            keys.add(f"photo:{photo.id}")
        best = None
        for size in getattr(photo, "sizes", None) or []:
            w, h = getattr(size, "w", 0), getattr(size, "h", 0)
            nbytes = getattr(size, "size", None) or max(getattr(size, "sizes", None) or [0])
            if w and h and (best is None or w * h > best[0] * best[1]):
                best = (w, h, nbytes)
        if best:
            keys.add("photoshape:%dx%d:%d" % best)
    document = getattr(media, "document", None)
    if document is not None:
        if getattr(document, "id", None):
            keys.add(f"doc:{document.id}")
        if getattr(document, "size", None):
            keys.add(f"docshape:{document.size}:{getattr(document, 'mime_type', '')}")
    return keys


def fingerprints(message) -> set:
    keys = set()
    text = _norm(getattr(message, "message", ""))
    if text:
        keys.add("t:" + text)
    keys |= {"m:" + k for k in _media_keys(message)}
    return keys


def describe(message) -> str:
    text = _norm(getattr(message, "message", ""))
    media = getattr(message, "media", None)
    kind = ("photo" if getattr(media, "photo", None) is not None else
            "file/video" if getattr(media, "document", None) is not None else
            type(media).__name__ if media is not None else "text")
    return kind + (f' "{text[:30]}"' if text else "")


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
        self.contents: list[str] = []      # human description, for .categoryguard
        self.loaded_at = 0.0
        try:
            self.chats = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.chats = {}                # {chat_id: {"ts": first use, "ids": [msg ids]}}
        self.groups: dict = {}             # {chat_id: {"ts", "ids", "action", "remaining"}}
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
        self.prints = [fingerprints(m) for m in ordered]   # index = position in the reply
        self.contents = [describe(m) for m in ordered]
        self.loaded_at = self.clock()
        return True

    def _match(self, message) -> int | None:
        keys = fingerprints(message)
        for index, prints in enumerate(self.prints):
            if keys & prints:
                return index
        return None

    async def _earlier(self, chat_id, message, count: int) -> list[int]:
        """The ``count`` outgoing messages just before ``message`` (the start
        of a send whose first message(s) weren't recognised)."""
        try:
            recent = await self.client.get_messages(chat_id, limit=count + 5, max_id=message.id)
        except Exception:  # noqa: BLE001
            return []
        date = getattr(message, "date", None)
        found = []
        for m in recent:
            if len(found) >= count:
                break
            if (m is None or not getattr(m, "out", False) or m.id in self.ignore
                    or date is None or getattr(m, "date", None) is None
                    or (date - m.date).total_seconds() > ATTACH_WINDOW):
                break
            found.append(m.id)
        return sorted(found)

    async def on_outgoing(self, chat_id: int, message) -> str | None:
        """Handle one outgoing message. Returns the action for a new send, or None.

        A send is the quick reply's messages in order. The reply has N
        messages, so after a send starts, exactly its next N-1 outgoing
        messages in that chat belong to it (whatever their content); the
        reply's FIRST message always starts a new send. No time guessing,
        so sending /category several times quickly is counted correctly.
        """
        if message.id in self.ignore:
            return None
        async with self.lock:
            if message.id in self.ignore:      # sent by us while we held the lock
                return None
            key = str(chat_id)
            now = self.clock()
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 - keep the last known content
                pass
            index = self._match(message)
            if index is None and fingerprints(message) and now - self.loaded_at >= MISS_REFRESH:
                try:  # maybe the quick reply was just edited
                    await self.refresh(force=True)
                    index = self._match(message)
                except Exception:  # noqa: BLE001
                    pass
            current = self.groups.get(key)
            if (current and current["remaining"] > 0 and index != 0
                    and now - current["ts"] <= ATTACH_WINDOW):
                # next message of the send in progress: same fate as its first
                current["remaining"] -= 1
                current["ids"].append(message.id)
                record = self.chats.get(key)
                if current["action"] == "allowed" and record is not None:
                    record["ids"].append(message.id)
                    self.save()
                elif current["action"] != "allowed":
                    await self._delete(chat_id, [message.id])
                return None
            if index is None:
                return None                     # an ordinary message
            ids = [message.id]
            if index > 0:                       # its first message(s) weren't recognised
                ids = await self._earlier(chat_id, message, index) + ids
            remaining = max(0, len(self.prints) - index - 1)
            action = await self._decide(chat_id, key, ids, now)
            self.groups[key] = {"ts": now, "ids": ids, "action": action, "remaining": remaining}
            return action

    async def _decide(self, chat_id, key, ids, now) -> str:
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
            if fkey in self.chats or self.send_foreign is None:
                return "foreign-kept"
            sent = await self.send_foreign(chat_id) or []
            self.ignore.update(sent)
            self.chats[fkey] = {"ts": now, "ids": list(sent)}
            self.save()
            return "foreign"
        if record is None:
            self.chats[key] = {"ts": now, "ids": list(ids)}
            self.save()
            return "allowed"
        if record.get("allow_next"):
            # the send after a quick repeat is allowed; the first one stays too
            record["ids"] = list(record["ids"]) + list(ids)
            record.pop("allow_next", None)
            record["ts"] = now
            self.save()
            return "allowed"
        await self._delete(chat_id, ids)  # the first send is never deleted
        if now - record["ts"] <= RESET_WINDOW:
            record["allow_next"] = True
            self.save()
            return "reset"
        return "duplicate"

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
