"""Fetch weather forecasts from Open-Meteo (free, no API key needed)."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import httpx

from src.config import CityConfig
from src.weather.models import Forecast

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Default confidence interval when ensemble data isn't available
DEFAULT_CONFIDENCE_F = 4.0


async def get_forecast(
    city: CityConfig,
    target_date: date | None = None,
    client: httpx.AsyncClient | None = None,
) -> Forecast:
    """Get the forecast high/low temperature for a city on a given date.

    Uses Open-Meteo API which is free and requires no API key.
    """
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
        resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

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
    """Fetch forecasts for multiple cities concurrently."""
    import asyncio

    async with httpx.AsyncClient(timeout=15) as client:
        tasks = [get_forecast(city, target_date, client) for city in cities]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    forecasts: dict[str, Forecast] = {}
    for city, result in zip(cities, results):
        if isinstance(result, Exception):
            logger.error("Forecast failed for %s: %s", city.name, result)
        else:
            forecasts[city.name] = result
    return forecasts
