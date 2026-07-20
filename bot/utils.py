"""Pure helpers: group short codes and template rendering.

No Telegram or DB imports here so this module stays trivially testable.
"""
from __future__ import annotations

import html
import re
import unicodedata
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
