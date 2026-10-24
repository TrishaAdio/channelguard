"""DM country separator for quickreply.py (``.separator`` and ``.gdpa``).

* Country of a private chat, cheapest source first: the phone number when the
  user shares it, Telegram's ``PeerSettings.phone_country`` (the "Not a
  contact" card), then the same field on the full user profile.
* Foreign (non-India) DMs go into ONE chat folder named ``Videshi``. Telegram
  caps a folder at 200 chats (100 without Premium), so when it is full the
  least active chats (silent for days, few messages, few words) leave and new
  customers take their places. Videshi chats are kept out of every folder
  with "Unread" in its name; chats that leave Videshi go back there.
* Folder edits are rate limited by Telegram, so they are batched: one edit
  per folder however many chats change.

State is kept in ``data/separator.json``.
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
from telethon.tl.types import DialogFilter, TextWithEntities

try:
    import phonenumbers
except ImportError:  # optional: only needed for numbers outside India
    phonenumbers = None

HOME_COUNTRY = "IN"
FOLDER_TITLE = "Videshi"
_FOLDER_RE = re.compile(rf"^{FOLDER_TITLE}(?:\s+(\d+))?$", re.IGNORECASE)
# Unknown countries are re-checked at most this often.
UNKNOWN_RETRY_SECONDS = 5 * 60
# Folders whose title contains this word lose every chat filed into Videshi.
UNREAD_WORD = "unread"
# Telegram service notifications.
_SKIP_IDS = {777000}
# Chats per folder (and excluded chats per folder) without Premium; Premium
# doubles it to 200. FolderManager is told which one applies.
FOLDER_FULL_AT = 100
# Activity ranking (who stays in Videshi when it is full):
INACTIVE_DAYS = 7        # no message for this long -> leaves first
NEW_GRACE_HOURS = 48     # a newcomer who is talking is protected this long
STATS_SAMPLE = 50        # recent messages read to count words
STATS_MAX_AGE = 12 * 3600  # re-read a chat's history at most this often
# Longer flood waits are not slept through; the work is retried later.
MAX_FLOOD_SLEEP = 120


class SeparatorState:
    """users: {id: {country, source, checked}}, filed: {id: folder title},
    stats: {id: {total, words, last, at}}, joined: {id: ts entered Videshi},
    excluded: [ids this bot put in Unread folders' excluded chats]."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.users: dict = data.get("users") or {}
        self.filed: dict = data.get("filed") or {}
        self.stats: dict = data.get("stats") or {}
        self.joined: dict = data.get("joined") or {}
        self.excluded: set = {int(x) for x in data.get("excluded") or []}
        # {id: ts of their latest unread message}; cleared when you reply/read
        self.unread: dict = data.get("unread") or {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({
            "users": self.users, "filed": self.filed, "stats": self.stats,
            "joined": self.joined, "excluded": sorted(self.excluded), "unread": self.unread,
        }, ensure_ascii=False), encoding="utf-8")
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

    def foreign_ids(self) -> set:
        return {int(uid) for uid, e in self.users.items()
                if e.get("country") and e.get("country") != HOME_COUNTRY}

    def touch(self, user_id: int, text: str, ts: float) -> None:
        """A message in a tracked chat: update its activity without any request."""
        stat = self.stats.get(str(user_id))
        if stat is None:
            return
        stat["total"] = stat.get("total", 0) + 1
        stat["words"] = stat.get("words", 0) + len((text or "").split())
        stat["last"] = max(stat.get("last", 0), int(ts))


def rank(stat: dict | None, joined: float | None, now: float) -> tuple:
    """Sort key for Videshi; the LOWEST ranks leave first when it is full.

    1. Silent for INACTIVE_DAYS or more -> lowest tier.
    2. Everyone else.
    3. Newcomers (entered Videshi < NEW_GRACE_HOURS ago and talking in that
       time) -> protected, so new customers really do come in.
    Within a tier: fewer messages + words (messages + words/10) leave first,
    then the one that has been quiet longest. A chat whose history couldn't be
    read is kept (never evicted on missing data).
    """
    if not stat:
        return (3, 0.0, now)
    last = stat.get("last", 0)
    grace = NEW_GRACE_HOURS * 3600
    if joined and now - joined < grace and now - last < grace:
        tier = 2
    elif now - last >= INACTIVE_DAYS * 86400:
        tier = 0
    else:
        tier = 1
    return (tier, stat.get("total", 0) + stat.get("words", 0) / 10, last)


async def chat_stats(client, peer) -> dict:
    """{total, words, last, at} from ONE history request."""
    messages = await client.get_messages(peer, limit=STATS_SAMPLE)
    total = getattr(messages, "total", None) or len(messages)
    words = sum(len((getattr(m, "message", "") or "").split()) for m in messages)
    last = max((m.date.timestamp() for m in messages if getattr(m, "date", None)), default=0)
    return {"total": int(total), "words": int(words), "last": int(last), "at": int(time.time())}


def _country_from_phone(phone: str | None):
    if not phone:
        return None
    digits = str(phone).lstrip("+")
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

    FloodWaitError is raised, never turned into "unknown": a rate-limited
    lookup must be retried, not remembered as a user without a country.
    """
    country = _country_from_phone(getattr(user, "phone", None))
    if country:
        return country, "phone"
    try:
        result = await client(GetPeerSettingsRequest(peer=peer))
        code = _iso2(getattr(result.settings, "phone_country", None))
        if code:
            return code, "telegram"
    except errors.FloodWaitError:
        raise
    except errors.RPCError:
        pass
    try:
        full = await client(GetFullUserRequest(peer))
        code = _iso2(getattr(getattr(full.full_user, "settings", None), "phone_country", None))
        if code:
            return code, "telegram"
        for u in getattr(full, "users", None) or []:
            if getattr(u, "id", None) == _peer_key(peer):
                country = _country_from_phone(getattr(u, "phone", None))
                if country:
                    return country, "phone"
    except errors.FloodWaitError:
        raise
    except errors.RPCError:
        pass
    return None, "unknown"


async def country_for(client, state: SeparatorState, peer, user_id: int, user=None, *,
                      fresh=False, save=True):
    """Cached detect_country; ``fresh`` forces a new lookup."""
    phone_country = _country_from_phone(getattr(user, "phone", None))
    if phone_country:  # free and authoritative
        if state.users.get(str(user_id), {}).get("country") != phone_country:
            state.remember(user_id, phone_country, "phone")
            if save:
                state.save()
        return phone_country
    if not fresh:
        hit, country = state.cached_country(user_id)
        if hit:
            return country
    country, source = await detect_country(client, peer, user)
    state.remember(user_id, country, source)
    if save:
        state.save()
    return country


def _title_text(title) -> str:
    return (getattr(title, "text", title) or "").strip()


def _peer_key(peer) -> int | None:
    return getattr(peer, "user_id", None)


def is_unread_folder(f) -> bool:
    """A folder that shows unread chats: named like "Unread", or set to hide read chats."""
    title = _title_text(f.title)
    if _FOLDER_RE.match(title):
        return False
    return UNREAD_WORD in title.lower() or bool(getattr(f, "exclude_read", False))


def _by_type(f) -> bool:
    return any(getattr(f, flag, False) for flag in
               ("contacts", "non_contacts", "groups", "broadcasts", "bots"))


class FolderManager:
    """Batched edits of the Videshi folder(s) and the Unread folder(s)."""

    def __init__(self, client, limit: int = FOLDER_FULL_AT):
        self.client = client
        self.limit = limit
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

    async def _write(self, folder_id: int, folder) -> None:
        for attempt in range(3):
            try:
                await self.client(UpdateDialogFilterRequest(id=folder_id, filter=folder))
                return
            except errors.FloodWaitError as error:
                if attempt == 2 or error.seconds > MAX_FLOOD_SLEEP:
                    raise
                await asyncio.sleep(error.seconds + 1)

    async def _write_adding(self, folder_id, base, field: str, keep: list, adds: list,
                            budget: list):
        """Write ``base`` with ``field = keep + adds``; if Telegram refuses,
        split ``adds`` in halves so one bad chat (deleted account, invalid
        peer) or a lower limit only loses those chats, not all of them.

        Returns (accepted adds, first error or None).
        """
        trial = copy.copy(base)
        setattr(trial, field, list(keep) + list(adds))
        try:
            await self._write(folder_id, trial)
            return list(adds), None
        except errors.FloodWaitError:
            raise
        except errors.RPCError as error:
            budget[0] -= 1
            if len(adds) <= 1 or budget[0] <= 0:
                return [], error
            mid = len(adds) // 2
            ok1, e1 = await self._write_adding(folder_id, base, field, keep, adds[:mid], budget)
            ok2, e2 = await self._write_adding(folder_id, base, field, list(keep) + ok1,
                                               adds[mid:], budget)
            return ok1 + ok2, (e1 or e2 or error)

    async def unread_report(self, member_ids, ours=frozenset()) -> list[str]:
        """Human-readable state of every Unread folder vs. Videshi."""
        async with self.lock:
            folders = await self._folders()
        lines = []
        for f in folders:
            if not is_unread_folder(f):
                continue
            title = _title_text(f.title)
            if _by_type(f):
                hidden = {_peer_key(p) for p in f.exclude_peers}
                showing = [u for u in member_ids if u not in hidden]
                mine = sum(1 for p in f.exclude_peers
                           if _peer_key(p) not in ours and _peer_key(p) not in member_ids)
                lines.append(f"'{title}' (chats by type): {len(f.exclude_peers)}/{self.limit} "
                             f"excluded ({mine} added by you), {len(showing)} Videshi chat(s) "
                             "not excluded" + (" - read ones don't show anyway"
                                               if getattr(f, "exclude_read", False) else ""))
            else:
                listed = {_peer_key(p) for p in list(f.include_peers) + list(f.pinned_peers)}
                showing = [u for u in member_ids if u in listed]
                lines.append(f"'{title}' (hand-picked chats): {len(showing)} Videshi chat(s) still in it")
        return lines or ["No Unread folder found (a folder named 'Unread' or set to "
                         "'exclude read chats')."]

    async def members(self) -> dict:
        """{user id: InputPeer} of every chat in any Videshi folder right now."""
        async with self.lock:
            folders = await self._folders()
        found = {}
        for _n, f in self._videshi(folders):
            for p in list(f.pinned_peers) + list(f.include_peers):
                uid = _peer_key(p)
                if uid:
                    found.setdefault(uid, p)
        return found

    async def apply(self, wanted: dict, ours_excluded: set, *, priority=None,
                    unread=frozenset(), foreign_known=frozenset()):
        """Make Videshi hold exactly ``wanted`` ({uid: peer}, <= limit chats).

        * One "Videshi" folder: created if missing; extra "Videshi 2/3..."
          folders from older versions are merged into it and deleted.
        * Unread folders: wanted chats are removed/excluded; chats this bot
          excluded earlier that are no longer in Videshi are let back in.
          Exclusions are capped (200), so they go to the chats that need them
          most: ``priority`` order (unread first, then most recent). A folder
          that hides read chats doesn't need read chats excluded at all.
          Exclusions of users in ``foreign_known`` (added by older versions
          without being recorded) are treated as the bot's own.

        Returns (new ours_excluded set, changed folder titles, problems,
        ids actually in Videshi).
        """
        wanted = dict(list(wanted.items())[: self.limit])
        changed, problems = [], []
        async with self.lock:
            folders = await self._folders()
            numbered = self._videshi(folders)
            main = next((f for n, f in numbered if n == 1), None) or (numbered[0][1] if numbered else None)
            current = set()
            if main is not None:
                current = {_peer_key(p) for p in list(main.include_peers) + list(main.pinned_peers)}
            rejected = []
            if main is None:
                if wanted:
                    used = {f.id for f in folders}
                    folder_id = next(i for i in range(2, 256) if i not in used)
                    base = DialogFilter(
                        id=folder_id, title=TextWithEntities(text=FOLDER_TITLE, entities=[]),
                        pinned_peers=[], include_peers=[], exclude_peers=[])
                    accepted, error = await self._write_adding(
                        folder_id, base, "include_peers", [], list(wanted.values()), budget=[12])
                    rejected = [uid for uid in wanted if uid not in {_peer_key(p) for p in accepted}]
                    if accepted:
                        changed.append(FOLDER_TITLE)
                    elif error is not None:
                        raise error
            elif current != set(wanted):
                updated = copy.copy(main)
                updated.pinned_peers = [p for p in main.pinned_peers if _peer_key(p) in wanted]
                pinned = {_peer_key(p) for p in updated.pinned_peers}
                stay = [p for p in main.include_peers if _peer_key(p) in wanted and _peer_key(p) not in pinned]
                have = pinned | {_peer_key(p) for p in stay}
                adds = [p for uid, p in wanted.items() if uid not in have]
                if stay or adds or updated.pinned_peers:
                    accepted, error = await self._write_adding(
                        main.id, updated, "include_peers", stay, adds, budget=[12])
                    rejected = [_peer_key(p) for p in adds if p not in accepted]
                    if error is not None and not accepted and adds and not stay and not updated.pinned_peers:
                        raise error
                else:  # Telegram refuses an empty folder: delete it
                    await self.client(UpdateDialogFilterRequest(id=main.id))
                changed.append(_title_text(main.title))
            if rejected:
                problems.append(f"Telegram refused {len(rejected)} chat(s) in {FOLDER_TITLE} "
                                "(deleted or invalid accounts)")
                wanted = {uid: p for uid, p in wanted.items() if uid not in set(rejected)}
            for _n, extra in numbered:
                if main is not None and extra.id == main.id:
                    continue
                await self.client(UpdateDialogFilterRequest(id=extra.id))  # merged above
                changed.append(f"{_title_text(extra.title)} (merged into {FOLDER_TITLE}, deleted)")

            order = [uid for uid in (priority or list(wanted)) if uid in wanted]
            order += [uid for uid in wanted if uid not in set(order)]
            new_ours: set = set()
            for f in folders:
                if not is_unread_folder(f):
                    continue
                title = _title_text(f.title)
                updated = copy.copy(f)
                updated.include_peers = [p for p in f.include_peers if _peer_key(p) not in wanted]
                updated.pinned_peers = [p for p in f.pinned_peers if _peer_key(p) not in wanted]
                by_type = _by_type(f)
                keep, adds = list(f.exclude_peers), []
                if by_type:
                    def ours(uid):
                        return uid in ours_excluded or uid in foreign_known or uid in wanted
                    own = [p for p in f.exclude_peers if not ours(_peer_key(p))]
                    room = max(0, self.limit - len(own))
                    target = order[:room]
                    present = {_peer_key(p): p for p in f.exclude_peers}
                    keep = own + [present[uid] for uid in target if uid in present]
                    adds = [wanted[uid] for uid in target if uid not in present]
                    hidden_read = bool(getattr(f, "exclude_read", False))
                    left = [uid for uid in order[room:] if uid in unread or not hidden_read]
                    if left:
                        problems.append(
                            f"'{title}': {len(left)} {'unread ' if hidden_read else ''}Videshi chat(s) "
                            f"still show there - its excluded list is full ({self.limit}, "
                            f"{len(own)} of them added by you)")
                same = lambda a, b: [_peer_key(p) for p in a] == [_peer_key(p) for p in b]  # noqa: E731
                if (same(updated.include_peers, f.include_peers) and same(updated.pinned_peers, f.pinned_peers)
                        and same(keep, f.exclude_peers) and not adds):
                    new_ours |= {_peer_key(p) for p in keep if _peer_key(p) in wanted}
                    continue
                if not by_type and not updated.include_peers and not updated.pinned_peers:
                    problems.append(f"'{title}' would be empty, so it was left as is")
                    continue
                accepted, error = await self._write_adding(
                    f.id, updated, "exclude_peers", keep, adds, budget=[12])
                if error is None or accepted:
                    changed.append(title)
                    new_ours |= {_peer_key(p) for p in keep + accepted if _peer_key(p) in wanted}
                if error is not None:
                    lost = len(adds) - len(accepted)
                    problems.append(f"'{title}': Telegram refused {lost or 'the update for'} "
                                    f"chat(s) ({error.message or type(error).__name__})")
        return new_ours, changed, problems, set(wanted)


def should_check(user, self_id: int, skip_ids=()) -> bool:
    """Only real people: no bots, no self, no service/guard accounts."""
    if (user is None or getattr(user, "bot", False) or getattr(user, "is_self", False)
            or getattr(user, "deleted", False)):  # deleted accounts can't be put in folders
        return False
    uid = getattr(user, "id", None)
    return bool(uid) and uid != self_id and uid not in _SKIP_IDS and uid not in set(skip_ids)
