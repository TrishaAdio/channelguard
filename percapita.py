"""Per capita income lookup for quickreply's .gdpa.

Reads the fixed chart in per_capita_chart.py (loaded into memory once at
import). No internet calls: every lookup is an instant dictionary read.
"""
from __future__ import annotations

from per_capita_chart import CHART


def flag(iso2: str) -> str:
    """'IN' -> regional-indicator flag emoji."""
    iso2 = (iso2 or "").upper()
    if len(iso2) != 2 or not iso2.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso2)


def lookup(iso2: str):
    """(name, US$ per person, year) or None."""
    return CHART.get((iso2 or "").upper())


def per_capita_text(iso2: str) -> str | None:
    """One-line draft, e.g. '🇺🇸 United States - per capita income: $90,027 (2025)'."""
    row = lookup(iso2)
    if not row:
        return None
    name, value, year = row
    return f"{flag(iso2)} {name} - per capita income: ${value:,} ({year})".strip()
