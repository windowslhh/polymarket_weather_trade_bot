"""National Weather Service (api.weather.gov) forecast integration.

NWS is the official resolution source for Polymarket US city temperature markets.
Free, no API key required — only needs a User-Agent header.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import httpx

from src.config import CityConfig
from src.weather.models import Forecast

logger = logging.getLogger(__name__)

NWS_BASE = "https://api.weather.gov"
NWS_HEADERS = {"User-Agent": "(polymarket-weather-bot, contact@example.com)", "Accept": "application/geo+json"}

# Cache: lat,lon → gridpoint forecast URL (never changes for a location)
_gridpoint_cache: dict[str, str] = {}


async def _get_gridpoint_url(lat: float, lon: float, client: httpx.AsyncClient) -> str | None:
    """Get the NWS gridpoint forecast URL for a lat/lon. Cached permanently."""
    key = f"{lat:.4f},{lon:.4f}"
    if key in _gridpoint_cache:
        return _gridpoint_cache[key]

    try:
        resp = await client.get(
            f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}",
            headers=NWS_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        forecast_url = data.get("properties", {}).get("forecast")
        if forecast_url:
            _gridpoint_cache[key] = forecast_url
            return forecast_url
    except Exception:
        logger.debug("NWS gridpoint lookup failed for %s", key)
    return None


async def get_nws_forecast(
    city: CityConfig,
    target_date: date | None = None,
    client: httpx.AsyncClient | None = None,
) -> Forecast | None:
    """Fetch daily high temperature forecast from NWS.

    Returns None on failure (caller should fallback to Open-Meteo).
    """
    # C-3: UTC-anchored fallback (see src/weather/forecast.py for rationale)
    target = target_date or datetime.now(timezone.utc).date()
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=15, headers=NWS_HEADERS)

    try:
        forecast_url = await _get_gridpoint_url(city.lat, city.lon, client)
        if not forecast_url:
            return None

        resp = await client.get(forecast_url, headers=NWS_HEADERS)
        resp.raise_for_status()
        data = resp.json()

        periods = data.get("properties", {}).get("periods", [])
        if not periods:
            return None

        # Find the period matching target_date with isDaytime=True
        for period in periods:
            start = period.get("startTime", "")
            try:
                period_date = datetime.fromisoformat(start).date()
            except (ValueError, TypeError):
                continue

            if period_date == target and period.get("isDaytime", False):
                temp = period.get("temperature")
                unit = period.get("temperatureUnit", "F")
                if temp is None:
                    continue

                temp_f = float(temp) if unit == "F" else float(temp) * 9 / 5 + 32

                return Forecast(
                    city=city.name,
                    forecast_date=target,
                    predicted_high_f=temp_f,
                    predicted_low_f=temp_f - 15,  # NWS daytime doesn't give low
                    confidence_interval_f=3.0,  # NWS point forecast, moderate confidence
                    source="nws",
                    fetched_at=datetime.now(timezone.utc),
                )

        # Target date not in forecast range (NWS only covers ~7 days)
        return None

    except Exception:
        logger.debug("NWS forecast failed for %s", city.name)
        return None
    finally:
        if should_close:
            await client.aclose()


async def get_nws_forecasts_batch(
    cities: list[CityConfig],
    target_date: date | None = None,
) -> dict[str, Forecast]:
    """Fetch NWS forecasts for multiple cities. Returns available forecasts."""
    import asyncio

    async with httpx.AsyncClient(timeout=15, headers=NWS_HEADERS) as client:
        tasks = [get_nws_forecast(city, target_date, client) for city in cities]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    forecasts: dict[str, Forecast] = {}
    for city, result in zip(cities, results):
        if isinstance(result, Forecast):
            forecasts[city.name] = result
        elif isinstance(result, Exception):
            logger.debug("NWS forecast error for %s: %s", city.name, result)
    return forecasts
