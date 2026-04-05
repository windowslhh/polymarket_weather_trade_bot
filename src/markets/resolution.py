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
