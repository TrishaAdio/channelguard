"""`.save` full account backup and payment-screenshot ZIPs for quickreply.py.

Both features only need the Telethon client and the existing ``data/`` folder.
They don't add a database or new dependencies.

.save
    One ZIP (data/backups/quickreply_backup_<stamp>.zip) holding:
    profile (names, username, bio, birthday, colours, emoji status, every
    profile photo incl. video avatars + fallback photo), Business setup
    (hours, location, intro, greeting/away, chat links), every quick reply
    with text, formatting, custom emoji ids, buttons and media files, the
    /set greeting post, and every data/*.json file (payments log,
    templates, greeted users...). Secrets are never included: no session
    file, and API_ID/API_HASH/tokens are stripped from the .env copy.
    The ZIP is verified, then sent to Saved Messages.

Payment screenshots
    payment_screenshots_<YYYY-MM-DD>.zip for one Asia/Kolkata day
    (00:00 inclusive to the next 00:00 exclusive, by the /add time, the
    same day /stats uses). It holds the original proof images plus
    manifest.json. Proofs are downloaded from the customer chat, or from
    the payment-channel post if the original was deleted. Days that ended
    are delivered automatically to SS_CHANNEL (default @ohthatsthepoint);
    each delivery is verified and recorded in data/ss_archive.json so a
    day is never sent twice. Nothing in Telegram is ever deleted.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import tempfile
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from telethon.errors import FloodWaitError
from telethon.tl import types as T
from telethon.tl.functions.account import GetBusinessChatLinksRequest
from telethon.tl.functions.messages import GetQuickRepliesRequest, GetQuickReplyMessagesRequest
from telethon.tl.functions.photos import GetUserPhotosRequest
from telethon.tl.functions.users import GetFullUserRequest

SS_STATUSES = ("valid", "cleared")   # real money; fake/failed/pending are left out
SECRET_ENV = re.compile(r"(API_ID|API_HASH|TOKEN|SECRET|PASSWORD|PASS|KEY|SESSION|MONGO)", re.I)
KEEP_BACKUPS = 10
MAX_MEDIA_BYTES = 200 * 1024 * 1024


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"$base64": base64.b64encode(value).decode("ascii")}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return str(value)


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=_json_default)


def _plain(obj):
    """TL object -> JSON-friendly dict (or None)."""
    if obj is None:
        return None
    return json.loads(json.dumps(obj.to_dict() if hasattr(obj, "to_dict") else obj, default=_json_default))


async def _call(coro_factory, *, attempts: int = 3, sleep=asyncio.sleep):
    """Run a Telegram call, sleeping through short flood waits."""
    for attempt in range(attempts):
        try:
            return await coro_factory()
        except FloodWaitError as error:
            if attempt == attempts - 1 or error.seconds > 600:
                raise
            await sleep(int(error.seconds) + 1)
    return None


def _media_ext(message) -> str:
    media = getattr(message, "media", None)
    if isinstance(media, T.MessageMediaPhoto):
        return ".jpg"
    file = getattr(message, "file", None)
    ext = getattr(file, "ext", None) if file is not None else None
    return ext or ".bin"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "")[:60] or "x"


class _Zip:
    """ZIP writer that tracks what went in, for verification."""

    def __init__(self, path: Path):
        self.path = path
        self.zip = zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED)
        self.files: dict[str, str] = {}

    def add(self, name: str, data: bytes | str, *, compress: bool = True) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.zip.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED)
        self.files[name] = hashlib.sha256(data).hexdigest()

    def close_and_verify(self) -> None:
        self.zip.close()
        with zipfile.ZipFile(self.path) as check:
            bad = check.testzip()
            if bad:
                raise RuntimeError(f"ZIP verification failed at {bad}")
            for name, digest in self.files.items():
                if hashlib.sha256(check.read(name)).hexdigest() != digest:
                    raise RuntimeError(f"ZIP checksum mismatch for {name}")


async def _verified_send(client, target, path: Path, caption: str):
    """Upload a ZIP as a document and confirm Telegram stored the full file."""
    sent = await _call(lambda: client.send_file(target, file=str(path), caption=caption,
                                                force_document=True))
    size = getattr(getattr(sent, "file", None), "size", None)
    if size is not None and int(size) != path.stat().st_size:
        raise RuntimeError(f"Telegram stored {size} bytes, expected {path.stat().st_size}")
    return sent


# ---------------------------------------------------------------------------
# .save
# ---------------------------------------------------------------------------
async def _download(client, obj, **kwargs) -> bytes | None:
    return await _call(lambda: client.download_media(obj, file=bytes, **kwargs))


async def _snapshot_message(client, archive: _Zip, folder: str, message, notes: list) -> dict:
    entry = {
        "id": message.id,
        "date": message.date,
        "text": message.message or "",
        "entities": [_plain(e) for e in (message.entities or [])],
        "custom_emoji_ids": [e.document_id for e in (message.entities or [])
                             if isinstance(e, T.MessageEntityCustomEmoji)],
        "grouped_id": message.grouped_id,
        "reply_markup": _plain(message.reply_markup),
        "media": None,
    }
    media = message.media
    if media is None or isinstance(media, T.MessageMediaWebPage):
        entry["link_preview"] = isinstance(media, T.MessageMediaWebPage)
        return entry
    if isinstance(media, (T.MessageMediaPhoto, T.MessageMediaDocument)):
        size = getattr(getattr(message, "file", None), "size", 0) or 0
        name = f"{folder}/{message.id}{_media_ext(message)}"
        info = {"type": type(media).__name__, "file": name, "size": size,
                "mime_type": getattr(getattr(message, "file", None), "mime_type", None)}
        if size > MAX_MEDIA_BYTES:
            info.update(file=None, error="larger than 200 MB, not copied")
            notes.append(f"{folder}/{message.id}: media too large")
        else:
            try:
                data = await _download(client, message)
                if not data:
                    raise ValueError("empty download")
                archive.add(name, data, compress=False)
                info["sha256"] = hashlib.sha256(data).hexdigest()
            except Exception as error:  # noqa: BLE001
                info.update(file=None, error=f"{type(error).__name__}: {error}")
                notes.append(f"{folder}/{message.id}: {type(error).__name__}")
        entry["media"] = info
    else:
        entry["media"] = {"type": type(media).__name__, "data": _plain(media)}
    return entry


async def _photo(client, archive: _Zip, photo, name: str, notes: list) -> dict:
    entry = {"id": photo.id, "date": photo.date, "file": f"{name}.jpg"}
    data = await _download(client, photo)
    if not data:
        notes.append(f"{name}: download failed")
        return {**entry, "file": None, "error": "download failed"}
    archive.add(entry["file"], data, compress=False)
    videos = [v for v in (photo.video_sizes or []) if isinstance(v, T.VideoSize)]
    if videos:
        video = max(videos, key=lambda v: v.size or 0)
        clip = await _download(client, photo, thumb=video)
        if clip:
            archive.add(f"{name}.mp4", clip, compress=False)
            entry["video_file"] = f"{name}.mp4"
    return entry


def _env_settings(env_path: Path) -> dict:
    out = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and not SECRET_ENV.search(key):
            out[key] = value.strip()
    return out


async def save_everything(client, *, data_dir: Path, env_path: Path, greeting_ref=None,
                          now: datetime | None = None) -> dict:
    """Build + verify the backup ZIP. Returns a result dict (path, counts, notes)."""
    now = now or datetime.now(timezone.utc)
    backups = Path(data_dir) / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    path = backups / f"quickreply_backup_{now:%Y-%m-%d_%H%M%S}.zip"
    archive = _Zip(path)
    notes: list[str] = []
    status: dict[str, str] = {}
    doc: dict = {"format": "quickreply-backup", "version": 1, "created_at": now}

    try:
        me = await _call(client.get_me)
        full = (await _call(lambda: client(GetFullUserRequest(T.InputUserSelf())))).full_user
        birthday = getattr(full, "birthday", None)
        doc["profile"] = {
            "id": me.id, "first_name": me.first_name, "last_name": me.last_name,
            "username": me.username, "usernames": [_plain(u) for u in (me.usernames or [])],
            "about": full.about, "premium": bool(me.premium),
            "birthday": _plain(birthday), "color": _plain(me.color), "profile_color": _plain(me.profile_color),
            "emoji_status": _plain(me.emoji_status),
            "personal_channel_id": getattr(full, "personal_channel_id", None),
        }
        status["profile"] = "saved"

        photos_doc = []
        try:
            result = await _call(lambda: client(GetUserPhotosRequest(
                user_id=T.InputUserSelf(), offset=0, max_id=0, limit=100)))
            current = getattr(getattr(full, "profile_photo", None), "id", None)
            for index, photo in enumerate(p for p in result.photos if isinstance(p, T.Photo)):
                entry = await _photo(client, archive, photo, f"profile_photos/{index + 1:02d}", notes)
                entry["is_current"] = photo.id == current
                photos_doc.append(entry)
            if isinstance(getattr(full, "fallback_photo", None), T.Photo):
                doc["fallback_photo"] = await _photo(client, archive, full.fallback_photo,
                                                     "profile_photos/fallback", notes)
            status["profile_photos"] = f"saved ({len(photos_doc)})"
        except Exception as error:  # noqa: BLE001
            status["profile_photos"] = f"failed: {type(error).__name__}"
            notes.append(f"profile photos: {error}")
        doc["profile_photos"] = photos_doc

        names: dict[int, str] = {}
        shortcuts = []
        try:
            replies = await _call(lambda: client(GetQuickRepliesRequest(hash=0)))
            for order, shortcut in enumerate(replies.quick_replies or []):
                names[shortcut.shortcut_id] = shortcut.shortcut
                result = await _call(lambda s=shortcut: client(GetQuickReplyMessagesRequest(
                    shortcut_id=s.shortcut_id, hash=0, id=None)))
                messages = sorted((m for m in result.messages if isinstance(m, T.Message)), key=lambda m: m.id)
                folder = f"quick_replies/{order + 1:02d}_{_safe(shortcut.shortcut)}"
                shortcuts.append({
                    "name": shortcut.shortcut, "command": "/" + shortcut.shortcut, "order": order,
                    "shortcut_id": shortcut.shortcut_id,
                    "messages": [await _snapshot_message(client, archive, folder, m, notes) for m in messages],
                })
            status["quick_replies"] = f"saved ({len(shortcuts)})"
        except Exception as error:  # noqa: BLE001
            reason = f"{type(error).__name__}: {error}"
            status["quick_replies"] = ("needs Telegram Premium" if "PREMIUM" in reason.upper()
                                       else f"failed: {reason}")
        doc["quick_replies"] = shortcuts

        business = {
            "work_hours": _plain(getattr(full, "business_work_hours", None)),
            "location": _plain(getattr(full, "business_location", None)),
            "intro": _plain(getattr(full, "business_intro", None)),
            "greeting_message": _plain(getattr(full, "business_greeting_message", None)),
            "away_message": _plain(getattr(full, "business_away_message", None)),
        }
        for key in ("greeting_message", "away_message"):
            value = getattr(full, f"business_{key}", None)
            if value is not None and business[key] is not None:
                business[key]["shortcut_name"] = names.get(value.shortcut_id)
        try:
            links = await _call(lambda: client(GetBusinessChatLinksRequest()))
            business["chat_links"] = [_plain(link) for link in links.links or []]
        except Exception as error:  # noqa: BLE001
            business["chat_links"] = []
            notes.append(f"business chat links: {type(error).__name__}")
        doc["business"] = business
        status["business"] = "saved"

        if greeting_ref:
            try:
                message = await _call(lambda: client.get_messages(
                    greeting_ref["chat_id"], ids=greeting_ref["message_id"]))
                if message is None:
                    raise ValueError("the /set greeting post no longer exists")
                doc["greeting_post"] = await _snapshot_message(client, archive, "greeting_post", message, notes)
                status["greeting_post"] = "saved"
            except Exception as error:  # noqa: BLE001
                status["greeting_post"] = f"failed: {error}"
        else:
            status["greeting_post"] = "not set"

        data_files = []
        for file in sorted(Path(data_dir).glob("*.json")):
            try:
                archive.add(f"data/{file.name}", file.read_bytes())
                data_files.append(file.name)
            except OSError as error:
                notes.append(f"data/{file.name}: {error}")
        doc["data_files"] = data_files
        status["data_files"] = f"saved ({len(data_files)})"
        doc["env_settings"] = _env_settings(Path(env_path))
        doc["not_included"] = [
            "Telegram session file and API_ID/API_HASH/tokens (secrets)",
            "saved locations, stories, gifts and privacy rules (not available through the API)",
        ]
        doc["status"] = status
        doc["notes"] = notes
        archive.add("backup.json", _dump(doc))
        archive.close_and_verify()
    except BaseException:
        try:
            archive.zip.close()
        finally:
            path.unlink(missing_ok=True)
        raise

    for old in sorted(backups.glob("quickreply_backup_*.zip"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    complete = not notes and not any(str(v).startswith("failed") for v in status.values())
    return {"path": path, "size": path.stat().st_size, "files": len(archive.files),
            "status": status, "notes": notes, "complete": complete,
            "quick_replies": len(shortcuts), "photos": len(photos_doc), "data_files": len(data_files)}


def backup_caption(result: dict) -> str:
    lines = [("Full backup - complete" if result["complete"] else "Full backup - PARTIAL (see notes)"),
             f"Quick replies: {result['quick_replies']}  |  profile photos: {result['photos']}  |  "
             f"data files: {result['data_files']}"]
    for key, value in result["status"].items():
        if not str(value).startswith("saved"):
            lines.append(f"{key}: {value}")
    for note in result["notes"][:5]:
        lines.append(f"! {note}")
    lines.append("No session or API keys are inside.")
    return "\n".join(lines)[:1000]


# ---------------------------------------------------------------------------
# payment screenshots
# ---------------------------------------------------------------------------
def parse_day(value: str, tz, now: datetime | None = None) -> str:
    value = (value or "").strip().lower()
    today = (now or datetime.now(timezone.utc)).astimezone(tz).date()
    if value in ("", "today"):
        return today.isoformat()
    if value == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("use today, yesterday or YYYY-MM-DD")
    return date.fromisoformat(value).isoformat()


def day_window(day: str, tz) -> tuple[float, float]:
    """Unix [start, end) for one local calendar day."""
    start = datetime.combine(date.fromisoformat(day), time(0, 0), tzinfo=tz)
    end = datetime.combine(date.fromisoformat(day) + timedelta(days=1), time(0, 0), tzinfo=tz)
    return start.timestamp(), end.timestamp()


def payments_for_day(payments, day: str, tz) -> list[dict]:
    start, end = day_window(day, tz)
    out = []
    for payment in payments or []:
        try:
            ts = float(payment.get("ts"))
        except (TypeError, ValueError):
            continue
        if not start <= ts < end or payment.get("status", "valid") not in SS_STATUSES:
            continue
        has_source = payment.get("source_chat_id") and payment.get("source_message_id")
        has_post = payment.get("post_chat_id") and payment.get("post_message_id")
        if has_source or has_post:
            out.append(payment)
    return sorted(out, key=lambda p: float(p["ts"]))


async def _fetch_many(client, chat_id, ids: list[int]) -> dict:
    found = {}
    for offset in range(0, len(ids), 100):
        chunk = ids[offset:offset + 100]
        try:
            messages = await _call(lambda c=chunk: client.get_messages(chat_id, ids=c))
        except Exception:  # noqa: BLE001 - chat not reachable: fall back per item
            continue
        for message_id, message in zip(chunk, messages or [], strict=False):
            if message is not None and getattr(message, "media", None) is not None:
                found[message_id] = message
    return found


async def build_ss_zip(client, payments, day: str, tz, out_dir: Path) -> dict:
    """Download every proof for ``day`` into a verified ZIP inside ``out_dir``."""
    items = payments_for_day(payments, day, tz)
    result = {"day": day, "path": None, "count": 0, "missing": [], "expected": len(items)}
    if not items:
        return result
    sources: dict[tuple, dict] = {}
    for payment in items:
        for kind in ("source", "post"):
            chat, mid = payment.get(f"{kind}_chat_id"), payment.get(f"{kind}_message_id")
            if chat and mid:
                sources.setdefault((kind, int(chat)), {})[int(mid)] = None
    for (kind, chat), wanted in sources.items():
        sources[(kind, chat)] = await _fetch_many(client, chat, sorted(wanted))

    path = Path(out_dir) / f"payment_screenshots_{day}.zip"
    archive = _Zip(path)
    start, end = day_window(day, tz)
    manifest = {"archive_date": day, "timezone": str(tz),
                "window_start": datetime.fromtimestamp(start, tz).isoformat(),
                "window_end_exclusive": datetime.fromtimestamp(end, tz).isoformat(),
                "created_at": datetime.now(timezone.utc).isoformat(), "items": [], "missing": []}
    index = 0
    for payment in items:
        order_id = str(payment.get("order_id") or "")
        message, origin = None, None
        for kind, label in (("source", "original"), ("post", "payment channel post")):
            chat, mid = payment.get(f"{kind}_chat_id"), payment.get(f"{kind}_message_id")
            if chat and mid:
                message = sources.get((kind, int(chat)), {}).get(int(mid))
                if message is not None:
                    origin = label
                    break
        data = None
        if message is not None:
            try:
                data = await _download(client, message)
            except Exception:  # noqa: BLE001
                data = None
        when = datetime.fromtimestamp(float(payment["ts"]), tz)
        if not data:
            manifest["missing"].append({"order_id": order_id, "time": when.isoformat(),
                                        "reason": "screenshot no longer available"})
            continue
        index += 1
        name = f"{index:04d}_{when:%H%M}_{_safe(order_id)}{_media_ext(message)}"
        archive.add(name, data, compress=False)
        manifest["items"].append({"file": name, "order_id": order_id, "amount": payment.get("amount"),
                                  "time": when.isoformat(), "from": origin,
                                  "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    manifest["item_count"] = len(manifest["items"])
    archive.add("manifest.json", _dump(manifest))
    archive.close_and_verify()
    result.update(path=path, count=manifest["item_count"], missing=manifest["missing"],
                  sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size=path.stat().st_size)
    if not manifest["items"]:
        path.unlink(missing_ok=True)
        result["path"] = None
    return result


def ss_caption(result: dict) -> str:
    day = datetime.fromisoformat(result["day"])
    text = (f"payment_screenshots_{result['day']}.zip\n"
            f"{day:%a %d %b %Y} (IST, 00:00-24:00)\n"
            f"Screenshots: {result['count']} of {result['expected']} payment(s)")
    if result["missing"]:
        text += f"\nMissing: {len(result['missing'])} (deleted before download)"
    return text


# ---------------------------------------------------------------------------
# automatic daily delivery state (data/ss_archive.json)
# ---------------------------------------------------------------------------
class SSState:
    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("delivered", {})
        self.data.setdefault("failures", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(_dump(self.data), encoding="utf-8")
        temporary.replace(self.path)

    def ensure_start(self, today: str) -> None:
        if not self.data.get("enabled_since"):
            self.data["enabled_since"] = today
            self.save()

    def due_days(self, today: str, limit: int = 3) -> list[str]:
        start = self.data.get("enabled_since") or today
        day = date.fromisoformat(start)
        out = []
        now_ts = datetime.now(timezone.utc).timestamp()
        while day.isoformat() < today and len(out) < limit:
            key = day.isoformat()
            failure = self.data["failures"].get(key) or {}
            if key not in self.data["delivered"] and float(failure.get("retry_at", 0)) <= now_ts:
                out.append(key)
            day += timedelta(days=1)
        return out

    def mark_delivered(self, day: str, **info) -> None:
        self.data["delivered"][day] = {**info, "at": datetime.now(timezone.utc).isoformat()}
        self.data["failures"].pop(day, None)
        self.save()

    def mark_failed(self, day: str, error: str) -> int:
        failure = self.data["failures"].get(day) or {"attempts": 0}
        failure["attempts"] = int(failure["attempts"]) + 1
        failure["error"] = error[:300]
        failure["retry_at"] = datetime.now(timezone.utc).timestamp() + min(3600, 120 * 2 ** failure["attempts"])
        self.data["failures"][day] = failure
        self.save()
        return failure["attempts"]


async def deliver_day(client, payments, day: str, tz, target, state: SSState) -> str:
    """Build + send one day's ZIP to ``target`` and record it. Returns a status line."""
    with tempfile.TemporaryDirectory(prefix="qr-ss-") as folder:
        result = await build_ss_zip(client, payments, day, tz, Path(folder))
        if result["path"] is None:
            state.mark_delivered(day, count=0, expected=result["expected"], message_id=None)
            return f"{day}: no screenshots"
        sent = await _verified_send(client, target, result["path"], ss_caption(result))
        state.mark_delivered(day, count=result["count"], expected=result["expected"],
                             missing=len(result["missing"]), message_id=getattr(sent, "id", None),
                             sha256=result["sha256"], size=result["size"])
        return f"{day}: {result['count']} screenshot(s) delivered"
