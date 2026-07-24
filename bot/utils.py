"""Pure helpers: group short codes and template rendering.

No Telegram or DB imports here so this module stays trivially testable.
"""
from __future__ import annotations

import html
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, Mapping

_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_VOWELS_RE = re.compile(r"[aeiou]", re.IGNORECASE)

# Unicode small-caps letters that NFKD does NOT decompose to ASCII. Titles like
# "ɴɪᴄᴇ ʙʀᴏ" are common; fold them so a normal typed query still matches.
_SMALL_CAPS = {
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ғ": "f",
    "ɢ": "g", "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m",
    "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ǫ": "q", "ꞯ": "q", "ʀ": "r", "ꜱ": "s",
    "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
}


def fold_fonts(text: str) -> str:
    """Fold fancy Unicode "fonts" back to plain letters so a normal typed query
    matches a styled title, e.g. "ɴɪᴄᴇ ʙʀᴏ" or "𝐍𝐢𝐜𝐞 𝐁𝐫𝐨" -> "nice bro".

    NFKD handles bold/italic/script/fraktur/double-struck/mono/sans/fullwidth/
    circled variants; a small-caps table covers what NFKD leaves untouched.
    """
    if not text:
        return ""
    folded = "".join(_SMALL_CAPS.get(ch, ch) for ch in text)
    folded = unicodedata.normalize("NFKD", folded)
    return "".join(c for c in folded if not unicodedata.combining(c))


def short_code(title: str) -> str:
    """Derive a compact code from a chat title.

    The rule mirrors the requested behaviour: take the first word and drop its
    vowels while always keeping the leading letter, e.g. ``"Lom And Som Op"``
    -> ``"Lm"``. Falls back gracefully for vowel-only or symbol-only titles.
    """
    words = _WORD_RE.findall(fold_fonts(title or ""))
    if not words:
        return "grp"
    word = words[0]
    reduced = word[0] + _VOWELS_RE.sub("", word[1:])
    if len(reduced) < 2:
        # First word was e.g. "Op" -> "Op"; or all vowels -> use first 2 chars.
        reduced = word[:2] if len(word) >= 2 else word
    return reduced[:1].upper() + reduced[1:].lower()


def unique_short_code(title: str, taken: Iterable[str]) -> str:
    """A short code guaranteed not to collide with ``taken`` (case-insensitive)."""
    base = short_code(title)
    lowered = {t.lower() for t in taken}
    if base.lower() not in lowered:
        return base
    n = 2
    while f"{base}{n}".lower() in lowered:
        n += 1
    return f"{base}{n}"


def normalized_group_key(text: str) -> str:
    """A font/case/punctuation-insensitive key used by group lookup."""
    return "".join(
        char
        for char in fold_fonts(text or "").casefold()
        if char.isalnum()
    )


def normalized_group_words(text: str) -> tuple[str, ...]:
    """Return human-readable title words after folding styled Unicode text."""
    return tuple(
        word.casefold() for word in _WORD_RE.findall(fold_fonts(text or ""))
    )


def _group_value(group: Mapping, key: str) -> str:
    try:
        return str(group[key] or "")
    except (KeyError, TypeError):
        return ""


def group_literal_rank(query: str, group: Mapping) -> int:
    """Rank non-fuzzy group matches.

    Human-readable names always outrank the generated short code:

    4. exact full title or public username
    3. exact title word
    2. prefix of a title word (``in``/``ind``/``indi`` -> ``Indian``)
    1. exact legacy short code
    """
    wanted = normalized_group_key(query)
    if not wanted:
        return 0

    title = _group_value(group, "title")
    title_key = normalized_group_key(title)
    username_key = normalized_group_key(_group_value(group, "username"))
    if wanted == title_key or (username_key and wanted == username_key):
        return 4

    query_words = normalized_group_words(query)
    title_words = normalized_group_words(title)
    if len(query_words) == 1:
        query_word = query_words[0]
        if query_word in title_words:
            return 3
        if len(query_word) >= 2 and any(
            word.startswith(query_word) for word in title_words
        ):
            return 2

    short_key = normalized_group_key(_group_value(group, "short_code"))
    if short_key and wanted == short_key:
        return 1
    return 0


def group_fuzzy_score(query: str, group: Mapping) -> float:
    """Typo score using title text only, never generated short codes."""
    wanted = normalized_group_key(query)
    if len(wanted) <= 2:
        return 0.0
    title = _group_value(group, "title")
    candidates = {
        normalized_group_key(title),
        *normalized_group_words(title),
    }
    candidates.discard("")
    return max(
        (
            SequenceMatcher(None, wanted, value, autojunk=False).ratio()
            for value in candidates
        ),
        default=0.0,
    )


def group_match_score(query: str, group: Mapping) -> float:
    """Compatibility score with literal title tiers above typo fallback."""
    literal = group_literal_rank(query, group)
    if literal:
        return {4: 1.0, 3: 0.99, 2: 0.97, 1: 0.95}[literal]
    return group_fuzzy_score(query, group)


def fuzzy_group_threshold(query: str) -> float:
    length = len(normalized_group_key(query))
    if length <= 2:
        return 1.0
    if length == 3:
        return 0.82
    if length <= 5:
        return 0.74
    return 0.70


# Tokens the owner can use inside a template body.
TEMPLATE_TOKENS = (
    "{link}", "{title}", "{short}", "{amount}", "{name}", "{keyword}", "{orderid}",
)


def render_template(body: str, values: Mapping[str, str]) -> str:
    """Replace ``{token}`` placeholders in ``body`` with escaped values.

    The body itself is owner-authored HTML (so <b>, <blockquote> etc. are kept
    verbatim); only the substituted *values* are HTML-escaped so a title with
    ``&`` or ``<`` cannot break Telegram's HTML parser.
    """
    out = body or ""
    for key in ("link", "title", "short", "amount", "name", "keyword", "orderid"):
        token = "{" + key + "}"
        if token in out:
            out = out.replace(token, html.escape(str(values.get(key, "") or "")))
    return out


def truncate(text: str, limit: int = 40) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"
