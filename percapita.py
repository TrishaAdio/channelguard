"""Per capita income lookup by ISO country code (used by quickreply's .gdpa).

Data lives in ``per_capita.json`` next to this file: World Bank World
Development Indicators, most recent year available per country. Refresh it with

    python percapita.py

which needs internet access only while refreshing; lookups are offline.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent / "per_capita.json"
_API = "https://api.worldbank.org/v2"
# GDP per capita (current US$) first; GNI per capita (Atlas, US$) is the
# fallback for the few economies the World Bank has no GDP figure for.
_INDICATORS = {"gdp": "NY.GDP.PCAP.CD", "gni": "NY.GNP.PCAP.CD"}

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {"countries": {}}
    return _cache


def flag(iso2: str) -> str:
    """'IN' -> regional-indicator flag emoji."""
    iso2 = (iso2 or "").upper()
    if len(iso2) != 2 or not iso2.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso2)


def lookup(iso2: str) -> dict | None:
    """{'name', 'gdp', 'gdp_year', 'gni', 'gni_year'} for a country, or None."""
    return _load()["countries"].get((iso2 or "").upper())


def per_capita_text(iso2: str) -> str | None:
    """One-line draft, e.g. '🇺🇸 United States - per capita income: $90,027 (2025)'."""
    row = lookup(iso2)
    if not row:
        return None
    for key in ("gdp", "gni"):
        if row.get(key) is not None:
            return (f"{flag(iso2)} {row['name']} - per capita income: "
                    f"${row[key]:,.0f} ({row[f'{key}_year']})").strip()
    return None


def _fetch(path: str, **params) -> list:
    query = "&".join(f"{k}={v}" for k, v in {"format": "json", "per_page": 20000, **params}.items())
    with urllib.request.urlopen(f"{_API}/{path}?{query}", timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload[1] if len(payload) > 1 and payload[1] else []


def refresh() -> int:
    """Re-download every country's latest figures into per_capita.json."""
    countries = {}
    iso3_to_iso2 = {}
    for c in _fetch("country"):
        if c["region"]["id"] == "NA" or not c.get("iso2Code"):  # aggregates
            continue
        iso3_to_iso2[c["id"]] = c["iso2Code"]
        countries[c["iso2Code"]] = {"name": c["name"].strip(), "gdp": None, "gdp_year": None,
                                    "gni": None, "gni_year": None}
    for key, code in _INDICATORS.items():
        for row in _fetch(f"country/all/indicator/{code}", mrnev=1):
            iso2 = iso3_to_iso2.get(row.get("countryiso3code"))
            if iso2 and row.get("value") is not None:
                countries[iso2][key] = round(row["value"])
                countries[iso2][f"{key}_year"] = int(row["date"])
    data = {
        "source": "World Bank, World Development Indicators (api.worldbank.org)",
        "indicators": {"gdp": "GDP per capita (current US$)",
                       "gni": "GNI per capita, Atlas method (current US$)"},
        "countries": dict(sorted(countries.items())),
    }
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    global _cache
    _cache = data
    return len(countries)


if __name__ == "__main__":
    print(f"Saved {refresh()} countries to {DATA_FILE}")
