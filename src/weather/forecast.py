"""Fetch weather forecasts from Open-Meteo (free, no API key needed).

Supports multi-model ensemble forecasts for better uncertainty quantification.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone

import httpx

from src.config import CityConfig
from src.weather.http_utils import fetch_with_retry
from src.weather.models import Forecast

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Models to include in the ensemble
ENSEMBLE_MODELS = [
    "gfs_seamless_ensemble",
    "icon_seamless_ensemble",
    "ecmwf_ifs025_ensemble",
]

# Default confidence interval when ensemble data isn't available
DEFAULT_CONFIDENCE_F = 4.0


async def get_ensemble_forecast(
    city: CityConfig,
    target_date: date | None = None,
    client: httpx.AsyncClient | None = None,
) -> Forecast:
    """Get ensemble forecast by combining multiple weather models.

    Fetches temperature_2m_max from GFS, ICON, and ECMWF ensemble members,
    then computes the mean and standard deviation across all members.
    Falls back to single-model forecast on failure.
    """
    target = target_date or date.today()
    params = {
        "latitude": city.lat,
        "longitude": city.lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "start_date": target.isoformat(),
        "end_date": target.isoformat(),
        "models": ",".join(ENSEMBLE_MODELS),
    }

    should_close = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        data = await fetch_with_retry(client, ENSEMBLE_URL, params)
        daily = data.get("daily", {})

        # Collect all ensemble member values across all models
        all_highs: list[float] = []
        model_count = 0
        for key, values in daily.items():
            if key.startswith("temperature_2m_max") and isinstance(values, list):
                for v in values:
                    if v is not None:
                        all_highs.append(float(v))
                if values:
                    model_count += 1

        if not all_highs:
            raise ValueError(f"No ensemble temperature data for {city.name}")

        ensemble_mean = sum(all_highs) / len(all_highs)
        ensemble_std = math.sqrt(
            sum((h - ensemble_mean) ** 2 for h in all_highs) / len(all_highs)
        ) if len(all_highs) > 1 else DEFAULT_CONFIDENCE_F

        # Inter-model spread: std of per-model means
        model_means: list[float] = []
        for key, values in daily.items():
            if key.startswith("temperature_2m_max") and isinstance(values, list):
                valid = [float(v) for v in values if v is not None]
                if valid:
                    model_means.append(sum(valid) / len(valid))
        inter_model_spread = math.sqrt(
            sum((m - ensemble_mean) ** 2 for m in model_means) / len(model_means)
        ) if len(model_means) > 1 else None

        return Forecast(
            city=city.name,
            forecast_date=target,
            predicted_high_f=round(ensemble_mean, 1),
            predicted_low_f=ensemble_mean - 15,  # rough estimate, not critical
            confidence_interval_f=max(ensemble_std, 1.0),
            source=f"ensemble({model_count}models,{len(all_highs)}members)",
            fetched_at=datetime.now(timezone.utc),
            ensemble_spread_f=round(inter_model_spread, 2) if inter_model_spread else None,
            model_count=model_count,
        )
    except Exception:
        logger.warning("Ensemble forecast failed for %s, falling back to single model", city.name)
        return await get_forecast(city, target, client)
    finally:
        if should_close:
            await client.aclose()


async def get_forecast(
    city: CityConfig,
    target_date: date | None = None,
    client: httpx.AsyncClient | None = None,
) -> Forecast:
    """Get single-model forecast (fallback when ensemble unavailable)."""
    target = target_date or date.today()
    params = {
        "latitude": city.lat,
        "longitude": city.lon,
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "start_date": target.isoformat(),
        "end_date": target.isoformat(),
    }

    should_close = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        data = await fetch_with_retry(client, OPEN_METEO_URL, params)

        daily = data["daily"]
        high = daily["temperature_2m_max"][0]
        low = daily["temperature_2m_min"][0]

        return Forecast(
            city=city.name,
            forecast_date=target,
            predicted_high_f=high,
            predicted_low_f=low,
            confidence_interval_f=DEFAULT_CONFIDENCE_F,
            source="open-meteo",
            fetched_at=datetime.now(timezone.utc),
        )
    except Exception:
        logger.exception("Failed to fetch forecast for %s", city.name)
        raise
    finally:
        if should_close:
            await client.aclose()


async def get_forecasts_batch(
    cities: list[CityConfig],
    target_date: date | None = None,
) -> dict[str, Forecast]:
    """Fetch ensemble forecasts for multiple cities concurrently."""
    import asyncio

    async with httpx.AsyncClient(timeout=30) as client:
        tasks = [get_ensemble_forecast(city, target_date, client) for city in cities]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    forecasts: dict[str, Forecast] = {}
    for city, result in zip(cities, results):
        if isinstance(result, Exception):
            logger.error("Forecast failed for %s: %s", city.name, result)
        else:
            forecasts[city.name] = result
    return forecasts
