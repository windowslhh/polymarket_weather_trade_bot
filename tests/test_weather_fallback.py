"""FIX-12: forecast cache TTL + settlement-temp daily-max fallback.

Two independent concerns bundled in one fix:
1. Open-Meteo cache: previously unbounded; now 3h TTL so a long outage
   can't silently keep the bot trading on stale forecasts.
2. Settlement-temp: KBKF failure can substitute KDEN for daily_max
   tracking but NEVER for settlement judgment (Polymarket still
   resolves against KBKF).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.weather import forecast as forecast_mod
from src.weather import settlement as settle_mod
from src.weather.models import Forecast


def _mk_forecast(high: float = 75.0) -> Forecast:
    from datetime import date
    return Forecast(
        city="Denver", forecast_date=date.today(),
        predicted_high_f=high, predicted_low_f=60.0,
        confidence_interval_f=3.0, source="test",
        fetched_at=datetime.now(timezone.utc),
    )


class TestForecastCacheTTL:

    def setup_method(self):
        forecast_mod._last_forecast_cache.clear()

    def test_fresh_cache_returns_forecast(self):
        f = _mk_forecast()
        forecast_mod._cache_forecast("NYC", f)
        assert forecast_mod._get_cached_forecast("NYC") is f

    def test_expired_cache_evicts_and_returns_none(self):
        f = _mk_forecast()
        stale_ts = datetime.now(timezone.utc) - timedelta(
            hours=forecast_mod.FORECAST_CACHE_TTL_HOURS + 0.1,
        )
        forecast_mod._last_forecast_cache["NYC"] = (stale_ts, f)
        assert forecast_mod._get_cached_forecast("NYC") is None
        # Evicted on read — no silent accumulation.
        assert "NYC" not in forecast_mod._last_forecast_cache

    def test_missing_city_returns_none(self):
        assert forecast_mod._get_cached_forecast("Nowhere") is None


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
