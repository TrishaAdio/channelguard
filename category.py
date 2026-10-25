"""Once-per-chat guard for the Business quick reply /category (quickreply.py).

Rules, per chat:
  * The first /category is allowed and remembered.
  * /category again later (more than RESET_WINDOW after it) is a duplicate:
    the new copy is deleted for both sides.
  * /category twice within RESET_WINDOW is a deliberate "undo": BOTH copies
    are deleted and the chat is reset, so the next /category is allowed.

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
GROUP_WINDOW = 8       # seconds: messages of one multi-message quick reply
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
    def __init__(self, client, shortcut: str, path: Path, *, clock=time.time):
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
        self.groups: dict = {}             # {chat_id: {"ts", "ids", "seen", "action"}} live use

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

    async def on_outgoing(self, chat_id: int, message) -> str | None:
        """Handle one outgoing message. Returns what happened, or None."""
        if not fingerprints(message):
            return None
        async with self.lock:
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 - keep the last known content
                pass
            index = self._match(message)
            if index is None:
                return None
            now = self.clock()
            key = str(chat_id)
            group = self.groups.get(key)
            if group and now - group["ts"] <= GROUP_WINDOW and index not in group["seen"]:
                group["seen"].add(index)          # next message of the same use
                group["ids"].append(message.id)
                action = group["action"]
                if action == "allowed":
                    self.chats[key]["ids"].append(message.id)
                    self.save()
                    return "allowed"
                await self._delete(chat_id, [message.id])
                return action
            # a new use
            record = self.chats.get(key)
            if record is None:
                self.chats[key] = {"ts": now, "ids": [message.id]}
                self.save()
                action = "allowed"
            elif now - record["ts"] <= RESET_WINDOW:
                await self._delete(chat_id, list(record["ids"]) + [message.id])
                del self.chats[key]
                self.save()
                action = "reset"
            else:
                await self._delete(chat_id, [message.id])
                action = "duplicate"
            self.groups[key] = {"ts": now, "ids": [message.id], "seen": {index}, "action": action}
            return action

    async def _delete(self, chat_id, ids) -> None:
        await self.client.delete_messages(chat_id, ids, revoke=True)

    def forget(self, chat_id: int) -> bool:
        removed = self.chats.pop(str(chat_id), None) is not None
        if removed:
            self.save()
        return removed
