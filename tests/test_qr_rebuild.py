"""`.qrrebuild`: back up, delete all quick replies, recreate with one first."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telethon.tl import types as T

import quickreply_restore as restore
from test_quickreply_setup import _msg, _write_backup


class RebuildAccount:
    """Fake that models delete + recreate so creation order is observable."""

    def __init__(self, names):
        self._next = 1
        self.ids = {}
        self.order = []          # live shortcut names, in order
        self.messages = {}       # name -> [texts]
        self.deleted = []
        self.reordered = None
        for n in names:
            self._add(n)

    def _add(self, name):
        self.ids[name] = 100 + self._next
        self._next += 1
        self.order.append(name)
        self.messages.setdefault(name, [])

    async def get_me(self):
        return T.User(id=1, is_self=True, access_hash=1, first_name="Me", premium=True)

    async def upload_file(self, data, file_name="f"):
        return SimpleNamespace(data=data, name=file_name)

    async def __call__(self, request):
        name = type(request).__name__
        if name == "GetQuickRepliesRequest":
            return T.messages.QuickReplies(
                quick_replies=[T.QuickReply(shortcut_id=self.ids[n], shortcut=n, top_message=0,
                                            count=len(self.messages[n])) for n in self.order],
                messages=[], chats=[], users=[])
        if name == "DeleteQuickReplyShortcutRequest":
            victim = next(n for n, i in self.ids.items() if i == request.shortcut_id)
            self.deleted.append(victim)
            self.order.remove(victim)
            del self.ids[victim]
            self.messages.pop(victim, None)
            return True
        if name in ("SendMessageRequest", "SendMediaRequest"):
            sc = request.quick_reply_shortcut.shortcut
            if sc not in self.ids:
                self._add(sc)
            self.messages[sc].append(request.message)
            return T.Updates(updates=[], users=[], chats=[], date=None, seq=0)
        if name == "ReorderQuickRepliesRequest":
            id_to_name = {i: n for n, i in self.ids.items()}
            self.reordered = [id_to_name[i] for i in request.order]
            self.order = list(self.reordered)   # client stores it
            return True
        raise NotImplementedError(name)


LIVE = ["w", "category", "forgein", "qr3", "payments"]


def _backup(tmp_path, names=LIVE):
    return _write_backup(tmp_path, [
        {"name": n, "command": f"/{n}", "order": i, "shortcut_id": i,
         "messages": [_msg(f"{n} text")]} for i, n in enumerate(names)])


async def _nosleep(_):
    return None


def test_rebuild_plan_puts_named_first_keeps_rest_order(tmp_path):
    backup = restore.Backup(_backup(tmp_path))
    assert restore.rebuild_plan(backup, ["category"]) == ["category", "w", "forgein", "qr3", "payments"]
    assert restore.rebuild_plan(backup, ["category", "forgein"])[:2] == ["category", "forgein"]
    assert restore.rebuild_plan(backup, ["nope"]) == LIVE          # unknown ignored
    backup.close()


def test_rebuild_deletes_all_then_recreates_category_first(tmp_path):
    client = RebuildAccount(LIVE)
    backup = restore.Backup(_backup(tmp_path))
    report, final = asyncio.run(restore.rebuild_quick_replies(client, backup, ["category"], sleep=_nosleep))
    backup.close()
    assert sorted(client.deleted) == sorted(LIVE)                  # every old one deleted
    assert final[0] == "category"                                  # recreated first -> leads /
    assert set(final) == set(LIVE)                                 # nothing lost
    assert client.messages["category"] == ["category text"]        # content preserved
    assert not report.failed


def test_rebuild_recreates_in_creation_order_before_reorder(tmp_path):
    client = RebuildAccount(LIVE)
    backup = restore.Backup(_backup(tmp_path))
    asyncio.run(restore.rebuild_quick_replies(client, backup, ["category", "payments"], sleep=_nosleep))
    backup.close()
    # category + payments created first, in that order, then the rest
    assert client.reordered[:2] == ["category", "payments"]


def test_rebuild_reports_failure_but_keeps_going(tmp_path):
    client = RebuildAccount(LIVE)
    original = RebuildAccount.__call__

    async def flaky(self, request):
        if type(request).__name__ == "SendMessageRequest" and \
                request.quick_reply_shortcut.shortcut == "qr3":
            raise RuntimeError("MEDIA_INVALID")
        return await original(self, request)

    RebuildAccount.__call__ = flaky
    try:
        backup = restore.Backup(_backup(tmp_path))
        report, final = asyncio.run(
            restore.rebuild_quick_replies(client, backup, ["category"], sleep=_nosleep))
        backup.close()
    finally:
        RebuildAccount.__call__ = original
    assert "quick reply /qr3" in report.failed                     # the one bad shortcut
    assert "category" in final and final[0] == "category"          # the rest still rebuilt


def test_qrrebuild_command_backup_then_confirm(tmp_path, monkeypatch):
    import quickreply
    from test_quickreply_extras import Event

    client = RebuildAccount(LIVE)
    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    quickreply.client = client
    quickreply._state["self_id"] = 1
    quickreply._pending_rebuild.clear()
    monkeypatch.setattr(quickreply.config, "DATA_DIR", data)
    monkeypatch.setattr(quickreply, "_load_greeting", lambda: None)

    # stub the heavy full-account backup with a quick-reply-only backup zip
    made = {}

    async def fake_save(cl, *, data_dir, env_path, greeting_ref=None):
        path = _backup(data_dir, LIVE)      # writes into data/backups
        made["path"] = path
        return {"path": path, "complete": True}

    monkeypatch.setattr(quickreply.extras, "save_everything", fake_save)

    async def run():
        preview = Event(".qrrebuild category", chat_id=1)
        await quickreply._handle_outgoing(preview)
        assert any("DELETE all 5 quick replies" in e for e in preview.edits)
        assert any("1. /category" in e for e in preview.edits) and quickreply._pending_rebuild
        confirm = Event(".qrrebuild confirm", chat_id=1)
        await quickreply._handle_outgoing(confirm)
        assert any("QUICK REPLIES REBUILT" in e for e in confirm.edits)
        assert client.order[0] == "category" and not quickreply._pending_rebuild

    asyncio.run(run())


def test_qrrebuild_cancel_changes_nothing(tmp_path, monkeypatch):
    import quickreply
    from test_quickreply_extras import Event

    client = RebuildAccount(LIVE)
    quickreply.client = client
    quickreply._state["self_id"] = 1
    quickreply._pending_rebuild.update(path="x", firsts=["category"], until=9e18)

    async def run():
        event = Event(".qrrebuild cancel", chat_id=1)
        await quickreply._handle_outgoing(event)
        assert any("cancelled" in e.lower() for e in event.edits)
        assert not quickreply._pending_rebuild and client.deleted == []

    asyncio.run(run())
