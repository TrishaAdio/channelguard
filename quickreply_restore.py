"""`.setup`: rebuild a fresh account from a `.save` backup ZIP.

Restored: names, bio, birthday, name/profile colour, emoji status, every
profile photo (incl. video avatars, current one last so it stays current),
the fallback photo, every quick reply (text, formatting, custom emoji,
photos/files/albums, original order), Business hours/location/intro/
greeting/away (re-linked to the new quick-reply ids)/chat links, the /set
greeting post, and the bot's templates + payment channel from pay.json.

Deliberately NOT restored: the username and gifts (they belong to the old
account), the payment log and greeted list (they describe the old
account's chats), inline buttons (user accounts cannot send them).

Every step is reported as restored / already correct / skipped / failed;
a failed step never stops the rest.
"""
from __future__ import annotations

import base64
import json
import random
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from telethon import utils as tl_utils
from telethon.errors import FloodWaitError
from telethon.tl import types as T
from telethon.tl.functions.account import (
    CreateBusinessChatLinkRequest,
    UpdateBirthdayRequest,
    UpdateBusinessAwayMessageRequest,
    UpdateBusinessGreetingMessageRequest,
    UpdateBusinessIntroRequest,
    UpdateBusinessLocationRequest,
    UpdateBusinessWorkHoursRequest,
    UpdateColorRequest,
    UpdateEmojiStatusRequest,
    UpdateProfileRequest,
)
from telethon.tl.functions.messages import (
    GetQuickRepliesRequest,
    SendMediaRequest,
    SendMessageRequest,
    SendMultiMediaRequest,
    UploadMediaRequest,
)
from telethon.tl.functions.photos import UploadProfilePhotoRequest

PREMIUM = "needs Telegram Premium on this account"
PAY_KEYS = ("done_template", "done_entities", "done_ref", "channel_template", "channel_entities",
            "post_channel")


# ---------------------------------------------------------------------------
# backup loading
# ---------------------------------------------------------------------------
class Backup:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.zip = zipfile.ZipFile(self.path)
        try:
            self.doc = json.loads(self.zip.read("backup.json"))
        except KeyError:
            raise ValueError("not a QuickReply backup (no backup.json inside)") from None
        if self.doc.get("format") != "quickreply-backup":
            raise ValueError("not a QuickReply backup")

    def file(self, name: str | None) -> bytes | None:
        if not name:
            return None
        try:
            return self.zip.read(name)
        except KeyError:
            return None

    def close(self) -> None:
        self.zip.close()


def newest_backup(data_dir: Path) -> Path | None:
    found = sorted((Path(data_dir) / "backups").glob("quickreply_backup_*.zip"))
    return found[-1] if found else None


def tl(value):
    """Rebuild TL objects from the ``to_dict`` JSON stored by `.save`."""
    if isinstance(value, list):
        return [tl(v) for v in value]
    if isinstance(value, dict):
        if set(value) == {"$base64"}:
            return base64.b64decode(value["$base64"])
        kwargs = {k: tl(v) for k, v in value.items() if k != "_"}
        name = value.get("_")
        if not name:
            return kwargs
        cls = getattr(T, name, None)
        if cls is None:
            raise ValueError(f"unknown type {name}")
        import inspect

        allowed = set(inspect.signature(cls.__init__).parameters) - {"self"}
        return cls(**{k: v for k, v in kwargs.items() if k in allowed})
    if isinstance(value, str) and len(value) >= 19 and value[4:5] == "-" and value[10:11] == "T":
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
class Report:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.rows: list[tuple[str, str, str]] = []

    def add(self, item: str, status: str, detail: str = "") -> None:
        if status == "restored" and self.dry_run:
            status = "will restore"
        self.rows.append((item, status, detail))

    def fail(self, item: str, error) -> None:
        self.add(item, "FAILED", error if isinstance(error, str) else f"{type(error).__name__}: {error}")

    @property
    def failed(self) -> list[str]:
        return [item for item, status, _ in self.rows if status == "FAILED"]

    def text(self) -> str:
        order = {"FAILED": 0, "will restore": 1, "restored": 1, "already correct": 2, "skipped": 3}
        lines = []
        for item, status, detail in sorted(self.rows, key=lambda r: order.get(r[1], 9)):
            lines.append(f"{status}: {item}" + (f" - {detail}" if detail else ""))
        return "\n".join(lines)


def _rid() -> int:
    return random.randrange(-(2 ** 63), 2 ** 63)


async def _call(client, request, sleep):
    for attempt in range(3):
        try:
            return await client(request)
        except FloodWaitError as error:
            if attempt == 2 or error.seconds > 600:
                raise
            await sleep(int(error.seconds) + 1)
    return None


# ---------------------------------------------------------------------------
# quick replies
# ---------------------------------------------------------------------------
async def _input_media(client, backup: Backup, media: dict | None):
    if not media:
        return None
    data = backup.file(media.get("file"))
    kind = media.get("type")
    if kind in ("MessageMediaPhoto", "MessageMediaDocument"):
        if data is None:
            raise ValueError(media.get("error") or "media file missing from the backup")
        name = Path(media["file"]).name
        uploaded = await client.upload_file(data, file_name=name)
        if kind == "MessageMediaPhoto":
            return T.InputMediaUploadedPhoto(file=uploaded)
        return T.InputMediaUploadedDocument(
            file=uploaded, mime_type=media.get("mime_type") or "application/octet-stream",
            attributes=[T.DocumentAttributeFilename(file_name=name)])
    raw = media.get("data") or {}
    geo = raw.get("geo") or {}
    if kind == "MessageMediaGeo" and geo:
        return T.InputMediaGeoPoint(geo_point=T.InputGeoPoint(lat=geo["lat"], long=geo["long"]))
    if kind == "MessageMediaVenue" and geo:
        return T.InputMediaVenue(geo_point=T.InputGeoPoint(lat=geo["lat"], long=geo["long"]),
                                 title=raw.get("title", ""), address=raw.get("address", ""),
                                 provider=raw.get("provider", ""), venue_id=raw.get("venue_id", ""),
                                 venue_type=raw.get("venue_type", ""))
    if kind == "MessageMediaContact":
        return T.InputMediaContact(phone_number=raw.get("phone_number", ""), first_name=raw.get("first_name", ""),
                                   last_name=raw.get("last_name", ""), vcard=raw.get("vcard", ""))
    raise ValueError(f"{kind} can't be recreated")


def _entities(items) -> list:
    out = []
    for item in items or []:
        try:
            entity = tl(item)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(entity, T.MessageEntityMentionName):
            continue  # needs the user's access hash on this account
        out.append(entity)
    return out


def _groups(messages):
    group = []
    for message in messages:
        gid = message.get("grouped_id")
        if group and (not gid or gid != group[-1].get("grouped_id")):
            yield group
            group = []
        group.append(message)
    if group:
        yield group


async def _send_shortcut(client, backup: Backup, name: str, messages: list, sleep) -> list[str]:
    notes = []
    shortcut = T.InputQuickReplyShortcut(shortcut=name)
    for group in _groups(messages):
        if any(m.get("reply_markup") for m in group):
            notes.append("buttons dropped (user accounts can't send them)")
        if len(group) > 1:
            singles = []
            for message in group:
                media = await _input_media(client, backup, message.get("media"))
                uploaded = await _call(client, UploadMediaRequest(peer=T.InputPeerSelf(), media=media), sleep)
                singles.append(T.InputSingleMedia(media=tl_utils.get_input_media(uploaded), random_id=_rid(),
                                                  message=message.get("text") or "",
                                                  entities=_entities(message.get("entities")) or None))
            await _call(client, SendMultiMediaRequest(peer=T.InputPeerSelf(), multi_media=singles,
                                                      quick_reply_shortcut=shortcut), sleep)
            continue
        message = group[0]
        entities = _entities(message.get("entities"))
        media = await _input_media(client, backup, message.get("media"))
        for attempt in (entities, [e for e in entities if not isinstance(e, T.MessageEntityCustomEmoji)]):
            try:
                if media is None:
                    request = SendMessageRequest(peer=T.InputPeerSelf(), message=message.get("text") or "",
                                                 entities=attempt or None, random_id=_rid(),
                                                 no_webpage=not message.get("link_preview"),
                                                 quick_reply_shortcut=shortcut)
                else:
                    request = SendMediaRequest(peer=T.InputPeerSelf(), media=media,
                                               message=message.get("text") or "", entities=attempt or None,
                                               random_id=_rid(), quick_reply_shortcut=shortcut)
                await _call(client, request, sleep)
                if len(attempt) != len(entities):
                    notes.append("custom emoji dropped (rejected by Telegram)")
                break
            except Exception as error:  # noqa: BLE001
                if attempt is not entities or "EMOJI" not in str(error).upper():
                    raise
    return notes


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
async def run(client, backup: Backup, *, dry_run: bool, data_dir: Path, sleep, pay_store: dict | None = None):
    """Apply ``backup`` to the logged-in account. Returns ``(report, greeting_ref)``."""
    from telethon.tl.functions.users import GetFullUserRequest

    doc = backup.doc
    report = Report(dry_run)
    profile = doc.get("profile") or {}
    me = await client.get_me()
    full = (await client(GetFullUserRequest(T.InputUserSelf()))).full_user
    premium = bool(getattr(me, "premium", False))
    greeting_ref = None

    async def step(item, check, apply, *, needs_premium=False):
        try:
            if needs_premium and not premium:
                report.add(item, "skipped", PREMIUM)
                return
            if check():
                report.add(item, "already correct")
                return
            if not dry_run:
                await apply()
            report.add(item, "restored")
        except Exception as error:  # noqa: BLE001
            report.fail(item, error)

    report.add("username", "skipped", "stays with the old account - set a new one in Telegram")
    report.add("gifts", "skipped", "belong to the old account and can't be moved")

    first, last, about = profile.get("first_name") or "", profile.get("last_name") or "", profile.get("about") or ""
    await step("name", lambda: (me.first_name or "", me.last_name or "") == (first, last),
               lambda: _call(client, UpdateProfileRequest(first_name=first, last_name=last), sleep))
    if about:
        await step("bio", lambda: (full.about or "") == about,
                   lambda: _call(client, UpdateProfileRequest(about=about), sleep))
    birthday = profile.get("birthday")
    if birthday:
        await step("birthday", lambda: getattr(full, "birthday", None) is not None and (
            full.birthday.day, full.birthday.month) == (birthday.get("day"), birthday.get("month")),
            lambda: _call(client, UpdateBirthdayRequest(birthday=T.Birthday(
                day=birthday["day"], month=birthday["month"], year=birthday.get("year"))), sleep))
    for key, for_profile in (("color", None), ("profile_color", True)):
        value = profile.get(key)
        if value and value.get("color") is not None:
            live = getattr(me, key, None)
            # Layer 200+: UpdateColorRequest takes a PeerColor object.
            await step(key.replace("_", " "), lambda v=value, lv=live: lv is not None
                       and getattr(lv, "color", None) == v["color"]
                       and getattr(lv, "background_emoji_id", None) == v.get("background_emoji_id"),
                       lambda v=value, fp=for_profile: _call(client, UpdateColorRequest(
                           for_profile=fp, color=T.PeerColor(
                               color=v["color"], background_emoji_id=v.get("background_emoji_id"))), sleep),
                       needs_premium=True)
    status = profile.get("emoji_status")
    if status and status.get("document_id"):
        await step("emoji status", lambda: getattr(me.emoji_status, "document_id", None) == status["document_id"],
                   lambda: _call(client, UpdateEmojiStatusRequest(
                       emoji_status=T.EmojiStatus(document_id=status["document_id"])), sleep), needs_premium=True)

    # profile photos: oldest first, current one last
    photos = [p for p in doc.get("profile_photos") or [] if p.get("file")]
    if photos:
        ordered = sorted(photos, key=lambda p: (bool(p.get("is_current")), p.get("date") or ""))

        async def upload_photos():
            for entry in ordered:
                clip = backup.file(entry.get("video_file"))
                if clip:
                    request = UploadProfilePhotoRequest(video=await client.upload_file(clip, file_name="a.mp4"))
                else:
                    request = UploadProfilePhotoRequest(file=await client.upload_file(
                        backup.file(entry["file"]), file_name="a.jpg"))
                await _call(client, request, sleep)
                await sleep(1)

        await step(f"profile photos ({len(photos)})", lambda: False, upload_photos)
    fallback = doc.get("fallback_photo") or {}
    if backup.file(fallback.get("file")):
        await step("public fallback photo", lambda: False,
                   lambda: _upload_fallback(client, backup, fallback, sleep))

    # quick replies
    shortcuts = doc.get("quick_replies") or []
    new_ids: dict[str, int] = {}
    if shortcuts:
        if not premium:
            report.add("quick replies", "skipped", PREMIUM)
        else:
            existing = {q.shortcut: q.shortcut_id for q in
                        (await client(GetQuickRepliesRequest(hash=0))).quick_replies or []}
            for shortcut in shortcuts:
                item = f"quick reply {shortcut['command']}"
                if shortcut["name"] in existing:
                    report.add(item, "already correct", "already exists - left unchanged")
                    continue
                try:
                    notes = [] if dry_run else await _send_shortcut(
                        client, backup, shortcut["name"], shortcut["messages"], sleep)
                    report.add(item, "restored", "; ".join(dict.fromkeys(notes)))
                    if not dry_run:
                        await sleep(1)
                except Exception as error:  # noqa: BLE001
                    report.fail(item, error)
            if not dry_run:
                new_ids = {q.shortcut: q.shortcut_id for q in
                           (await client(GetQuickRepliesRequest(hash=0))).quick_replies or []}

    # business
    business = doc.get("business") or {}
    if any(business.get(k) for k in ("work_hours", "location", "intro", "greeting_message", "away_message",
                                     "chat_links")):
        if not premium:
            report.add("business setup", "skipped", PREMIUM)
        else:
            await _business(client, report, business, new_ids, dry_run, sleep)

    # greeting post -> Saved Messages
    post = doc.get("greeting_post")
    if post:
        try:
            if not dry_run:
                media = await _input_media(client, backup, post.get("media"))
                entities = _entities(post.get("entities")) or None
                if media is not None:
                    sent = await client.send_file("me", file=media, caption=post.get("text") or "",
                                                  formatting_entities=entities)
                else:
                    sent = await client.send_message("me", post.get("text") or "", formatting_entities=entities)
                greeting_ref = {"chat_id": int(me.id), "message_id": int(sent.id)}
            report.add("/set greeting post", "restored", "posted in Saved Messages and set as the greeting")
        except Exception as error:  # noqa: BLE001
            report.fail("/set greeting post", error)

    # payment templates + channel
    pay_raw = backup.file("data/pay.json")
    if pay_raw and pay_store is not None:
        try:
            old = json.loads(pay_raw)
            values = {k: old[k] for k in PAY_KEYS if k in old and k != "done_ref"}
            if not dry_run:
                pay_store.update(values)
            report.add("payment templates + channel", "restored",
                       "payment log and greeted list are not copied (they belong to the old account)")
        except Exception as error:  # noqa: BLE001
            report.fail("payment templates + channel", error)

    if not dry_run:
        me = await client.get_me()
        if (me.first_name or "", me.last_name or "") != (first, last):
            report.fail("name", "Telegram shows a different name after the update")
    return report, greeting_ref


async def _upload_fallback(client, backup, fallback, sleep):
    uploaded = await client.upload_file(backup.file(fallback["file"]), file_name="fallback.jpg")
    await _call(client, UploadProfilePhotoRequest(fallback=True, file=uploaded), sleep)


async def _business(client, report, business, new_ids, dry_run, sleep):
    async def attempt(item, factory):
        try:
            if not dry_run:
                await _call(client, factory(), sleep)
            report.add(item, "restored")
        except Exception as error:  # noqa: BLE001
            report.fail(item, error)

    if business.get("work_hours"):
        hours = dict(business["work_hours"])
        hours.pop("open_now", None)
        await attempt("business hours", lambda: UpdateBusinessWorkHoursRequest(business_work_hours=tl(hours)))
    location = business.get("location")
    if location:
        geo = location.get("geo_point") or {}
        point = T.InputGeoPoint(lat=geo["lat"], long=geo["long"]) if geo.get("lat") is not None else None
        await attempt("business location", lambda: UpdateBusinessLocationRequest(
            geo_point=point, address=location.get("address")))
    intro = business.get("intro")
    if intro:
        await attempt("business intro", lambda: UpdateBusinessIntroRequest(intro=T.InputBusinessIntro(
            title=intro.get("title") or "", description=intro.get("description") or "")))
    for key, label in (("greeting_message", "business greeting"), ("away_message", "business away message")):
        saved = business.get(key)
        if not saved:
            continue
        name = saved.get("shortcut_name")
        shortcut_id = new_ids.get(name) if not dry_run else 0
        if not dry_run and shortcut_id is None:
            report.fail(label, f"quick reply /{name} wasn't restored")
            continue
        recipients = dict(saved.get("recipients") or {})
        recipients.pop("_", None)
        recipients.pop("users", None)   # user ids from the old account aren't reachable
        flags = {k: recipients.get(k) for k in ("existing_chats", "new_chats", "contacts",
                                                 "non_contacts", "exclude_selected")}
        inputs = T.InputBusinessRecipients(**flags)
        if key == "greeting_message":
            await attempt(label, lambda: UpdateBusinessGreetingMessageRequest(message=T.InputBusinessGreetingMessage(
                shortcut_id=shortcut_id, recipients=inputs, no_activity_days=saved.get("no_activity_days") or 7)))
        else:
            await attempt(label, lambda: UpdateBusinessAwayMessageRequest(message=T.InputBusinessAwayMessage(
                shortcut_id=shortcut_id, schedule=tl(saved.get("schedule")), recipients=inputs,
                offline_only=saved.get("offline_only"))))
    for link in business.get("chat_links") or []:
        await attempt(f"chat link {link.get('title') or ''}".strip(), lambda link=link: CreateBusinessChatLinkRequest(
            link=T.InputBusinessChatLink(message=link.get("message") or "",
                                         entities=_entities(link.get("entities")) or None,
                                         title=link.get("title"))))


def describe(backup: Backup) -> str:
    doc = backup.doc
    profile = doc.get("profile") or {}
    created = str(doc.get("created_at") or "")[:16].replace("T", " ")
    return (f"Backup of {profile.get('first_name', '')} {profile.get('last_name') or ''}".rstrip()
            + f" (id {profile.get('id')}), made {created} UTC\n"
            f"{len(doc.get('quick_replies') or [])} quick replies, "
            f"{len(doc.get('profile_photos') or [])} profile photos")


__all__ = ["Backup", "newest_backup", "run", "describe", "timezone"]
