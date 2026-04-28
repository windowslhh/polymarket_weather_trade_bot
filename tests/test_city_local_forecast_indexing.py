"""City-local forecast indexing (2026-04-28 hotfix).

The rebalancer keys ``_cached_forecasts_by_date`` by ``event.market_date``,
which is constructed in city-local time over in markets/discovery.  Anchoring
the forecast-fetch side by UTC opened a 5-8h race each day where west-coast
cities had already rolled their local calendar but UTC had not (or vice
versa) — the lookup ``cache[market_date][city]`` missed, the by-name
fallback served a stale forecast whose ``forecast_date`` no longer matched
the event, and ``evaluator.evaluate_exit_signals`` tripped its FIX-22
invariant (AssertionError) — taking out the 15-min TRIM/EXIT safety net
for the duration of the window.

These tests pin ``city_local_date`` and ``get_forecasts_for_city_local_window``
to the new semantics so the bug cannot recur.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from src.config import CityConfig
from src.weather.forecast import (
    city_local_date,
    get_forecasts_for_city_local_window,
)
from src.weather.models import Forecast


# ── Fake "now" plumbing ─────────────────────────────────────────────────
#
# city_local_date reads datetime.now(tz).  We freeze that by monkey-patching
# the symbol on the forecast module; the real datetime class stays intact
# everywhere else (notably zoneinfo internals, which would otherwise misbehave
# under a wholesale class swap).


class _FrozenNow:
    """Returns a fixed UTC instant when called as datetime.now(tz)."""

    def __init__(self, fixed_utc: datetime):
        self._fixed_utc = fixed_utc

    def __call__(self, tz=None):
        if tz is None:
            return self._fixed_utc.astimezone()
        return self._fixed_utc.astimezone(tz)


def _patch_now(monkeypatch, fixed_utc: datetime) -> None:
    """Patch datetime.now in src.weather.forecast to return fixed_utc."""
    import src.weather.forecast as fc_mod

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_utc.astimezone()
            return fixed_utc.astimezone(tz)

    monkeypatch.setattr(fc_mod, "datetime", _FakeDatetime)


def _city(name: str, tz: str) -> CityConfig:
    return CityConfig(name=name, icao=f"K{name[:3].upper()}", lat=0.0, lon=0.0, tz=tz)


# ── city_local_date ─────────────────────────────────────────────────────


def test_city_local_date_west_of_utc(monkeypatch):
    """LA at 2026-04-28 03:00 UTC is still 2026-04-27 20:00 PDT — local
    date is 04-27, not 04-28.  This is the production failure mode."""
    _patch_now(monkeypatch, datetime(2026, 4, 28, 3, 0, tzinfo=timezone.utc))
    la = _city("Los Angeles", "America/Los_Angeles")

    assert city_local_date(la) == date(2026, 4, 27)
    assert city_local_date(la, offset_days=1) == date(2026, 4, 28)
    assert city_local_date(la, offset_days=2) == date(2026, 4, 29)


def test_city_local_date_east_of_utc(monkeypatch):
    """At 2026-04-27 23:00 UTC, NYC is still 19:00 EDT same day."""
    _patch_now(monkeypatch, datetime(2026, 4, 27, 23, 0, tzinfo=timezone.utc))
    nyc = _city("New York", "America/New_York")

    assert city_local_date(nyc) == date(2026, 4, 27)


def test_city_local_date_cross_day_split(monkeypatch):
    """At 2026-04-28 04:00 UTC, NYC has already rolled to the new day
    (00:00 EDT) but LA is still 21:00 PDT on 04-27 — different cities
    legitimately produce different date keys at the same wall-clock
    instant.  This is the whole point of the FIX."""
    _patch_now(monkeypatch, datetime(2026, 4, 28, 4, 0, tzinfo=timezone.utc))
    nyc = _city("New York", "America/New_York")
    la = _city("Los Angeles", "America/Los_Angeles")

    assert city_local_date(nyc) == date(2026, 4, 28)
    assert city_local_date(la) == date(2026, 4, 27)


def test_city_local_date_missing_tz_falls_back_to_utc(monkeypatch, caplog):
    """A city with empty tz logs a warning and falls back to UTC date —
    operator notices the misconfiguration but the bot still picks *some*
    date so trading isn't blocked entirely."""
    _patch_now(monkeypatch, datetime(2026, 4, 28, 3, 0, tzinfo=timezone.utc))
    broken = CityConfig(name="Broken", icao="KBRK", lat=0.0, lon=0.0, tz="")

    import logging
    with caplog.at_level(logging.WARNING, logger="src.weather.forecast"):
        d = city_local_date(broken)

    assert d == date(2026, 4, 28)  # UTC of fixed instant
    assert any("Broken" in r.message for r in caplog.records)


def test_city_local_date_invalid_tz_falls_back_to_utc(monkeypatch, caplog):
    _patch_now(monkeypatch, datetime(2026, 4, 28, 3, 0, tzinfo=timezone.utc))
    broken = CityConfig(name="Broken", icao="KBRK", lat=0.0, lon=0.0, tz="Not/A/Real_Zone")

    import logging
    with caplog.at_level(logging.WARNING, logger="src.weather.forecast"):
        d = city_local_date(broken)

    assert d == date(2026, 4, 28)
    assert any("Not/A/Real_Zone" in r.message for r in caplog.records)


# ── get_forecasts_for_city_local_window ─────────────────────────────────


def _mock_forecast(city_name: str, target: date) -> Forecast:
    return Forecast(
        city=city_name, forecast_date=target,
        predicted_high_f=70.0 + (target.toordinal() % 10),
        predicted_low_f=55.0,
        confidence_interval_f=3.0, source="mock",
        fetched_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_window_keys_are_city_local(monkeypatch):
    """Each city's today/D+1/D+2 lands under its own local date key — at
    a UTC instant that splits NYC and LA across calendar days, the
    returned dict has different keys for each city's same-offset entry."""
    _patch_now(monkeypatch, datetime(2026, 4, 28, 4, 0, tzinfo=timezone.utc))
    nyc = _city("New York", "America/New_York")
    la = _city("Los Angeles", "America/Los_Angeles")

    captured_targets: list[tuple[str, date]] = []

    async def _fake_get_forecast(city, target, client):
        captured_targets.append((city.name, target))
        return _mock_forecast(city.name, target)

    with patch("src.weather.forecast.get_forecast", side_effect=_fake_get_forecast):
        out = await get_forecasts_for_city_local_window([nyc, la], days=3)

    # NYC's local today=04-28; LA's local today=04-27.  D+1/D+2 follow.
    assert date(2026, 4, 28) in out
    assert "New York" in out[date(2026, 4, 28)]
    assert date(2026, 4, 27) in out
    assert "Los Angeles" in out[date(2026, 4, 27)]

    # LA D+1 = 04-28 — same date key as NYC's today, but each lands
    # under that key for its own city, so the cache merges naturally.
    assert "Los Angeles" in out[date(2026, 4, 28)]


@pytest.mark.asyncio
async def test_window_skips_failures(monkeypatch):
    """A failure for one (city, date) pair logs a warning but doesn't
    drop the rest of the window."""
    _patch_now(monkeypatch, datetime(2026, 4, 28, 4, 0, tzinfo=timezone.utc))
    nyc = _city("New York", "America/New_York")

    call_count = {"n": 0}

    async def _flaky(city, target, client):
        call_count["n"] += 1
        if call_count["n"] == 2:  # fail D+1 only
            raise RuntimeError("synthetic api failure")
        return _mock_forecast(city.name, target)

    with patch("src.weather.forecast.get_forecast", side_effect=_flaky):
        out = await get_forecasts_for_city_local_window([nyc], days=3)

    # Today + D+2 succeed; D+1 missing.
    assert date(2026, 4, 28) in out  # NYC today
    assert date(2026, 4, 30) in out  # NYC D+2
    assert date(2026, 4, 29) not in out


@pytest.mark.asyncio
async def test_window_empty_inputs():
    assert await get_forecasts_for_city_local_window([], days=3) == {}
    nyc = _city("New York", "America/New_York")
    assert await get_forecasts_for_city_local_window([nyc], days=0) == {}


# ── End-to-end invariant: forecast_date == market_date ──────────────────


@pytest.mark.asyncio
async def test_lookup_invariant_holds_at_cross_night(monkeypatch):
    """The reason this whole hotfix exists.  At UTC=2026-04-28 04:00,
    discovery.py creates an LA event with market_date=2026-04-27 (LA
    local).  The window helper must populate
    ``out[2026-04-27]['Los Angeles']`` so the production lookup
    ``_cached_forecasts_by_date[event.market_date][event.city]``
    resolves and the FIX-22 invariant ``forecast.forecast_date ==
    event.market_date`` holds."""
    _patch_now(monkeypatch, datetime(2026, 4, 28, 4, 0, tzinfo=timezone.utc))
    la = _city("Los Angeles", "America/Los_Angeles")

    async def _fake_get_forecast(city, target, client):
        return _mock_forecast(city.name, target)

    with patch("src.weather.forecast.get_forecast", side_effect=_fake_get_forecast):
        out = await get_forecasts_for_city_local_window([la], days=3)

    # Simulate the production lookup.
    la_market_date = date(2026, 4, 27)  # discovery would compute this same way
    fc = out.get(la_market_date, {}).get("Los Angeles")
    assert fc is not None, "LA forecast must be reachable under its city-local market_date"
    assert fc.forecast_date == la_market_date, (
        "FIX-22 invariant: forecast.forecast_date must match the lookup date key"
    )
