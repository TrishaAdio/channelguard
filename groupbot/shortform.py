"""Group-title short form.

Rule (from the spec example "Lom And Som Op" -> "Lm"):
  * take the leading meaningful word(s), skipping filler words like "and"/"the",
  * shorten each kept word to its first letter plus the consonants of the rest,
  * Title-case the result.

So "Lom" -> "Lm", "Som" -> "Sm". By default only the first meaningful word is
used (SHORT_FORM_WORDS=1) which yields "Lm" for "Lom And Som Op". Increase
SHORT_FORM_WORDS to combine more words (e.g. 2 -> "LmSm").
"""
from __future__ import annotations

import re

_VOWELS = set("aeiouAEIOU")
_STOPWORDS = {"and", "the", "of", "or", "a", "an", "for", "to", "in", "on"}
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)  # letter runs only


def shorten_word(word: str) -> str:
    """First letter + the consonants of the rest. 'Lom' -> 'Lm'."""
    letters = [c for c in word if c.isalpha()]
    if not letters:
        return ""
    first = letters[0]
    rest = [c for c in letters[1:] if c not in _VOWELS]
    return (first + "".join(rest)).capitalize()


def short_form(title: str, words: int = 1) -> str:
    """Build a short form from a group title. Always returns a non-empty label."""
    title = (title or "").strip()
    if not title:
        return "Grp"

    tokens = _WORD_RE.findall(title)
    meaningful = [t for t in tokens if t.lower() not in _STOPWORDS] or tokens

    pieces = [shorten_word(t) for t in meaningful[: max(1, words)]]
    pieces = [p for p in pieces if p]
    if pieces:
        return "".join(pieces)

    # No alphabetic content (e.g. an emoji-only or digit title): fall back to a
    # compact, readable label so the group is still identifiable.
    compact = re.sub(r"\s+", "", title)
    return compact[:6] or "Grp"
