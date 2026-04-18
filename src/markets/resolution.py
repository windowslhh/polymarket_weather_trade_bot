"""Parse resolution sources from Polymarket market metadata.

Extracts the official data source used for market settlement from
event descriptions (e.g. NOAA, Weather Underground, NWS).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Known resolution source patterns in Polymarket event descriptions
_RESOLUTION_PATTERNS = [
    (re.compile(r"weather\.gov|national weather service|NWS", re.I), "nws"),
    (re.compile(r"NOAA|noaa\.gov", re.I), "noaa"),
    (re.compile(r"weather\s*underground|wunderground", re.I), "wunderground"),
    (re.compile(r"accuweather", re.I), "accuweather"),
    (re.compile(r"hong kong observatory|hko\.gov", re.I), "hko"),
    (re.compile(r"met office|metoffice", re.I), "metoffice"),
    (re.compile(r"japan meteorological|jma\.go\.jp", re.I), "jma"),
    (re.compile(r"bureau of meteorology|bom\.gov\.au", re.I), "bom"),
    (re.compile(r"open-?meteo", re.I), "open-meteo"),
]

# Cache: event_id → resolution source
_resolution_cache: dict[str, str] = {}


@dataclass
class ResolutionSource:
    source_name: str  # e.g. "nws", "wunderground", "noaa"
    raw_text: str     # the matched text from description
    city: str


def parse_resolution_source(event_id: str, description: str, city: str = "") -> str:
    """Extract resolution source from event description.

    Returns source identifier string (e.g. "nws", "wunderground").
    Returns "unknown" if no known source is found.
    """
    if event_id in _resolution_cache:
        return _resolution_cache[event_id]

    source = "unknown"
    for pattern, name in _RESOLUTION_PATTERNS:
        if pattern.search(description):
            source = name
            break

    _resolution_cache[event_id] = source

    if source == "unknown" and description:
        logger.debug("Unknown resolution source for %s: %s", city, description[:100])

    return source


def parse_resolution_from_event(event_data: dict, city: str = "") -> str:
    """Parse resolution source from a Gamma API event dict.

    Checks multiple fields: description, resolutionSource, rules.
    """
    event_id = event_data.get("id", "")

    # Check multiple possible fields
    text_parts = []
    for field in ["description", "resolutionSource", "rules", "resolution"]:
        val = event_data.get(field, "")
        if val:
            text_parts.append(str(val))

    combined = " ".join(text_parts)
    return parse_resolution_source(event_id, combined, city)


def get_cached_sources() -> dict[str, str]:
    """Return all cached resolution sources for dashboard display."""
    return dict(_resolution_cache)


# ICAO extraction --------------------------------------------------------------
#
# Polymarket weather events list their settlement station inside the event's
# `resolutionSource` / `description` / `rules` — typically as:
#   - a Wunderground URL segment: .../history/daily/us/ny/KLGA/...
#   - a bare ICAO mention: "station KLGA", "Weather Underground station KLGA"
#   - a human-readable airport name ("LaGuardia Airport")
#
# The first two forms are machine-extractable.  The airport-name form requires
# a fuzzy lookup and is left to future work; for now we only return a value when
# a confident K-code match exists.
#
# Runtime purpose: cross-check the extracted ICAO against SETTLEMENT_STATIONS at
# startup (see src/weather/settlement.py:check_station_alignment).  If Polymarket
# quietly switches the settlement station for a city (as we discovered for
# Houston/Dallas/Denver in 2026-04), this check fails loudly instead of us
# trading against a mismatched data source for weeks.

# Match a K-code inside a Wunderground URL path or as a standalone token.
# Anchored on word boundaries to avoid false positives in free text.
_ICAO_IN_URL_RE = re.compile(r"/([A-Z]{4})(?:/|\b)")
_ICAO_BARE_RE = re.compile(r"\b([KC][A-Z]{3})\b")


def extract_settlement_icao(event_data: dict) -> str | None:
    """Extract the ICAO airport code used for settlement, if discoverable.

    Looks across `resolutionSource`, `resolutionUrl`, `description`, and `rules`
    fields.  Preference order:
      1. ICAO inside a Wunderground-style URL path (most reliable)
      2. Bare K-code (e.g. "KLGA") mentioned in text

    Returns None when no confident match is found.  Callers should treat None
    as "unable to verify" (warn, not fail) — only non-None mismatches are
    hard errors.
    """
    candidates: list[str] = []

    for field_name in ("resolutionSource", "resolutionUrl", "description", "rules", "resolution"):
        val = event_data.get(field_name, "")
        if not val:
            continue
        text = str(val)

        # URL-form first (higher confidence)
        for match in _ICAO_IN_URL_RE.findall(text):
            candidates.append(match)

        # Bare K-code / C-code mentions
        for match in _ICAO_BARE_RE.findall(text):
            candidates.append(match)

    if not candidates:
        return None

    # Majority vote across fields — protects against stray K-codes
    # (e.g. someone cited two stations in prose).
    from collections import Counter
    winner, _count = Counter(candidates).most_common(1)[0]
    return winner
