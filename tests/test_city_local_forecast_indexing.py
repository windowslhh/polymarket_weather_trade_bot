"""FIX-2P-3: forecast cache must be keyed in city-local time.

H-9 (audit, 2026-04-26): position_check died 7 times in 25h of paper
trading, every failure inside the UTC-evening window when west-coast
cities are still on the previous local date.  Root cause: the cache
was populated from ``datetime.now(timezone.utc).date()`` while
``event.market_date`` is built in city-local time (discovery.py).
Between 00:00 and 08:00 UTC the two anchors disagreed → cache miss →
fallback to a wrong-day forecast → FIX-22 invariant tripped →
position_check raised AssertionError.

These tests freeze "now" to a specific UTC instant where NYC is
still on the previous local date and verify:

1. ``city_local_date(NYC, offset_days=0)`` returns NYC's local
   yesterday (relative to UTC), not UTC today.
2. ``get_forecasts_for_city_local_window`` populates a date that
   matches the city-local market_date — i.e., a NYC event whose
   market_date is its city-local "today" finds its forecast in
   the cache.
3. The downstream evaluator's FIX-22 invariant
   (``forecast.forecast_date == event.market_date``) is satisfied
   end-to-end with the city-local cache, where the UTC-keyed cache
   would have produced a mismatch.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from src.config import CityConfig
from src.weather import forecast as forecast_mod
from src.weather.forecast import (
    city_local_date,
    get_forecasts_for_city_local_window,
)
from src.weather.models import Forecast


_NYC = CityConfig(name="NYC", icao="KNYC", lat=40.71, lon=-74.0, tz="America/New_York")
_LA = CityConfig(name="LA", icao="KLAX", lat=34.05, lon=-118.24, tz="America/Los_Angeles")


# 2026-04-26 03:00 UTC == 2026-04-25 23:00 EDT == 2026-04-25 20:00 PDT
# All US cities are still on the *previous* calendar day.
_UTC_CROSS_NIGHT = datetime(2026, 4, 26, 3, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """datetime subclass whose ``now()`` returns the frozen instant."""

    @classmethod
    def now(cls, tz=None):  # noqa: D401 - test helper
        if tz is None:
            return _UTC_CROSS_NIGHT.astimezone().replace(tzinfo=None)
        return _UTC_CROSS_NIGHT.astimezone(tz)


def test_city_local_date_returns_yesterday_during_utc_cross_night() -> None:
    """NYC at 23:00 EDT is still on the previous day relative to UTC midnight."""
    with patch.object(forecast_mod, "datetime", _FrozenDatetime):
        nyc_today = city_local_date(_NYC)
        la_today = city_local_date(_LA)

    # UTC date at the frozen instant is 2026-04-26.  Both cities should
    # be one day behind.
    utc_date = _UTC_CROSS_NIGHT.date()
    assert utc_date == date(2026, 4, 26)
    assert nyc_today == date(2026, 4, 25), (
        f"NYC at 23:00 EDT 2026-04-25 must report local-today=04-25, "
        f"got {nyc_today}"
    )
    assert la_today == date(2026, 4, 25)


@pytest.mark.asyncio
async def test_city_local_window_keys_cache_by_city_local_today() -> None:
    """The window cache must hold a NYC entry under NYC-local today (04-25),
    not UTC today (04-26).  Pre-fix the cache key was UTC and the lookup
    `cache[market_date][city]` (with city-local market_date) missed."""
    captured_targets: list[date] = []

    async def _stub_get_forecast(city, target, client):
        captured_targets.append(target)
        return Forecast(
            city=city.name, forecast_date=target,
            predicted_high_f=72.0, predicted_low_f=60.0,
            confidence_interval_f=3.0, source="stub",
            fetched_at=datetime.now(timezone.utc),
        )

    with patch.object(forecast_mod, "datetime", _FrozenDatetime), \
         patch.object(forecast_mod, "get_forecast", _stub_get_forecast):
        cache = await get_forecasts_for_city_local_window([_NYC], days=3)

    nyc_local_today = date(2026, 4, 25)
    nyc_local_d1 = date(2026, 4, 26)
    nyc_local_d2 = date(2026, 4, 27)
    assert nyc_local_today in cache
    assert nyc_local_d1 in cache
    assert nyc_local_d2 in cache
    # The UTC-keyed bug would have populated 04-26/04-27/04-28 only,
    # leaving NYC-local 04-25 absent.  Make that pin explicit.
    assert date(2026, 4, 28) not in cache
    assert "NYC" in cache[nyc_local_today]
    assert cache[nyc_local_today]["NYC"].forecast_date == nyc_local_today


@pytest.mark.asyncio
async def test_city_local_window_satisfies_fix22_invariant_for_market_date() -> None:
    """End-to-end pin: a NYC event whose market_date is its city-local today
    finds a matching forecast in the cache, so FIX-22's invariant
    ``forecast.forecast_date == event.market_date`` holds.

    The pre-FIX-2P-3 UTC-keyed cache would miss here and the rebalancer
    fallback would route a *different-day* forecast into the evaluator,
    crashing position_check 7×/25h in production.
    """
    async def _stub_get_forecast(city, target, client):
        return Forecast(
            city=city.name, forecast_date=target,
            predicted_high_f=72.0, predicted_low_f=60.0,
            confidence_interval_f=3.0, source="stub",
            fetched_at=datetime.now(timezone.utc),
        )

    with patch.object(forecast_mod, "datetime", _FrozenDatetime), \
         patch.object(forecast_mod, "get_forecast", _stub_get_forecast):
        cache = await get_forecasts_for_city_local_window([_NYC, _LA], days=3)
        # Discovery builds market_date in city-local terms (see
        # markets/discovery.py:229) — same anchor as city_local_date.
        nyc_market_date = city_local_date(_NYC)
        la_market_date = city_local_date(_LA)

    nyc_fc = cache.get(nyc_market_date, {}).get("NYC")
    la_fc = cache.get(la_market_date, {}).get("LA")
    assert nyc_fc is not None
    assert la_fc is not None
    # FIX-22 invariant in evaluators: forecast.forecast_date == event.market_date
    assert nyc_fc.forecast_date == nyc_market_date
    assert la_fc.forecast_date == la_market_date
