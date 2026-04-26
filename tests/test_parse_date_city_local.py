"""Y9: _parse_date year fallback uses city-local tz when supplied.

A market title like "January 1" parsed at UTC 23:30 on Dec 31 has an
ambiguous year — depends on the city's local clock.  Pre-Y9 we used
UTC year (2026) which would tag a Honolulu (UTC-10) Jan 1 event
created on its eve as 2026 even though Honolulu is still in 2025 at
that instant.  With Y9 the city tz threads through and the fallback
year matches the city's local clock.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from src.markets import discovery as discovery_mod
from src.markets.discovery import _parse_date


def _frozen_utc(utc_iso: str):
    instant = datetime.fromisoformat(utc_iso).replace(tzinfo=timezone.utc)

    class _Fz(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.astimezone().replace(tzinfo=None)
            return instant.astimezone(tz)

    return _Fz


def test_parse_date_explicit_year_string_unaffected_by_tz() -> None:
    """Sanity: a fully-qualified date string does NOT depend on the
    fallback year code path."""
    out = _parse_date("2027-01-15", city_tz="America/New_York")
    assert out == date(2027, 1, 15)


def test_parse_date_year_fallback_uses_city_tz_when_supplied() -> None:
    """At UTC 2026-12-31 23:30, a 'January 1' string parsed for a
    Honolulu (UTC-10) event should yield year 2026 (HNL still on
    2026-12-31 at that instant), NOT 2027 (UTC year)."""
    fz = _frozen_utc("2026-12-31T23:30:00")
    with patch.object(discovery_mod, "datetime", fz):
        # No tz hint → UTC fallback (2026 since UTC is still 2026-12-31)
        out_utc = _parse_date("January 1")
        # Honolulu still on 2026-12-31 at this instant
        out_hnl = _parse_date("January 1", city_tz="Pacific/Honolulu")
    assert out_utc == date(2026, 1, 1)  # UTC year is 2026
    assert out_hnl == date(2026, 1, 1)  # HNL is also 2026 here


def test_parse_date_year_fallback_diverges_at_utc_midnight_crossover() -> None:
    """At UTC 2027-01-01 00:30 (just past UTC midnight), HNL is still
    on 2026-12-31.  A 'January 1' event for HNL should still tag year
    2026 (since HNL hasn't ticked into 2027 yet), while the same string
    for NYC (also still on 2026-12-31 EST) should tag 2026 too —
    but a same-instant call WITHOUT city_tz uses UTC year 2027."""
    fz = _frozen_utc("2027-01-01T00:30:00")
    with patch.object(discovery_mod, "datetime", fz):
        out_no_tz = _parse_date("January 1")
        out_hnl = _parse_date("January 1", city_tz="Pacific/Honolulu")
        out_nyc = _parse_date("January 1", city_tz="America/New_York")
    # No-tz: UTC year is 2027 → "January 1" → 2027-01-01
    assert out_no_tz == date(2027, 1, 1)
    # HNL local: 2026-12-31 → fallback year 2026 → "January 1" → 2026-01-01
    # (mid-year-rollover boundary, this is the case Y9 specifically targets)
    assert out_hnl == date(2026, 1, 1), (
        f"Y9: HNL still on 2026 at UTC 00:30 — got {out_hnl}"
    )
    # NYC at UTC 00:30 = 2026-12-31 19:30 EST → still 2026
    assert out_nyc == date(2026, 1, 1)


def test_parse_date_invalid_tz_falls_back_to_utc_year_silently() -> None:
    """A bogus city_tz must not crash _parse_date — it should just
    fall back to UTC (the documented safe default)."""
    fz = _frozen_utc("2027-06-15T12:00:00")
    with patch.object(discovery_mod, "datetime", fz):
        out = _parse_date("January 1", city_tz="Not_A_Real/Zone")
    assert out == date(2027, 1, 1)
