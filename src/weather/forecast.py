"""Multi-source weather forecast engine.

Priority: NWS (official resolution source) → Open-Meteo Ensemble → Open-Meteo single model.
When multiple sources are available, produces a weighted forecast with proper uncertainty.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone

import httpx

from src.config import CityConfig
from src.weather.http_utils import fetch_with_retry
from src.weather.models import Forecast
from src.weather.nws import get_nws_forecast

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

ENSEMBLE_MODELS = [
    "gfs_seamless_ensemble",
    "icon_seamless_ensemble",
    "ecmwf_ifs025_ensemble",
]

DEFAULT_CONFIDENCE_F = 4.0

# Last successful forecast cache (fallback on API failure)
_last_forecast_cache: dict[str, Forecast] = {}


async def get_ensemble_forecast(
    city: CityConfig,
    target_date: date | None = None,
    client: httpx.AsyncClient | None = None,
) -> Forecast | None:
    """Get ensemble forecast from Open-Meteo (GFS + ICON + ECMWF)."""
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
            return None

        ensemble_mean = sum(all_highs) / len(all_highs)
        ensemble_std = math.sqrt(
            sum((h - ensemble_mean) ** 2 for h in all_highs) / len(all_highs)
        ) if len(all_highs) > 1 else DEFAULT_CONFIDENCE_F

        # Inter-model spread
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
            predicted_low_f=ensemble_mean - 15,
            confidence_interval_f=max(ensemble_std, 1.0),
            source=f"ensemble({model_count}m,{len(all_highs)}mem)",
            fetched_at=datetime.now(timezone.utc),
            ensemble_spread_f=round(inter_model_spread, 2) if inter_model_spread else None,
            model_count=model_count,
        )
    except Exception:
        logger.debug("Ensemble forecast failed for %s", city.name)
        return None
    finally:
        if should_close:
            await client.aclose()


async def get_single_forecast(
    city: CityConfig,
    target_date: date | None = None,
    client: httpx.AsyncClient | None = None,
) -> Forecast | None:
    """Get single-model Open-Meteo forecast (last resort fallback)."""
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
        return Forecast(
            city=city.name,
            forecast_date=target,
            predicted_high_f=daily["temperature_2m_max"][0],
            predicted_low_f=daily["temperature_2m_min"][0],
            confidence_interval_f=DEFAULT_CONFIDENCE_F,
            source="open-meteo",
            fetched_at=datetime.now(timezone.utc),
        )
    except Exception:
        logger.debug("Single-model forecast failed for %s", city.name)
        return None
    finally:
        if should_close:
            await client.aclose()


async def get_forecast(
    city: CityConfig,
    target_date: date | None = None,
    client: httpx.AsyncClient | None = None,
) -> Forecast:
    """Multi-source forecast with priority chain: NWS → Ensemble → Single → Cache.

    When both NWS and Ensemble are available, produces a weighted average:
    - NWS: 50% weight (official resolution source, most aligned with settlement)
    - Ensemble mean: 50% weight (multi-model consensus)
    - Confidence = ensemble std (data-driven uncertainty)
    """
    target = target_date or date.today()
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=30)

    try:
        # Fetch NWS and Ensemble in parallel
        import asyncio
        nws_task = get_nws_forecast(city, target, client)
        ensemble_task = get_ensemble_forecast(city, target, client)
        nws_fc, ensemble_fc = await asyncio.gather(nws_task, ensemble_task, return_exceptions=True)

        # Handle exceptions
        if isinstance(nws_fc, Exception):
            nws_fc = None
        if isinstance(ensemble_fc, Exception):
            ensemble_fc = None

        # Combine available sources
        if nws_fc and ensemble_fc:
            # Best case: both sources → weighted average
            weighted_high = nws_fc.predicted_high_f * 0.5 + ensemble_fc.predicted_high_f * 0.5
            forecast = Forecast(
                city=city.name,
                forecast_date=target,
                predicted_high_f=round(weighted_high, 1),
                predicted_low_f=ensemble_fc.predicted_low_f,
                confidence_interval_f=ensemble_fc.confidence_interval_f,
                source=f"nws+{ensemble_fc.source}",
                fetched_at=datetime.now(timezone.utc),
                ensemble_spread_f=ensemble_fc.ensemble_spread_f,
                model_count=(ensemble_fc.model_count or 0) + 1,
            )
            logger.info(
                "Forecast %s: NWS=%.1f°F, Ensemble=%.1f°F → Weighted=%.1f°F (±%.1f°F)",
                city.name, nws_fc.predicted_high_f, ensemble_fc.predicted_high_f,
                weighted_high, ensemble_fc.confidence_interval_f,
            )
        elif nws_fc:
            forecast = nws_fc
            logger.info("Forecast %s: NWS only %.1f°F", city.name, nws_fc.predicted_high_f)
        elif ensemble_fc:
            forecast = ensemble_fc
            logger.info("Forecast %s: Ensemble only %.1f°F", city.name, ensemble_fc.predicted_high_f)
        else:
            # Try single model
            single = await get_single_forecast(city, target, client)
            if single:
                forecast = single
                logger.warning("Forecast %s: Single model fallback %.1f°F", city.name, single.predicted_high_f)
            else:
                # Last resort: cached forecast
                cached = _last_forecast_cache.get(city.name)
                if cached:
                    logger.warning("Forecast %s: Using cached forecast (all APIs failed)", city.name)
                    forecast = cached
                else:
                    raise RuntimeError(f"All forecast sources failed for {city.name}")

        # Update cache
        _last_forecast_cache[city.name] = forecast
        return forecast

    finally:
        if should_close:
            await client.aclose()


async def get_forecasts_batch(
    cities: list[CityConfig],
    target_date: date | None = None,
) -> dict[str, Forecast]:
    """Fetch multi-source forecasts for all cities concurrently."""
    import asyncio

    async with httpx.AsyncClient(timeout=30) as client:
        tasks = [get_forecast(city, target_date, client) for city in cities]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    forecasts: dict[str, Forecast] = {}
    for city, result in zip(cities, results):
        if isinstance(result, Exception):
            logger.error("Forecast failed for %s: %s", city.name, result)
        else:
            forecasts[city.name] = result
    return forecasts
