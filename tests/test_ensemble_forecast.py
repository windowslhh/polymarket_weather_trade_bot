"""Tests for multi-model ensemble forecasting."""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import CityConfig
from src.weather.forecast import (
    DEFAULT_CONFIDENCE_F,
    get_ensemble_forecast,
    get_forecast,
    get_forecasts_batch,
)


def _city() -> CityConfig:
    return CityConfig(name="TestCity", icao="KTEST", lat=40.0, lon=-74.0)


def _make_ensemble_response(highs_per_model: dict[str, list[float]]) -> dict:
    """Build a mock Open-Meteo ensemble API response."""
    daily = {"time": ["2026-04-05"]}
    for model_key, values in highs_per_model.items():
        daily[f"temperature_2m_max_{model_key}"] = values
    return {"daily": daily}


@pytest.mark.asyncio
async def test_ensemble_mean_and_std():
    """Ensemble mean and std computed from all members across models."""
    resp = _make_ensemble_response({
        "gfs_seamless_ensemble": [70.0, 72.0, 74.0],
        "icon_seamless_ensemble": [71.0, 73.0],
        "ecmwf_ifs025_ensemble": [72.0, 74.0, 76.0],
    })
    # All values: [70,72,74,71,73,72,74,76] → mean=72.75
    with patch("src.weather.forecast.fetch_with_retry", return_value=resp):
        forecast = await get_ensemble_forecast(_city(), date(2026, 4, 5))

    assert forecast.model_count == 3
    assert abs(forecast.predicted_high_f - 72.8) < 0.5  # approx mean
    assert forecast.confidence_interval_f > 1.0  # real spread, not default 4.0
    assert forecast.ensemble_spread_f is not None
    assert "ensemble" in forecast.source


@pytest.mark.asyncio
async def test_ensemble_single_model_fallback_confidence():
    """When only one value, confidence should use default."""
    resp = _make_ensemble_response({"gfs": [72.0]})
    with patch("src.weather.forecast.fetch_with_retry", return_value=resp):
        forecast = await get_ensemble_forecast(_city(), date(2026, 4, 5))

    assert forecast.predicted_high_f == 72.0
    assert forecast.confidence_interval_f == DEFAULT_CONFIDENCE_F


@pytest.mark.asyncio
async def test_ensemble_fallback_to_single_model():
    """On ensemble API failure, falls back to single-model forecast."""
    single_resp = {
        "daily": {
            "temperature_2m_max": [68.0],
            "temperature_2m_min": [55.0],
        }
    }

    call_count = 0

    async def mock_fetch(client, url, params, **kwargs):
        nonlocal call_count
        call_count += 1
        if "ensemble" in url:
            raise Exception("Ensemble API down")
        return single_resp

    with patch("src.weather.forecast.fetch_with_retry", side_effect=mock_fetch):
        forecast = await get_ensemble_forecast(_city(), date(2026, 4, 5))

    assert forecast.source == "open-meteo"
    assert forecast.predicted_high_f == 68.0
    assert forecast.model_count == 1
    assert call_count == 2  # one ensemble attempt, one fallback


@pytest.mark.asyncio
async def test_ensemble_skips_none_values():
    """None values in ensemble response are ignored."""
    resp = _make_ensemble_response({
        "gfs": [70.0, None, 74.0],
        "icon": [None, 72.0],
    })
    with patch("src.weather.forecast.fetch_with_retry", return_value=resp):
        forecast = await get_ensemble_forecast(_city(), date(2026, 4, 5))

    assert forecast.model_count == 2
    # Valid values: [70, 74, 72] → mean=72
    assert abs(forecast.predicted_high_f - 72.0) < 0.5


@pytest.mark.asyncio
async def test_batch_uses_ensemble():
    """get_forecasts_batch should use ensemble forecasts."""
    resp = _make_ensemble_response({
        "gfs": [75.0, 76.0],
        "icon": [74.0, 77.0],
    })
    with patch("src.weather.forecast.fetch_with_retry", return_value=resp):
        results = await get_forecasts_batch([_city()])

    assert "TestCity" in results
    assert results["TestCity"].model_count == 2
