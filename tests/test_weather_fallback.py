"""FIX-12: forecast cache TTL + settlement-temp daily-max fallback.

Two independent concerns bundled in one fix:
1. Open-Meteo cache: previously unbounded; now bounded TTL so a long
   outage can't silently keep the bot trading on stale forecasts.
   (2026-05-01: split into FRESH for in-cycle dedup + STALE for
   failure fallback, re-keyed by ``(city, target_date)`` so multi-day
   fetches don't overwrite each other.)
2. Settlement-temp: KBKF failure can substitute KDEN for daily_max
   tracking but NEVER for settlement judgment (Polymarket still
   resolves against KBKF).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.weather import forecast as forecast_mod
from src.weather import settlement as settle_mod
from src.weather.models import Forecast


def _mk_forecast(
    high: float = 75.0, target: date | None = None,
) -> Forecast:
    target = target or date.today()
    return Forecast(
        city="Denver", forecast_date=target,
        predicted_high_f=high, predicted_low_f=60.0,
        confidence_interval_f=3.0, source="test",
        fetched_at=datetime.now(timezone.utc),
    )


class TestForecastCacheTTL:

    def setup_method(self):
        forecast_mod._last_forecast_cache.clear()

    def test_fresh_cache_returns_forecast(self):
        f = _mk_forecast()
        d = date.today()
        forecast_mod._cache_forecast("NYC", d, f)
        # Default lookup uses the STALE window (failure-fallback semantics
        # — same as the pre-2026-05-01 single-TTL behaviour).
        assert forecast_mod._get_cached_forecast("NYC", d) is f

    def test_expired_cache_evicts_and_returns_none(self):
        f = _mk_forecast()
        d = date.today()
        stale_ts = datetime.now(timezone.utc) - timedelta(
            hours=forecast_mod.FORECAST_CACHE_STALE_HOURS + 0.1,
        )
        forecast_mod._last_forecast_cache[("NYC", d)] = (stale_ts, f)
        assert forecast_mod._get_cached_forecast("NYC", d) is None
        # Evicted on read once past the stale window — no silent
        # accumulation.
        assert ("NYC", d) not in forecast_mod._last_forecast_cache

    def test_missing_city_returns_none(self):
        assert forecast_mod._get_cached_forecast("Nowhere", date.today()) is None

    def test_separate_dates_do_not_overwrite_each_other(self):
        """2026-05-01 keying fix: fetching today's forecast and then
        D+1's must leave both reachable in the cache.  Pre-fix the cache
        was city-only-keyed and the second call clobbered the first.
        """
        today = date.today()
        d_plus_1 = today + timedelta(days=1)
        f_today = _mk_forecast(75.0, target=today)
        f_d1 = _mk_forecast(80.0, target=d_plus_1)
        forecast_mod._cache_forecast("NYC", today, f_today)
        forecast_mod._cache_forecast("NYC", d_plus_1, f_d1)
        assert forecast_mod._get_cached_forecast("NYC", today) is f_today
        assert forecast_mod._get_cached_forecast("NYC", d_plus_1) is f_d1

    def test_fresh_only_lookup_misses_outside_fresh_window(self):
        """``fresh_only=True`` (the in-cycle dedup hot path) must miss
        when the entry is older than FRESH but younger than STALE — the
        stale-fallback path will still find it later in the same call.
        """
        f = _mk_forecast()
        d = date.today()
        # Age the entry 1.0h: past FRESH (0.75h) but inside STALE (3.0h).
        ts = datetime.now(timezone.utc) - timedelta(hours=1.0)
        forecast_mod._last_forecast_cache[("NYC", d)] = (ts, f)

        assert forecast_mod._get_cached_forecast(
            "NYC", d, fresh_only=True,
        ) is None
        # Entry NOT evicted — stale fallback can still see it.
        assert ("NYC", d) in forecast_mod._last_forecast_cache
        assert forecast_mod._get_cached_forecast("NYC", d) is f

    def test_fresh_only_lookup_hits_inside_fresh_window(self):
        """Inside FRESH, fresh_only=True returns the cached forecast —
        this is the hot-path dedup that makes the 27-fetch / cycle
        burst collapse to ~9 fetches once cache is warm.
        """
        f = _mk_forecast()
        d = date.today()
        forecast_mod._cache_forecast("NYC", d, f)
        assert forecast_mod._get_cached_forecast(
            "NYC", d, fresh_only=True,
        ) is f


class TestSettlementDailyMaxFallback:

    @pytest.mark.asyncio
    async def test_kbkf_success_used_directly(self):
        """Primary KBKF fetch succeeds → no fallback attempted."""
        client = MagicMock()
        # Accept any httpx.Response-like object via our helper; we patch
        # _fetch_metar_at directly to keep the test tight.
        async def fake_fetch(icao, c):
            assert icao == "KBKF"  # never reaches fallback
            return {
                "temp": 22.0, "reportTime": "2026-04-25T14:00Z",
                "rawOb": "KBKF 2514Z ...",
            }

        with patch.object(settle_mod, "_fetch_metar_at", side_effect=fake_fetch):
            obs = await settle_mod.fetch_settlement_temp(
                "Denver", client=client,
            )
        assert obs is not None
        assert obs.icao == "KBKF"

    @pytest.mark.asyncio
    async def test_kbkf_failure_falls_back_to_kden_for_daily_max(self):
        """When KBKF fails, KDEN is queried for the daily_max path."""
        client = MagicMock()

        calls: list[str] = []

        async def fake_fetch(icao, c):
            calls.append(icao)
            if icao == "KBKF":
                raise RuntimeError("METAR unreachable")
            return {
                "temp": 20.0, "reportTime": "2026-04-25T14:00Z",
                "rawOb": "KDEN 2514Z ...",
            }

        with patch.object(settle_mod, "_fetch_metar_at", side_effect=fake_fetch):
            obs = await settle_mod.fetch_settlement_temp(
                "Denver", client=client,
            )
        assert calls == ["KBKF", "KDEN"]
        assert obs is not None
        assert obs.icao == "KDEN"  # fallback used, logged as such

    @pytest.mark.asyncio
    async def test_settlement_mode_never_falls_back(self):
        """for_settlement=True restricts to the primary ICAO only — a
        settlement judgment must only ever use Polymarket's exact station.
        """
        client = MagicMock()

        calls: list[str] = []

        async def fake_fetch(icao, c):
            calls.append(icao)
            if icao == "KBKF":
                raise RuntimeError("METAR down")
            return {"temp": 20.0, "reportTime": ""}

        with patch.object(settle_mod, "_fetch_metar_at", side_effect=fake_fetch):
            obs = await settle_mod.fetch_settlement_temp(
                "Denver", client=client, for_settlement=True,
            )
        assert calls == ["KBKF"], "for_settlement must not attempt fallback"
        assert obs is None

    @pytest.mark.asyncio
    async def test_non_fallback_city_primary_only(self):
        """A city without a DAILY_MAX_FALLBACK_ICAO entry stays primary-only
        even for daily_max mode."""
        client = MagicMock()
        calls: list[str] = []

        async def fake_fetch(icao, c):
            calls.append(icao)
            raise RuntimeError("boom")

        with patch.object(settle_mod, "_fetch_metar_at", side_effect=fake_fetch):
            obs = await settle_mod.fetch_settlement_temp(
                "Chicago", client=client,
            )
        assert calls == ["KORD"]
        assert obs is None
