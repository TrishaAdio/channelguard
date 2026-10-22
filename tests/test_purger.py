"""purger.py: reply + .purge deletes that message and everything after it."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telethon.errors import FloodWaitError

import purger


class FakeMessage:
    def __init__(self, mid):
        self.id = mid


class FakeClient:
    def __init__(self, chat_ids, *, fail_first=False):
        # chat_ids: {chat_id: [message ids present, newest first]}
        self.chats = chat_ids
        self.deleted: list = []        # (chat_id, [ids], revoke)
        self.saved: list[str] = []
        self.fail_first = fail_first

    async def iter_messages(self, chat_id, min_id=0):
        for mid in sorted(self.chats.get(chat_id, []), reverse=True):   # newest first
            if mid > min_id:
                yield FakeMessage(mid)

    async def delete_messages(self, chat_id, ids, revoke=False):
        if self.fail_first:
            self.fail_first = False
            raise FloodWaitError(request=None, capture=0)
        self.deleted.append((chat_id, list(ids), revoke))

    async def send_message(self, chat, text):
        self.saved.append(text)


class FakeEvent:
    def __init__(self, chat_id, reply_id):
        self.chat_id = chat_id
        self.raw_text = ".purge"
        self.message = SimpleNamespace(fwd_from=None)
        self._reply = FakeMessage(reply_id) if reply_id else None
        self.edits: list[str] = []

    async def get_reply_message(self):
        return self._reply

    async def edit(self, text):
        self.edits.append(text)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_trigger_only_on_plain_purge_reply():
    ok = SimpleNamespace(raw_text=".purge", message=SimpleNamespace(fwd_from=None))
    assert purger._is_purge_command(ok)
    assert purger._is_purge_command(SimpleNamespace(raw_text="/PURGE ", message=SimpleNamespace(fwd_from=None)))
    assert not purger._is_purge_command(SimpleNamespace(raw_text="purge", message=SimpleNamespace(fwd_from=None)))
    assert not purger._is_purge_command(SimpleNamespace(raw_text=".purge now", message=SimpleNamespace(fwd_from=None)))
    # forwarded .purge must not fire
    assert not purger._is_purge_command(SimpleNamespace(raw_text=".purge", message=SimpleNamespace(fwd_from=object())))


def test_purge_deletes_from_reply_onward_inclusive(monkeypatch):
    client = FakeClient({100: [1, 2, 3, 4, 5, 6]})
    monkeypatch.setattr(purger, "client", client)
    purger._busy = asyncio.Lock()
    event = FakeEvent(chat_id=100, reply_id=3)
    _run(purger.on_purge(event))
    assert client.deleted == [(100, [3, 4, 5, 6], True)]       # 3 inclusive, for everyone
    assert client.saved and "deleted 4 message(s)" in client.saved[0]


def test_purge_requires_a_reply(monkeypatch):
    client = FakeClient({100: [1, 2]})
    monkeypatch.setattr(purger, "client", client)
    purger._busy = asyncio.Lock()
    event = FakeEvent(chat_id=100, reply_id=None)
    _run(purger.on_purge(event))
    assert client.deleted == [] and "Reply to a message" in event.edits[0]


def test_large_chat_is_deleted_in_batches(monkeypatch):
    client = FakeClient({7: list(range(1, 251))})
    monkeypatch.setattr(purger, "client", client)
    purger._busy = asyncio.Lock()
    _run(purger.on_purge(FakeEvent(chat_id=7, reply_id=1)))
    sizes = [len(ids) for _chat, ids, _revoke in client.deleted]
    assert sizes == [100, 100, 50]                             # 250 ids -> 3 batches
    assert sum(sizes) == 250


def test_flood_wait_is_retried(monkeypatch):
    client = FakeClient({9: [1, 2, 3]}, fail_first=True)
    monkeypatch.setattr(purger, "client", client)

    async def _noop(_seconds):
        return None

    monkeypatch.setattr(purger.asyncio, "sleep", _noop)
    purger._busy = asyncio.Lock()
    _run(purger.on_purge(FakeEvent(chat_id=9, reply_id=1)))
    assert client.deleted == [(9, [1, 2, 3], True)]            # retried after the flood wait


def test_concurrent_purge_is_rejected(monkeypatch):
    client = FakeClient({1: [1]})
    monkeypatch.setattr(purger, "client", client)
    purger._busy = asyncio.Lock()

    async def scenario():
        await purger._busy.acquire()                           # pretend one is running
        event = FakeEvent(chat_id=1, reply_id=1)
        await purger.on_purge(event)
        assert "already running" in event.edits[0]
        purger._busy.release()

    _run(scenario())
    assert client.deleted == []
