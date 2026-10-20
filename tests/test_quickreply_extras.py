"""`.save` backup and payment-screenshot ZIPs (quickreply_extras + wiring)."""
from __future__ import annotations

import asyncio
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon.tl import types as T
from telethon.tl.types import users as TU

import quickreply
import quickreply_extras as extras

IST = quickreply.IST
NOW = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
_ids = iter(range(10_000, 99_999))


def jpeg(tag: str) -> bytes:
    return b"\xff\xd8\xff\xe0" + tag.encode() + b"\x00" * 40 + b"\xff\xd9"


def ist(day, hour, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=IST).timestamp()


class FakeClient:
    def __init__(self):
        self.media: dict[int, bytes] = {}
        self.chats: dict[int, dict[int, T.Message]] = {}
        self.sent: list = []
        self.fail_send = None
        self.user = T.User(id=111, is_self=True, access_hash=1, first_name="Alice", last_name="S",
                           username="alice", premium=True, phone="919999999999",
                           color=T.PeerColor(color=5, background_emoji_id=7),
                           emoji_status=T.EmojiStatus(document_id=42))
        self.full = T.UserFull(id=111, settings=T.PeerSettings(), notify_settings=T.PeerNotifySettings(),
                               common_chats_count=0, about="Bio here",
                               birthday=T.Birthday(day=1, month=2, year=1999),
                               business_location=T.BusinessLocation(address="MG Road"))
        self.photos = []
        self.shortcuts = []   # (name, id, [messages])

    # builders ------------------------------------------------------------
    def photo(self, data: bytes) -> T.Photo:
        pid = next(_ids)
        self.media[pid] = data
        return T.Photo(id=pid, access_hash=1, file_reference=b"", date=NOW,
                       sizes=[T.PhotoSize(type="y", w=10, h=10, size=len(data))], dc_id=1)

    def message(self, chat, data: bytes | None, text="", mid=None):
        media = T.MessageMediaPhoto(photo=self.photo(data)) if data else None
        msg = T.Message(id=mid or next(_ids), peer_id=T.PeerUser(chat), date=NOW, message=text, media=media,
                        entities=[T.MessageEntityBold(offset=0, length=len(text))] if text else None)
        self.chats.setdefault(chat, {})[msg.id] = msg
        return msg

    # telethon surface ----------------------------------------------------
    async def get_me(self):
        return self.user

    async def __call__(self, request):
        name = type(request).__name__
        if name == "GetFullUserRequest":
            return TU.UserFull(full_user=self.full, chats=[], users=[self.user])
        if name == "GetUserPhotosRequest":
            return T.photos.Photos(photos=list(self.photos), users=[])
        if name == "GetQuickRepliesRequest":
            return T.messages.QuickReplies(quick_replies=[
                T.QuickReply(shortcut_id=sid, shortcut=n, top_message=0, count=len(m))
                for n, sid, m in self.shortcuts], messages=[], chats=[], users=[])
        if name == "GetQuickReplyMessagesRequest":
            msgs = next(m for _n, sid, m in self.shortcuts if sid == request.shortcut_id)
            return T.messages.Messages(messages=msgs, chats=[], users=[])
        if name == "GetBusinessChatLinksRequest":
            return T.account.BusinessChatLinks(links=[], chats=[], users=[])
        raise NotImplementedError(name)

    async def download_media(self, obj, file=bytes, thumb=None):
        media = getattr(obj, "media", obj)
        photo = getattr(media, "photo", media)
        return self.media.get(getattr(photo, "id", None))

    async def get_messages(self, chat, ids=None):
        pool = self.chats.get(chat, {})
        if isinstance(ids, list):
            return [pool.get(i) for i in ids]
        return pool.get(ids)

    async def send_file(self, target, file=None, caption=None, force_document=False):
        if self.fail_send:
            raise self.fail_send
        size = Path(file).stat().st_size
        self.sent.append({"target": target, "data": Path(file).read_bytes(), "name": Path(file).name,
                          "caption": caption})
        return SimpleNamespace(id=next(_ids), file=SimpleNamespace(size=size))


def payment(order, ts, chat=None, mid=None, post=None, status="valid"):
    p = {"order_id": order, "amount": "100.00", "ts": ts, "status": status}
    if chat:
        p.update(source_chat_id=chat, source_message_id=mid)
    if post:
        p.update(post_chat_id=post[0], post_message_id=post[1])
    return p


# ---------------------------------------------------------------------------
# screenshots
# ---------------------------------------------------------------------------
def test_day_window_and_selection_use_ist_midnights():
    pays = [payment("A", ist(20, 0, 0), 5, 1), payment("B", ist(20, 23, 59, 59), 5, 2),
            payment("C", ist(21, 0, 0), 5, 3), payment("D", ist(19, 23, 59), 5, 4),
            payment("E", ist(20, 12), 5, 5, status="fake"), payment("F", ist(20, 12), 5, 6, status="cleared"),
            payment("G", ist(20, 13))]                       # no screenshot at all
    chosen = [p["order_id"] for p in extras.payments_for_day(pays, "2026-10-20", IST)]
    assert chosen == ["A", "F", "B"]
    start, end = extras.day_window("2026-10-20", IST)
    assert end - start == 86400
    assert datetime.fromtimestamp(start, timezone.utc) == datetime(2026, 10, 19, 18, 30, tzinfo=timezone.utc)


def test_parse_day():
    now = datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc)      # 00:30 IST on the 21st
    assert extras.parse_day("", IST, now) == "2026-10-21"
    assert extras.parse_day("yesterday", IST, now) == "2026-10-20"
    assert extras.parse_day("2026-10-01", IST, now) == "2026-10-01"


def test_zip_has_originals_channel_fallback_and_reports_missing(tmp_path):
    client = FakeClient()
    a = client.message(500, jpeg("a"))
    post = client.message(-1009, jpeg("posted-copy"))
    pays = [payment("ANIA", ist(20, 9), 500, a.id),
            payment("ANIB", ist(20, 10), 500, 9999, post=(-1009, post.id)),   # original deleted
            payment("ANIC", ist(20, 11), 500, 8888)]                          # gone everywhere
    result = asyncio.run(extras.build_ss_zip(client, pays, "2026-10-20", IST, tmp_path))
    assert result["count"] == 2 and result["expected"] == 3 and len(result["missing"]) == 1
    with zipfile.ZipFile(result["path"]) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("manifest.json"))
        names = sorted(n for n in archive.namelist() if n != "manifest.json")
        assert names == ["0001_0900_ANIA.jpg", "0002_1000_ANIB.jpg"]
        assert archive.read(names[0]) == jpeg("a") and archive.read(names[1]) == jpeg("posted-copy")
    assert manifest["archive_date"] == "2026-10-20" and manifest["item_count"] == 2
    assert [i["from"] for i in manifest["items"]] == ["original", "payment channel post"]
    assert manifest["missing"][0]["order_id"] == "ANIC"
    assert "2 of 3" in extras.ss_caption(result) and "Missing: 1" in extras.ss_caption(result)


def test_daily_delivery_once_per_day_with_retry(tmp_path, monkeypatch):
    client = FakeClient()
    m1, m2 = client.message(500, jpeg("1")), client.message(500, jpeg("2"))
    pays = [payment("X1", ist(18, 10), 500, m1.id), payment("X2", ist(19, 10), 500, m2.id)]
    state = extras.SSState(tmp_path / "ss_archive.json")
    state.ensure_start("2026-10-18")
    assert state.due_days("2026-10-20") == ["2026-10-18", "2026-10-19"]   # today never included

    client.fail_send = ConnectionError("network down")
    try:
        asyncio.run(extras.deliver_day(client, pays, "2026-10-18", IST, "@ch", state))
    except ConnectionError as error:
        assert state.mark_failed("2026-10-18", str(error)) == 1
    assert state.due_days("2026-10-20") == ["2026-10-19"]                 # 18th waits for retry
    client.fail_send = None
    for day in state.due_days("2026-10-20"):
        asyncio.run(extras.deliver_day(client, pays, day, IST, "@ch", state))
    assert [s["name"] for s in client.sent] == ["payment_screenshots_2026-10-19.zip"]
    state.data["failures"]["2026-10-18"]["retry_at"] = 0
    for day in state.due_days("2026-10-20"):
        asyncio.run(extras.deliver_day(client, pays, day, IST, "@ch", state))
    assert len(client.sent) == 2 and state.due_days("2026-10-21") == ["2026-10-20"]
    reloaded = extras.SSState(tmp_path / "ss_archive.json")
    assert set(reloaded.data["delivered"]) == {"2026-10-18", "2026-10-19"}
    assert reloaded.data["failures"] == {}


def test_empty_day_is_recorded_without_sending(tmp_path):
    client = FakeClient()
    state = extras.SSState(tmp_path / "s.json")
    line = asyncio.run(extras.deliver_day(client, [], "2026-10-18", IST, "@ch", state))
    assert "no screenshots" in line and client.sent == [] and "2026-10-18" in state.data["delivered"]


# ---------------------------------------------------------------------------
# .save
# ---------------------------------------------------------------------------
def make_account(tmp_path):
    client = FakeClient()
    client.photos = [client.photo(jpeg("avatar-new")), client.photo(jpeg("avatar-old"))]
    client.full.profile_photo = client.photos[0]
    demo = [client.message(111, None, "Join now"), client.message(111, jpeg("price"), "Prices")]
    client.shortcuts = [("demo", 1, demo), ("price list", 2, [client.message(111, None, "x")])]
    client.full.business_away_message = T.BusinessAwayMessage(
        shortcut_id=1, schedule=T.BusinessAwayMessageScheduleAlways(),
        recipients=T.BusinessRecipients(new_chats=True))
    greeting = client.message(111, jpeg("greet"), "Hi!")
    data = tmp_path / "data"
    data.mkdir()
    (data / "pay.json").write_text('{"payments": [{"order_id": "A"}]}')
    (data / "greeted.json").write_text("[1, 2]")
    env = tmp_path / ".env"
    env.write_text("API_ID=1\nAPI_HASH=deadbeef\nBOT_TOKEN=123:abc\nSHORTCUT=demo\nPAYMENT_CHANNEL=-100\n")
    (tmp_path / "quickreply.session").write_bytes(b"secret session")
    return client, data, env, {"chat_id": 111, "message_id": greeting.id}


def test_save_everything_builds_a_complete_verified_zip(tmp_path):
    client, data, env, greeting = make_account(tmp_path)
    result = asyncio.run(extras.save_everything(client, data_dir=data, env_path=env,
                                                greeting_ref=greeting, now=NOW))
    assert result["complete"] and result["quick_replies"] == 2 and result["photos"] == 2
    with zipfile.ZipFile(result["path"]) as archive:
        names = set(archive.namelist())
        doc = json.loads(archive.read("backup.json"))
        everything = b"".join(archive.read(n) for n in names)
    assert {"profile_photos/01.jpg", "profile_photos/02.jpg", "data/pay.json", "data/greeted.json"} <= names
    assert any(n.startswith("quick_replies/01_demo/") and n.endswith(".jpg") for n in names)
    assert any(n.startswith("greeting_post/") for n in names)
    assert doc["profile"]["first_name"] == "Alice" and doc["profile"]["about"] == "Bio here"
    assert doc["profile"]["birthday"]["day"] == 1 and doc["profile"]["emoji_status"]["document_id"] == 42
    assert "phone" not in doc["profile"]
    assert [s["command"] for s in doc["quick_replies"]] == ["/demo", "/price list"]
    first = doc["quick_replies"][0]["messages"][0]
    assert first["text"] == "Join now" and first["entities"][0]["_"] == "MessageEntityBold"
    assert doc["business"]["away_message"]["shortcut_name"] == "demo"
    assert doc["business"]["location"]["address"] == "MG Road"
    assert doc["profile_photos"][0]["is_current"] is True
    # secrets never included
    assert doc["env_settings"] == {"SHORTCUT": "demo", "PAYMENT_CHANNEL": "-100"}
    assert b"deadbeef" not in everything and b"123:abc" not in everything
    assert b"secret session" not in everything and b"919999999999" not in everything


class PhotosDown(FakeClient):
    async def __call__(self, request):
        if type(request).__name__ == "GetUserPhotosRequest":
            raise RuntimeError("photos down")
        return await super().__call__(request)


def test_failed_part_marks_backup_partial(tmp_path):
    client, data, env, _greeting = make_account(tmp_path)
    client.__class__ = PhotosDown
    result = asyncio.run(extras.save_everything(client, data_dir=data, env_path=env, now=NOW))
    assert not result["complete"] and result["status"]["profile_photos"].startswith("failed")
    assert "PARTIAL" in extras.backup_caption(result)
    with zipfile.ZipFile(result["path"]) as archive:      # the rest is still saved
        assert "data/pay.json" in archive.namelist()


def test_old_backups_are_pruned(tmp_path):
    client, data, env, _greeting = make_account(tmp_path)
    for minute in range(12):
        asyncio.run(extras.save_everything(client, data_dir=data, env_path=env,
                                           now=NOW + timedelta(minutes=minute)))
    assert len(list((data / "backups").glob("*.zip"))) == extras.KEEP_BACKUPS


# ---------------------------------------------------------------------------
# quickreply.py wiring
# ---------------------------------------------------------------------------
class Event:
    def __init__(self, text, chat_id=111):
        self.raw_text = text
        self.chat_id = chat_id
        self.id = next(_ids)
        self.is_private = True
        self.message = SimpleNamespace(media=None, fwd_from=None, entities=None)
        self.edits = []

    async def edit(self, text, **kwargs):
        self.edits.append(text)


def test_commands_run_from_saved_messages_only(tmp_path, monkeypatch):
    client, data, env, _greeting = make_account(tmp_path)
    m = client.message(500, jpeg("proof"))
    monkeypatch.setattr(quickreply, "client", client)
    monkeypatch.setattr(quickreply.config, "DATA_DIR", data)
    monkeypatch.setattr(quickreply.config, "ENV_PATH", env)
    monkeypatch.setattr(quickreply, "_load_greeting", lambda: None)
    quickreply._state["self_id"] = 111
    quickreply._pay = quickreply._default_pay()
    quickreply._pay["payments"] = [payment("ANIZ", datetime.now(IST).timestamp(), 500, m.id)]

    async def run():
        elsewhere = Event(".save", chat_id=999)
        await quickreply._handle_outgoing(elsewhere)
        assert client.sent == [] and elsewhere.edits == []
        save = Event(".save")
        await quickreply._handle_outgoing(save)
        assert client.sent[-1]["target"] == "me" and client.sent[-1]["name"].startswith("quickreply_backup_")
        assert save.edits[-1].startswith("Backup complete")
        zip_event = Event(".sszip today")
        await quickreply._handle_outgoing(zip_event)
        assert client.sent[-1]["name"].startswith("payment_screenshots_")
        assert "1 screenshot(s) zipped" in zip_event.edits[-1]
        bad = Event(".sszip 2026-99-99")
        await quickreply._handle_outgoing(bad)
        assert "Usage: .sszip" in bad.edits[-1]

    asyncio.run(run())
    assert "<code>.save</code>" in quickreply.HELP_TEXT and "<code>.sszip" in quickreply.HELP_TEXT
