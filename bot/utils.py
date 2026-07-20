"""Pure helpers: group short codes and template rendering.

No Telegram or DB imports here so this module stays trivially testable.
"""
from __future__ import annotations

import html
import re
from typing import Iterable, Mapping

_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_VOWELS_RE = re.compile(r"[aeiou]", re.IGNORECASE)


def short_code(title: str) -> str:
    """Derive a compact code from a chat title.

    The rule mirrors the requested behaviour: take the first word and drop its
    vowels while always keeping the leading letter, e.g. ``"Lom And Som Op"``
    -> ``"Lm"``. Falls back gracefully for vowel-only or symbol-only titles.
    """
    words = _WORD_RE.findall(title or "")
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
