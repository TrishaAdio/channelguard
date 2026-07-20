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
from pathlib import Path
from typing import Optional

STORE = Path(__file__).resolve().parent / "data" / "links.json"
_MAX_ENTRIES = 500

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
