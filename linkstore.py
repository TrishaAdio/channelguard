"""Tiny shared link store used by BOTH programs on the same server.

The BOT (bot/) is the account actually inside the groups, so it is the only one
that can create invite links. Every time it generates a link for a group it
records it here. The userbot (quickreply.py) cannot see those groups, so at
payment time it just looks the link up by your search keyword and pastes it into
the "Thanks for paying" message ({link}).

Both programs run from the repo root, so they share this one JSON file:
    <repo>/data/links.json

Format:
    {"entries": [{"link","title","short","ftitle","fshort","ts"}...],
     "last": {same shape} | null}
"""
from __future__ import annotations

import json
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Optional

STORE = Path(__file__).resolve().parent / "data" / "links.json"
# Reservation bridge: the userbot writes requests, the bot writes results.
# Single writer per file (userbot -> REQUESTS, bot -> RESULTS) avoids races.
REQUESTS = STORE.parent / "link_requests.json"
RESULTS = STORE.parent / "link_results.json"
_MAX_ENTRIES = 500
_TTL = 3600  # seconds a request/result stays relevant

# Small-caps letters NFKD does not decompose (titles are often styled).
_SMALL_CAPS = {
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ғ": "f",
    "ɢ": "g", "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m",
    "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ǫ": "q", "ꞯ": "q", "ʀ": "r", "ꜱ": "s",
    "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
}


def fold(text: str) -> str:
    """Fold fancy Unicode fonts to plain lowercase so a normal typed keyword
    matches a styled title (e.g. 'ᴄᴘ 2026' -> 'cp 2026')."""
    if not text:
        return ""
    s = "".join(_SMALL_CAPS.get(c, c) for c in text)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).casefold().strip()


def _load() -> dict:
    try:
        data = json.loads(STORE.read_text("utf-8"))
        if isinstance(data, dict):
            data.setdefault("entries", [])
            data.setdefault("last", None)
            return data
    except (OSError, ValueError):
        pass
    return {"entries": [], "last": None}


def _save(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_name(STORE.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(STORE)


def save_link(link: str, title: str = "", short: str = "") -> None:
    """Record a link the bot just generated for a group. Called by the BOT."""
    if not link:
        return
    entry = {
        "link": link,
        "title": title or "",
        "short": short or "",
        "ftitle": fold(title),
        "fshort": fold(short),
        "ts": time.time(),
    }
    data = _load()
    ftitle = entry["ftitle"]
    # Replace any previous entry for the same (folded) title so lookups return
    # the freshest link for a group.
    entries = [
        e for e in data.get("entries", [])
        if not ftitle or e.get("ftitle") != ftitle
    ]
    entries.append(entry)
    data["entries"] = entries[-_MAX_ENTRIES:]
    data["last"] = entry
    _save(data)


def find_link(query: str) -> Optional[str]:
    """Return the freshest link whose folded title/short matches the folded
    query. Empty query -> the most recently generated link. Called by the
    userbot at payment time."""
    data = _load()
    q = fold(query)
    if not q:
        last = data.get("last")
        return last.get("link") if last else None

    best = None
    for e in data.get("entries", []):
        ftitle = e.get("ftitle", "")
        fshort = e.get("fshort", "")
        if q in ftitle or (fshort and (q == fshort or q in fshort)):
            if best is None or e.get("ts", 0) > best.get("ts", 0):
                best = e
    if best:
        return best.get("link")
    last = data.get("last")
    return last.get("link") if last else None


def find_entry(query: str) -> Optional[dict]:
    """Like find_link but returns the whole entry (link + title), or None."""
    data = _load()
    q = fold(query)
    candidates = data.get("entries", [])
    if not q:
        return data.get("last")
    best = None
    for e in candidates:
        if q in e.get("ftitle", "") or (e.get("fshort") and q in e.get("fshort", "")):
            if best is None or e.get("ts", 0) > best.get("ts", 0):
                best = e
    return best or data.get("last")


# --- reservation bridge ---------------------------------------------------
def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def request_link(query: str, user_id: int) -> str:
    """USERBOT: ask the BOT to reserve a link for `query`, bound to `user_id`.
    Returns a request id to poll with get_result/has_result."""
    rid = uuid.uuid4().hex
    now = time.time()
    reqs = _read_json(REQUESTS, [])
    if not isinstance(reqs, list):
        reqs = []
    reqs = [r for r in reqs if now - r.get("ts", 0) < _TTL]  # prune old
    reqs.append({"id": rid, "query": query, "user_id": int(user_id), "ts": now})
    _write_json(REQUESTS, reqs[-_MAX_ENTRIES:])
    return rid


def pending_requests() -> list:
    """BOT: read reservation requests (read-only; never clears the file)."""
    reqs = _read_json(REQUESTS, [])
    return reqs if isinstance(reqs, list) else []


def has_result(rid: str) -> bool:
    res = _read_json(RESULTS, {})
    return isinstance(res, dict) and rid in res


def put_result(rid: str, link: str, title: str = "") -> None:
    """BOT: publish the reserved link (or "" on failure) for a request id."""
    now = time.time()
    res = _read_json(RESULTS, {})
    if not isinstance(res, dict):
        res = {}
    res[rid] = {"link": link or "", "title": title or "", "ts": now}
    res = {k: v for k, v in res.items() if now - v.get("ts", 0) < _TTL}
    _write_json(RESULTS, res)


def get_result(rid: str) -> Optional[str]:
    """USERBOT: the reserved link for a request id, or None if not published or
    the bot couldn't make one (empty)."""
    res = _read_json(RESULTS, {})
    if not isinstance(res, dict):
        return None
    entry = res.get(rid)
    if not entry:
        return None
    return entry.get("link") or None
