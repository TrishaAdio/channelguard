"""`.setup`: a `.save` backup rebuilds a fresh account (round trip)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from telethon.tl import types as T
from telethon.tl.types import users as TU

import quickreply
import quickreply_extras as extras
import quickreply_restore as restore
from test_quickreply_extras import NOW, Event, jpeg, make_account


class FreshAccount:
    """A brand-new account that applies the update requests it receives."""

    def __init__(self, premium=True):
        self.user = T.User(id=222, is_self=True, access_hash=1, first_name="New", premium=premium)
        self.full = T.UserFull(id=222, settings=T.PeerSettings(), notify_settings=T.PeerNotifySettings(),
                               common_chats_count=0, about="")
        self.shortcuts: dict[str, list] = {}
        self.photos, self.saved, self.calls = [], [], []

    async def get_me(self):
        return self.user

    async def upload_file(self, data, file_name="f"):
        handle = T.InputFile(id=len(self.calls), parts=1, name=file_name, md5_checksum="")
        handle.data = data
        return handle

    async def send_message(self, chat, text, formatting_entities=None):
        self.saved.append(text)
        return SimpleNamespace(id=900 + len(self.saved))

    async def send_file(self, chat, file=None, caption=None, formatting_entities=None):
        self.saved.append(caption)
        return SimpleNamespace(id=900 + len(self.saved))

    async def __call__(self, r):
        name = type(r).__name__
        self.calls.append(name)
        if name == "GetFullUserRequest":
            return TU.UserFull(full_user=self.full, chats=[], users=[self.user])
        if name == "GetQuickRepliesRequest":
            return T.messages.QuickReplies(quick_replies=[
                T.QuickReply(shortcut_id=i + 1, shortcut=n, top_message=0, count=len(m))
                for i, (n, m) in enumerate(self.shortcuts.items())], messages=[], chats=[], users=[])
        if name == "UpdateProfileRequest":
            if r.first_name is not None:
                self.user.first_name, self.user.last_name = r.first_name, r.last_name
            if r.about is not None:
                self.full.about = r.about
        elif name == "UploadProfilePhotoRequest":
            self.photos.append(("fallback" if r.fallback else "video" if r.video else "photo",
                                (r.video or r.file).data))
        elif name in ("SendMessageRequest", "SendMediaRequest"):
            self.shortcuts.setdefault(r.quick_reply_shortcut.shortcut, []).append(r.message)
        elif name == "UploadMediaRequest":
            return T.MessageMediaPhoto(photo=T.Photo(id=5, access_hash=1, file_reference=b"", date=NOW,
                                                     sizes=[], dc_id=1))
        elif name == "UpdateBusinessAwayMessageRequest":
            self.full.business_away_message = r.message
        elif name == "UpdateBusinessLocationRequest":
            self.full.business_location = T.BusinessLocation(address=r.address)
        elif name == "UpdateBirthdayRequest":
            self.full.birthday = r.birthday
        elif name == "UpdateColorRequest":
            self.user.color = r.color
        elif name == "UpdateEmojiStatusRequest":
            self.user.emoji_status = r.emoji_status
        return True


async def nosleep(_):
    return None


def backup_zip(tmp_path):
    client, data, env, greeting = make_account(tmp_path)
    result = asyncio.run(extras.save_everything(client, data_dir=data, env_path=env,
                                                greeting_ref=greeting, now=NOW))
    return result["path"], data


def test_preview_changes_nothing_then_restore_everything(tmp_path):
    path, data = backup_zip(tmp_path)
    fresh = FreshAccount()
    backup = restore.Backup(path)
    report, _ = asyncio.run(restore.run(fresh, backup, dry_run=True, data_dir=data, sleep=nosleep))
    assert fresh.user.first_name == "New" and fresh.shortcuts == {} and fresh.photos == []
    assert "will restore: name" in report.text()
    assert "skipped: username" in report.text() and "skipped: gifts" in report.text()

    pay = {}
    report, greeting = asyncio.run(restore.run(fresh, backup, dry_run=False, data_dir=data,
                                               sleep=nosleep, pay_store=pay))
    assert not report.failed, report.text()
    assert (fresh.user.first_name, fresh.user.last_name, fresh.full.about) == ("Alice", "S", "Bio here")
    assert fresh.full.birthday.day == 1 and fresh.user.color.color == 5
    assert fresh.user.emoji_status.document_id == 42
    assert [kind for kind, _ in fresh.photos] == ["photo", "photo"]
    assert fresh.photos[-1][1] == jpeg("avatar-new")           # current photo uploaded last
    assert fresh.shortcuts == {"demo": ["Join now", "Prices"], "price list": ["x"]}
    assert fresh.full.business_away_message.shortcut_id == 1   # re-linked to the NEW /demo id
    assert fresh.full.business_location.address == "MG Road"
    assert greeting == {"chat_id": 222, "message_id": 901} and fresh.saved == ["Hi!"]
    assert "UpdateUsernameRequest" not in fresh.calls
    assert "payments" not in pay                               # old payment log not copied
    backup.close()


def test_second_run_skips_existing_quick_replies(tmp_path):
    path, data = backup_zip(tmp_path)
    fresh = FreshAccount()
    backup = restore.Backup(path)
    asyncio.run(restore.run(fresh, backup, dry_run=False, data_dir=data, sleep=nosleep))
    report, _ = asyncio.run(restore.run(fresh, backup, dry_run=False, data_dir=data, sleep=nosleep))
    assert "already correct: name" in report.text()
    assert fresh.shortcuts["demo"] == ["Join now", "Prices"]   # not duplicated
    backup.close()


def test_non_premium_target_skips_premium_parts(tmp_path):
    path, data = backup_zip(tmp_path)
    fresh = FreshAccount(premium=False)
    backup = restore.Backup(path)
    report, _ = asyncio.run(restore.run(fresh, backup, dry_run=False, data_dir=data, sleep=nosleep))
    text = report.text()
    assert "skipped: quick replies - needs Telegram Premium" in text
    assert "skipped: business setup" in text and "restored: name" in text
    assert fresh.shortcuts == {}
    backup.close()


def test_one_failure_does_not_stop_the_rest(tmp_path):
    path, data = backup_zip(tmp_path)
    fresh = FreshAccount()
    original = FreshAccount.__call__

    async def flaky(self, r):
        if type(r).__name__ == "UpdateBirthdayRequest":
            raise RuntimeError("BIRTHDAY_INVALID")
        return await original(self, r)

    FreshAccount.__call__ = flaky
    try:
        backup = restore.Backup(path)
        report, _ = asyncio.run(restore.run(fresh, backup, dry_run=False, data_dir=data, sleep=nosleep))
    finally:
        FreshAccount.__call__ = original
    assert report.failed == ["birthday"] and fresh.shortcuts and fresh.user.first_name == "Alice"
    backup.close()


def test_rejects_non_backup_zip(tmp_path):
    import zipfile

    bad = tmp_path / "x.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("a.txt", "hi")
    try:
        restore.Backup(bad)
    except ValueError as error:
        assert "not a QuickReply backup" in str(error)
    else:
        raise AssertionError("accepted a random zip")


def test_setup_command_preview_confirm_flow(tmp_path, monkeypatch):
    path, data = backup_zip(tmp_path)
    fresh = FreshAccount()
    fresh.get_reply_message = None
    monkeypatch.setattr(quickreply, "client", fresh)
    monkeypatch.setattr(quickreply.config, "DATA_DIR", data)
    monkeypatch.setattr(quickreply, "_save_pay", lambda: None)
    saved_greeting = {}
    monkeypatch.setattr(quickreply, "_save_greeting", lambda c, m: saved_greeting.update(chat=c, msg=m))
    quickreply._state["self_id"] = 222
    quickreply._pay = quickreply._default_pay()

    class E(Event):
        async def get_reply_message(self):
            return None

    async def run():
        confirm_early = E(".setup confirm", chat_id=222)
        await quickreply._handle_outgoing(confirm_early)
        assert "Nothing to confirm" in confirm_early.edits[-1]
        preview = E(".setup", chat_id=222)
        await quickreply._handle_outgoing(preview)
        assert "SETUP PREVIEW" in preview.edits[-1] and fresh.user.first_name == "New"
        go = E(".setup confirm", chat_id=222)
        await quickreply._handle_outgoing(go)
        assert go.edits[-1].startswith("SETUP DONE"), go.edits[-1]
        assert fresh.user.first_name == "Alice" and saved_greeting == {"chat": 222, "msg": 901}
        elsewhere = E(".setup", chat_id=999)
        await quickreply._handle_outgoing(elsewhere)
        assert elsewhere.edits == []

    asyncio.run(run())
    assert datetime.now(timezone.utc)


def test_setup_reply_to_zip_applies_immediately(tmp_path, monkeypatch):
    import shutil

    path, data = backup_zip(tmp_path)
    # the forwarded ZIP the operator replies to, stored outside data/backups
    incoming = tmp_path / "forwarded.zip"
    shutil.copy(path, incoming)

    fresh = FreshAccount()

    async def download_media(message, file):
        shutil.copy(incoming, file)      # Telethon writes the replied file to `file`
        return file

    fresh.download_media = download_media
    monkeypatch.setattr(quickreply, "client", fresh)
    monkeypatch.setattr(quickreply.config, "DATA_DIR", data)
    monkeypatch.setattr(quickreply, "_save_pay", lambda: None)
    saved_greeting = {}
    monkeypatch.setattr(quickreply, "_save_greeting", lambda c, m: saved_greeting.update(chat=c, msg=m))
    quickreply._state["self_id"] = 222
    quickreply._pay = quickreply._default_pay()
    quickreply._pending_setup.clear()

    zip_msg = SimpleNamespace(file=SimpleNamespace(name="quickreply_backup_2026-10-20_060000.zip"))

    class E(Event):
        async def get_reply_message(self):
            return zip_msg

    async def run():
        event = E(".setup", chat_id=222)
        await quickreply._handle_outgoing(event)
        # one step: no "SETUP PREVIEW", goes straight to transforming + done
        assert any("Transforming this account" in e for e in event.edits)
        assert event.edits[-1].startswith("SETUP DONE"), event.edits[-1]
        assert fresh.user.first_name == "Alice" and fresh.shortcuts
        assert saved_greeting == {"chat": 222, "msg": 901}
        assert quickreply._pending_setup == {}          # nothing left pending

    asyncio.run(run())


def test_setup_reply_to_non_zip_is_rejected(tmp_path, monkeypatch):
    _path, data = backup_zip(tmp_path)
    fresh = FreshAccount()
    monkeypatch.setattr(quickreply, "client", fresh)
    monkeypatch.setattr(quickreply.config, "DATA_DIR", data)
    quickreply._state["self_id"] = 222
    quickreply._pay = quickreply._default_pay()

    photo_msg = SimpleNamespace(file=SimpleNamespace(name="photo.jpg"))

    class E(Event):
        async def get_reply_message(self):
            return photo_msg

    async def run():
        event = E(".setup", chat_id=222)
        await quickreply._handle_outgoing(event)
        assert "quickreply_backup" in event.edits[-1] and fresh.user.first_name == "New"

    asyncio.run(run())
