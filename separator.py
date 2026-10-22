"""DM country separator for quickreply.py (``.separator on|off`` and ``.gdpa``).

* Country of a private chat: Telegram's own ``PeerSettings.phone_country``
  (what the app shows on the "Not a contact" card), falling back to the
  phone number when it is visible (contacts who share their number).
* Foreign (non-India) DMs are added to a chat folder named ``Videshi``. When
  Telegram refuses another chat in a full folder, ``Videshi 2``, ``Videshi 3``,
  ... are created. Indian DMs, and DMs whose country can't be determined, are
  left where they are.

State is kept in ``data/separator.json`` so each user is looked up and filed
only once.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import time
from pathlib import Path

from telethon import errors
from telethon.tl.functions.messages import (
    GetDialogFiltersRequest,
    GetPeerSettingsRequest,
    UpdateDialogFilterRequest,
)
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import DialogFilter, InputPeerUser, TextWithEntities

try:
    import phonenumbers
except ImportError:  # optional: only needed for contacts without phone_country
    phonenumbers = None

HOME_COUNTRY = "IN"
FOLDER_TITLE = "Videshi"
_FOLDER_RE = re.compile(rf"^{FOLDER_TITLE}(?:\s+(\d+))?$", re.IGNORECASE)
# Unknown countries are re-checked at most this often (Telegram may start
# reporting phone_country a little later, e.g. once the chat has messages).
UNKNOWN_RETRY_SECONDS = 5 * 60
# Folders whose title contains this word lose every chat filed into Videshi.
UNREAD_WORD = "unread"
# Telegram service notifications.
_SKIP_IDS = {777000}
# Chats per folder before Telegram refuses more (200 with Premium). A refusal
# at or above this size starts the next "Videshi N" folder.
FOLDER_FULL_AT = 100


class SeparatorState:
    """{users: {id: {country, source, checked}}, filed: {id: folder title}}."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.users: dict = data.get("users") or {}
        self.filed: dict = data.get("filed") or {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"users": self.users, "filed": self.filed},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def cached_country(self, user_id: int):
        """(hit, iso2|None). An unknown result is only trusted for a while."""
        entry = self.users.get(str(user_id))
        if not entry:
            return False, None
        if entry.get("country") is None and time.time() - entry.get("checked", 0) > UNKNOWN_RETRY_SECONDS:
            return False, None
        return True, entry.get("country")

    def remember(self, user_id: int, country, source: str) -> None:
        self.users[str(user_id)] = {"country": country, "source": source, "checked": int(time.time())}


def _country_from_phone(phone: str | None):
    if not phone:
        return None
    digits = phone.lstrip("+")
    if phonenumbers is not None:
        try:
            number = phonenumbers.parse("+" + digits)
            # Unallocated/unusual numbers have no exact region; the calling
            # code's main country (+44 -> GB) is still right for our purpose.
            region = (phonenumbers.region_code_for_number(number)
                      or phonenumbers.region_code_for_country_code(number.country_code))
        except phonenumbers.NumberParseException:
            region = None
        if region and region != "ZZ":
            return region
    # Without phonenumbers we can still tell India apart.
    return HOME_COUNTRY if digits.startswith("91") else None


def _iso2(code) -> str | None:
    code = (code or "").strip().upper()
    return code if len(code) == 2 and code.isalpha() else None


async def detect_country(client, peer, user=None) -> tuple[str | None, str]:
    """(ISO2 country or None, how it was found) for a private-chat user.

    Tries, in order: the chat's peer settings (the "Not a contact" card),
    the full user profile's settings (same field, other request), and the
    phone number when the user shares it.
    """
    try:
        result = await client(GetPeerSettingsRequest(peer=peer))
        code = _iso2(getattr(result.settings, "phone_country", None))
        if code:
            return code, "telegram"
    except errors.RPCError:
        pass
    try:
        full = await client(GetFullUserRequest(peer))
        code = _iso2(getattr(getattr(full.full_user, "settings", None), "phone_country", None))
        if code:
            return code, "telegram"
        for u in getattr(full, "users", None) or []:
            if getattr(u, "id", None) == _peer_key(peer) and getattr(u, "phone", None):
                user = u
    except errors.RPCError:
        pass
    if user is None or not getattr(user, "phone", None):
        try:
            user = await client.get_entity(peer)
        except (ValueError, TypeError, errors.RPCError):
            pass
    country = _country_from_phone(getattr(user, "phone", None))
    return (country, "phone") if country else (None, "unknown")


async def country_for(client, state: SeparatorState, peer, user_id: int, user=None, *, fresh=False):
    """Cached detect_country; ``fresh`` forces a new lookup."""
    if not fresh:
        hit, country = state.cached_country(user_id)
        if hit:
            return country
    country, source = await detect_country(client, peer, user)
    state.remember(user_id, country, source)
    state.save()
    return country


def _title_text(title) -> str:
    return (getattr(title, "text", title) or "").strip()


def _peer_key(peer) -> int | None:
    return getattr(peer, "user_id", None)


class FolderManager:
    """Adds users to the Videshi folder(s), creating them when needed."""

    def __init__(self, client):
        self.client = client
        self.lock = asyncio.Lock()

    async def _folders(self):
        result = await self.client(GetDialogFiltersRequest())
        filters = getattr(result, "filters", result)  # older layers returned a list
        return [f for f in filters if isinstance(f, DialogFilter)]

    @staticmethod
    def _videshi(folders):
        found = []
        for f in folders:
            match = _FOLDER_RE.match(_title_text(f.title))
            if match:
                found.append((int(match.group(1) or 1), f))
        return sorted(found, key=lambda item: item[0])

    async def _create(self, folders, number: int, peer):
        used = {f.id for f in folders}
        folder_id = next(i for i in range(2, 256) if i not in used)
        title = FOLDER_TITLE if number == 1 else f"{FOLDER_TITLE} {number}"
        new = DialogFilter(id=folder_id, title=TextWithEntities(text=title, entities=[]),
                           pinned_peers=[], include_peers=[peer], exclude_peers=[])
        await self.client(UpdateDialogFilterRequest(id=folder_id, filter=new))
        return title

    async def add(self, peer: InputPeerUser) -> tuple[str, bool]:
        """(folder title, newly added). Raises RPCError if Telegram refuses."""
        async with self.lock:
            folders = await self._folders()
            numbered = self._videshi(folders)
            videshi = [f for _n, f in numbered]
            user_id = _peer_key(peer)
            for f in videshi:
                if any(_peer_key(p) == user_id for p in f.include_peers):
                    return _title_text(f.title), False
            if videshi:  # only the newest folder can still have room
                newest = videshi[-1]
                existing = list(newest.include_peers)
                updated = copy.copy(newest)  # keep the fetched folder untouched on refusal
                updated.include_peers = existing + [peer]
                try:
                    await self.client(UpdateDialogFilterRequest(id=newest.id, filter=updated))
                    return _title_text(newest.title), True
                except errors.RPCError:
                    # Telegram refuses a full folder (100 chats, 200 with
                    # Premium); on a smaller folder the error is real.
                    if len(existing) < FOLDER_FULL_AT:
                        raise
            next_number = max((n for n, _f in numbered), default=0) + 1
            return await self._create(folders, next_number, peer), True

    async def remove_from_unread(self, peers) -> tuple[list[str], list[str]]:
        """Take these users out of every folder named like "Unread".

        A chat is listed in a folder either explicitly (include/pinned list)
        or because the folder includes a whole chat type (e.g. "non-contacts"
        with "exclude read"). The first is removed, the second gets the chat
        in the folder's exclude list. Returns (changed titles, problems).
        """
        ids = {_peer_key(p): p for p in peers if _peer_key(p)}
        if not ids:
            return [], []
        changed, problems = [], []
        async with self.lock:
            for f in await self._folders():
                title = _title_text(f.title)
                if UNREAD_WORD not in title.lower() or _FOLDER_RE.match(title):
                    continue
                updated = copy.copy(f)
                updated.include_peers = [p for p in f.include_peers if _peer_key(p) not in ids]
                updated.pinned_peers = [p for p in f.pinned_peers if _peer_key(p) not in ids]
                by_type = any(getattr(f, flag, False) for flag in
                              ("contacts", "non_contacts", "groups", "broadcasts", "bots"))
                excluded = {_peer_key(p) for p in f.exclude_peers}
                missing = [p for uid, p in ids.items() if uid not in excluded] if by_type else []
                updated.exclude_peers = list(f.exclude_peers) + missing
                if (len(updated.include_peers) == len(f.include_peers)
                        and len(updated.pinned_peers) == len(f.pinned_peers) and not missing):
                    continue
                if not by_type and not updated.include_peers:
                    problems.append(f"'{title}' would be empty, so it was left as is")
                    continue
                try:
                    await self.client(UpdateDialogFilterRequest(id=f.id, filter=updated))
                    changed.append(title)
                except errors.RPCError as error:
                    problems.append(f"'{title}': {error.message or type(error).__name__}"
                                    + (" (its exclude list is full)" if len(f.exclude_peers) >= FOLDER_FULL_AT else ""))
        return changed, problems


def should_check(user, self_id: int, skip_ids=()) -> bool:
    """Only real people: no bots, no self, no service/guard accounts."""
    if user is None or getattr(user, "bot", False) or getattr(user, "is_self", False):
        return False
    uid = getattr(user, "id", None)
    return bool(uid) and uid != self_id and uid not in _SKIP_IDS and uid not in set(skip_ids)
