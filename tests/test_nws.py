"""Tests for NWS forecast integration."""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import CityConfig
from src.weather.nws import get_nws_forecast, _gridpoint_cache


def _city():
    return CityConfig(name="Dallas", icao="KDFW", lat=32.78, lon=-96.80)


def _make_points_response():
    return {"properties": {"forecast": "https://api.weather.gov/gridpoints/FWD/84,108/forecast"}}


def _make_forecast_response(temp=67, unit="F"):
    return {
        "properties": {
            "periods": [
                {
                    "startTime": "2026-04-05T06:00:00-05:00",
                    "isDaytime": True,
                    "temperature": temp,
                    "temperatureUnit": unit,
                },
                {
                    "startTime": "2026-04-05T18:00:00-05:00",
                    "isDaytime": False,
                    "temperature": 52,
                    "temperatureUnit": unit,
                },
            ]
        }
    }


@pytest.fixture(autouse=True)
def clear_cache():
    _gridpoint_cache.clear()
    yield
    _gridpoint_cache.clear()


@pytest.mark.asyncio
async def test_nws_forecast_success():
    client = AsyncMock()
    resp1 = MagicMock()
    resp1.json.return_value = _make_points_response()
    resp1.raise_for_status.return_value = None

    resp2 = MagicMock()
    resp2.json.return_value = _make_forecast_response(67)
    resp2.raise_for_status.return_value = None

    client.get.side_effect = [resp1, resp2]

    fc = await get_nws_forecast(_city(), date(2026, 4, 5), client)
    assert fc is not None
    assert fc.predicted_high_f == 67.0
    assert fc.source == "nws"


@pytest.mark.asyncio
async def test_nws_returns_none_on_failure():
    client = AsyncMock()
    client.get.side_effect = Exception("Network error")

    fc = await get_nws_forecast(_city(), date(2026, 4, 5), client)
    assert fc is None


@pytest.mark.asyncio
async def test_nws_gridpoint_cached():
    """Second call should use cached gridpoint URL."""
    _gridpoint_cache["32.7800,-96.8000"] = "https://api.weather.gov/gridpoints/FWD/84,108/forecast"

    client = AsyncMock()
    resp = MagicMock()
    resp.json.return_value = _make_forecast_response(70)
    resp.raise_for_status.return_value = None
    client.get.return_value = resp

    fc = await get_nws_forecast(_city(), date(2026, 4, 5), client)
    assert fc is not None
    assert fc.predicted_high_f == 70.0
    # Only one call (forecast), not two (no points lookup)
    assert client.get.call_count == 1
