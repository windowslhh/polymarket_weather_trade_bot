"""Tests for discovery.py date filtering using city-local timezone.

Critical scenario: during UTC midnight crossover (00:00–06:00 UTC), west coast
US cities (PDT = UTC-7, PST = UTC-8) are still on the *previous* calendar day.
A market dated "today" (city-local) must NOT be filtered out as "past".
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.config import CityConfig
from src.markets.discovery import discover_weather_markets


# ── Helpers ───────────────────────────────────────────────────────────────────

def _city(name: str = "Los Angeles", tz: str = "America/Los_Angeles") -> CityConfig:
    return CityConfig(name=name, icao="KLAX", lat=34.05, lon=-118.24, tz=tz)


def _event_payload(market_date: date, city: str = "Los Angeles") -> list[dict]:
    """Minimal Gamma API response for a single market on *market_date*."""
    month = market_date.strftime("%B")
    day = market_date.day
    return [{
        "id": "event-utc-test",
        "conditionId": "cond-utc-test",
        "title": f"Highest temperature in {city} on {month} {day}",
        "volume": "5000",
        "endDate": f"{market_date.isoformat()}T23:00:00Z",
        "markets": [{
            "question": "78°F to 81°F",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.30", "0.70"],
            "clobTokenIds": ["tok-yes", "tok-no"],
        }],
    }]


def _mock_client(payload: list[dict]) -> AsyncMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    # First call returns the event; second call returns [] (end of pagination)
    resp.json.side_effect = [payload, []]
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = resp
    return client


# ── 1. UTC midnight crossover — west coast city ───────────────────────────────

class TestUTCMidnightCrossover:

    @pytest.mark.asyncio
    async def test_la_same_day_market_not_filtered_during_utc_midnight(self):
        """At 00:30 UTC on April 12, LA is still April 11 (PDT=UTC-7).

        A market dated April 11 must NOT be filtered out as "past".
        """
        # Simulate: UTC is 2026-04-12 00:30, LA (PDT, UTC-7) is 2026-04-11 17:30
        utc_now = datetime(2026, 4, 12, 0, 30, tzinfo=timezone.utc)
        la_date = date(2026, 4, 11)  # city-local date for LA at that UTC moment

        market_date = la_date  # same-day market for LA
        payload = _event_payload(market_date)

        with patch("src.markets.discovery.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: utc_now.astimezone(tz) if tz else utc_now
            mock_dt.strptime = datetime.strptime
            mock_dt.fromisoformat = datetime.fromisoformat

            events = await discover_weather_markets(
                [_city("Los Angeles", "America/Los_Angeles")],
                _mock_client(payload),
                max_days_ahead=2,
            )

        assert len(events) == 1, (
            f"Same-day LA market (April 11) should NOT be filtered "
            f"when UTC is already April 12 00:30"
        )

    @pytest.mark.asyncio
    async def test_la_yesterday_market_filtered_during_utc_midnight(self):
        """At 00:30 UTC on April 12, LA is April 11.

        A market dated April 10 (city-local yesterday) MUST be filtered out.
        """
        utc_now = datetime(2026, 4, 12, 0, 30, tzinfo=timezone.utc)
        market_date = date(2026, 4, 10)  # yesterday for LA
        payload = _event_payload(market_date)

        with patch("src.markets.discovery.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: utc_now.astimezone(tz) if tz else utc_now
            mock_dt.strptime = datetime.strptime
            mock_dt.fromisoformat = datetime.fromisoformat

            events = await discover_weather_markets(
                [_city("Los Angeles", "America/Los_Angeles")],
                _mock_client(payload),
                max_days_ahead=2,
            )

        assert len(events) == 0, "April 10 market must be filtered when LA is April 11"

    @pytest.mark.asyncio
    async def test_ny_uses_eastern_time_not_utc(self):
        """At 03:00 UTC on April 12, New York (EDT=UTC-4) is April 11 22:00.

        A market dated April 11 must NOT be filtered as past.
        """
        utc_now = datetime(2026, 4, 12, 3, 0, tzinfo=timezone.utc)
        market_date = date(2026, 4, 11)  # still today for NY (22:00 EDT)
        payload = _event_payload(market_date, city="New York")

        with patch("src.markets.discovery.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: utc_now.astimezone(tz) if tz else utc_now
            mock_dt.strptime = datetime.strptime
            mock_dt.fromisoformat = datetime.fromisoformat

            events = await discover_weather_markets(
                [_city("New York", "America/New_York")],
                _mock_client(payload),
                max_days_ahead=2,
            )

        assert len(events) == 1, "April 11 NY market must not be filtered at 22:00 EDT"


# ── 2. max_days_ahead uses city-local date ────────────────────────────────────

class TestMaxDaysAheadCityLocal:

    @pytest.mark.asyncio
    async def test_d2_market_not_filtered_at_utc_midnight(self):
        """At 01:00 UTC on April 12, LA is April 11.

        A market dated April 13 (LA+2 days) must pass the max_days_ahead=2 gate.
        If UTC date (April 12) were used, April 13 would be D+1 and pass too —
        but April 14 (LA's D+3) must be filtered.
        """
        utc_now = datetime(2026, 4, 12, 1, 0, tzinfo=timezone.utc)
        # LA local date = April 11 → D+2 = April 13
        d2_market = date(2026, 4, 13)
        d3_market = date(2026, 4, 14)

        for market_date, should_pass in [(d2_market, True), (d3_market, False)]:
            payload = _event_payload(market_date)
            with patch("src.markets.discovery.datetime") as mock_dt:
                mock_dt.now.side_effect = lambda tz=None: utc_now.astimezone(tz) if tz else utc_now
                mock_dt.strptime = datetime.strptime
                mock_dt.fromisoformat = datetime.fromisoformat

                events = await discover_weather_markets(
                    [_city("Los Angeles", "America/Los_Angeles")],
                    _mock_client(payload),
                    max_days_ahead=2,
                )

            if should_pass:
                assert len(events) == 1, f"D+2 market {market_date} must not be filtered"
            else:
                assert len(events) == 0, f"D+3 market {market_date} must be filtered"


# ── 3. Normal (daytime) operation unaffected ─────────────────────────────────

class TestNormalDaytimeOperation:

    @pytest.mark.asyncio
    async def test_past_market_always_filtered(self):
        """A market clearly in the past is filtered regardless of timezone."""
        utc_now = datetime(2026, 4, 12, 15, 0, tzinfo=timezone.utc)  # midday UTC
        market_date = date(2026, 4, 10)  # two days ago

        with patch("src.markets.discovery.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: utc_now.astimezone(tz) if tz else utc_now
            mock_dt.strptime = datetime.strptime
            mock_dt.fromisoformat = datetime.fromisoformat

            events = await discover_weather_markets(
                [_city()],
                _mock_client(_event_payload(market_date)),
                max_days_ahead=2,
            )

        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_today_market_always_passes(self):
        """A market dated city-local today always passes during daytime."""
        utc_now = datetime(2026, 4, 12, 15, 0, tzinfo=timezone.utc)
        # LA at 15:00 UTC = 08:00 PDT, still April 12
        market_date = date(2026, 4, 12)

        with patch("src.markets.discovery.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: utc_now.astimezone(tz) if tz else utc_now
            mock_dt.strptime = datetime.strptime
            mock_dt.fromisoformat = datetime.fromisoformat

            events = await discover_weather_markets(
                [_city()],
                _mock_client(_event_payload(market_date)),
                max_days_ahead=2,
            )

        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_city_without_tz_falls_back_gracefully(self):
        """If city.tz is empty, UTC is used as fallback — no crash."""
        utc_now = datetime(2026, 4, 12, 15, 0, tzinfo=timezone.utc)
        market_date = date(2026, 4, 12)
        city_no_tz = CityConfig(name="Los Angeles", icao="KLAX", lat=34.05, lon=-118.24, tz="")

        with patch("src.markets.discovery.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: utc_now.astimezone(tz) if tz else utc_now
            mock_dt.strptime = datetime.strptime
            mock_dt.fromisoformat = datetime.fromisoformat

            # Should not raise ZoneInfoNotFoundError
            events = await discover_weather_markets(
                [city_no_tz],
                _mock_client(_event_payload(market_date)),
                max_days_ahead=2,
            )

        # UTC fallback: April 12 UTC same as market_date → passes
        assert len(events) == 1
