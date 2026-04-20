"""Tests for market discovery liquidity filtering."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.config import CityConfig
from src.markets.discovery import discover_weather_markets


def _make_city() -> CityConfig:
    return CityConfig(name="New York", icao="KLGA", lat=40.7, lon=-74.0)


def _make_gamma_response(volume: float = 5000, yes_price: float = 0.3, no_price: float = 0.7) -> list[dict]:
    from datetime import date, timedelta
    # Use tomorrow's date so the market is never in the past
    future = date.today() + timedelta(days=1)
    month_name = future.strftime("%B")  # e.g. "April"
    day_num = future.day
    end_date = future.isoformat() + "T23:00:00Z"
    return [{
        "id": "event-1",
        "conditionId": "cond-1",
        "title": f"Highest temperature in New York on {month_name} {day_num}",
        "volume": str(volume),
        "endDate": end_date,
        "markets": [{
            "question": "78°F to 81°F",
            "outcomes": ["Yes", "No"],
            "outcomePrices": [str(yes_price), str(no_price)],
            "clobTokenIds": ["token-yes-1", "token-no-1"],
        }],
    }]


@pytest.mark.asyncio
async def test_volume_filter_passes():
    """Markets above min_volume are included."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.side_effect = [_make_gamma_response(volume=1000), []]

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = resp

    events = await discover_weather_markets([_make_city()], client, min_volume=500)
    assert len(events) == 1
    assert events[0].volume == 1000


@pytest.mark.asyncio
async def test_volume_filter_blocks():
    """Markets below min_volume are skipped."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.side_effect = [_make_gamma_response(volume=100), []]

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = resp

    events = await discover_weather_markets([_make_city()], client, min_volume=500)
    assert len(events) == 0


@pytest.mark.asyncio
async def test_spread_filter_blocks_illiquid_slots():
    """Slots with high YES/NO spread are filtered out."""
    # YES=0.1, NO=0.5 → spread = |1 - 0.1 - 0.5| = 0.4
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.side_effect = [_make_gamma_response(yes_price=0.1, no_price=0.5), []]

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = resp

    events = await discover_weather_markets([_make_city()], client, max_spread=0.15)
    # All slots filtered → no event
    assert len(events) == 0


@pytest.mark.asyncio
async def test_spread_filter_passes_liquid_slots():
    """Slots with tight YES/NO spread are included."""
    # YES=0.3, NO=0.7 → spread = |1 - 0.3 - 0.7| = 0.0
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.side_effect = [_make_gamma_response(yes_price=0.3, no_price=0.7), []]

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = resp

    events = await discover_weather_markets([_make_city()], client, max_spread=0.15)
    assert len(events) == 1
    assert events[0].slots[0].spread is not None
    assert events[0].slots[0].spread <= 0.15


# ──────────────────────────────────────────────────────────────────────
# D1 (2026-04-20): drop slots with invalid NO prices (0.0 / 1.0) at
# the discovery layer so downstream consumers can trust the range.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_no_price_slot_filtered():
    """NO price 0 (illiquid Gamma response) → slot dropped at discovery."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.side_effect = [_make_gamma_response(yes_price=0.5, no_price=0.0), []]

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = resp

    events = await discover_weather_markets([_make_city()], client)
    # All slots filtered → event dropped (no slots left)
    assert len(events) == 0


@pytest.mark.asyncio
async def test_one_no_price_slot_filtered():
    """NO price 1 (already-resolved YES) → slot dropped at discovery."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.side_effect = [_make_gamma_response(yes_price=0.0, no_price=1.0), []]

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = resp

    events = await discover_weather_markets([_make_city()], client)
    assert len(events) == 0


@pytest.mark.asyncio
async def test_valid_no_price_kept():
    """A normal NO price in (0, 1) still reaches the event's slots list."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.side_effect = [_make_gamma_response(yes_price=0.3, no_price=0.7), []]

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = resp

    events = await discover_weather_markets([_make_city()], client)
    assert len(events) == 1
    assert events[0].slots[0].price_no == 0.7
