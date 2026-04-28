"""Multi-source weather forecast engine.

Priority: NWS (official resolution source) → Open-Meteo Ensemble → Open-Meteo single model.
When multiple sources are available, produces a weighted forecast with proper uncertainty.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from src.config import CityConfig
from src.weather.http_utils import fetch_with_retry
from src.weather.models import Forecast
from src.weather.nws import get_nws_forecast

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

ENSEMBLE_MODELS = [
    "gfs_seamless",
    "icon_seamless",
    "ecmwf_ifs025",
]

DEFAULT_CONFIDENCE_F = 4.0

# FIX-12: fallback cache now carries a timestamp so stale-on-failure reuse is
# bounded.  Before, an API outage lasting longer than a day would keep the bot
# trading on yesterday's forecast.  The 3-hour TTL is generous enough to ride
# out transient Open-Meteo hiccups without freezing decisions on a dead cache.
FORECAST_CACHE_TTL_HOURS = 3.0
_last_forecast_cache: dict[str, tuple[datetime, Forecast]] = {}


def _cache_forecast(city_name: str, forecast: Forecast) -> None:
    """Store a successful forecast with its wall-clock timestamp."""
    from datetime import datetime, timezone as _tz
    _last_forecast_cache[city_name] = (datetime.now(_tz.utc), forecast)


def _get_cached_forecast(city_name: str) -> Forecast | None:
    """Return the cached forecast only if it's inside the TTL window."""
    from datetime import datetime, timedelta, timezone as _tz
    entry = _last_forecast_cache.get(city_name)
    if entry is None:
        return None
    ts, forecast = entry
    if datetime.now(_tz.utc) - ts > timedelta(hours=FORECAST_CACHE_TTL_HOURS):
        # Evict to prevent unbounded staleness reuse.
        _last_forecast_cache.pop(city_name, None)
        return None
    return forecast


async def get_ensemble_forecast(
    city: CityConfig,
    target_date: date,
    client: httpx.AsyncClient | None = None,
) -> Forecast | None:
    """Get ensemble forecast from Open-Meteo (GFS + ICON + ECMWF)."""
    if target_date is None:
        raise ValueError(
            "target_date is required — pass city_local_date(city) for the "
            "city-local 'today' rather than relying on a global default."
        )
    target = target_date
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

        # Collect all member highs for ensemble statistics.
        # API returns keys like:
        #   temperature_2m_max_ncep_gefs_seamless          (model mean)
        #   temperature_2m_max_member01_ncep_gefs_seamless  (member value)
        # We use ALL values (means + members) for the overall ensemble mean,
        # but only non-member keys for model_count and inter-model spread.
        all_highs: list[float] = []
        model_count = 0
        for key, values in daily.items():
            if key.startswith("temperature_2m_max") and isinstance(values, list):
                for v in values:
                    if v is not None:
                        all_highs.append(float(v))
                # Only count non-member keys as distinct models
                if values and "member" not in key:
                    model_count += 1

        if not all_highs:
            return None

        ensemble_mean = sum(all_highs) / len(all_highs)
        ensemble_std = math.sqrt(
            sum((h - ensemble_mean) ** 2 for h in all_highs) / len(all_highs)
        ) if len(all_highs) > 1 else DEFAULT_CONFIDENCE_F

        # Inter-model spread — only from model mean keys (not individual members)
        model_means: list[float] = []
        for key, values in daily.items():
            if (key.startswith("temperature_2m_max")
                    and isinstance(values, list)
                    and "member" not in key):
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
    target_date: date,
    client: httpx.AsyncClient | None = None,
) -> Forecast | None:
    """Get single-model Open-Meteo forecast (last resort fallback)."""
    if target_date is None:
        raise ValueError(
            "target_date is required — pass city_local_date(city) for the "
            "city-local 'today' rather than relying on a global default."
        )
    target = target_date
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
    target_date: date,
    client: httpx.AsyncClient | None = None,
) -> Forecast:
    """Multi-source forecast with priority chain: NWS → Ensemble → Single → Cache.

    When both NWS and Ensemble are available, produces a weighted average:
    - NWS: 50% weight (official resolution source, most aligned with settlement)
    - Ensemble mean: 50% weight (multi-model consensus)
    - Confidence = ensemble std (data-driven uncertainty)
    """
    if target_date is None:
        raise ValueError(
            "target_date is required — pass city_local_date(city) for the "
            "city-local 'today' rather than relying on a global default."
        )
    target = target_date
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=30)

    try:
        # Fetch NWS and Ensemble in parallel
        import asyncio
        nws_task = get_nws_forecast(city, target, client)
        ensemble_task = get_ensemble_forecast(city, target, client)
        nws_fc, ensemble_fc = await asyncio.gather(nws_task, ensemble_task, return_exceptions=True)

        # Handle exceptions (log before discarding)
        if isinstance(nws_fc, Exception):
            logger.warning("NWS forecast error for %s: %s", city.name, nws_fc)
            nws_fc = None
        if isinstance(ensemble_fc, Exception):
            logger.warning("Ensemble forecast error for %s: %s", city.name, ensemble_fc)
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
                # Last resort: cached forecast — FIX-12 enforces a TTL so
                # we don't reuse a multi-day-old forecast when every live
                # source stays broken.
                cached = _get_cached_forecast(city.name)
                if cached:
                    logger.warning(
                        "Forecast %s: Using cached forecast (all APIs failed, "
                        "within %.1fh TTL)", city.name, FORECAST_CACHE_TTL_HOURS,
                    )
                    forecast = cached
                else:
                    raise RuntimeError(f"All forecast sources failed for {city.name}")

        # Update cache with fresh timestamp
        _cache_forecast(city.name, forecast)
        return forecast

    finally:
        if should_close:
            await client.aclose()


async def get_forecasts_batch(
    cities: list[CityConfig],
    target_date: date,
) -> dict[str, Forecast]:
    """Fetch multi-source forecasts for all cities concurrently for a single
    explicit target_date.

    Note: callers that want today/D+1/D+2 across cities in different time
    zones should use ``get_forecasts_for_city_local_window`` instead — this
    helper anchors all cities to one shared date, which only makes sense
    when the caller has already resolved the per-city local calendar.
    """
    if target_date is None:
        raise ValueError(
            "target_date is required — use get_forecasts_for_city_local_window "
            "for the cross-city today/D+1/D+2 sweep instead of relying on a default."
        )
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


# ──────────────────────────────────────────────────────────────────────
# City-local forecast indexing (2026-04-28)
#
# The rebalancer keys ``_cached_forecasts_by_date`` by ``event.market_date``
# which is itself constructed in city-local time over in markets/discovery.
# Anchoring forecast fetches by UTC date opens a 5-8 hour window each day
# where west-coast cities have already rolled their local calendar but
# UTC has not (or vice versa); the lookup
# ``cache[event.market_date][city]`` then misses, the rebalancer falls
# back to a stale by-name forecast whose ``forecast_date`` no longer
# matches the event, and the FIX-22 invariant in
# ``evaluator.evaluate_exit_signals`` raises AssertionError, taking out
# the 15-min position-check (TRIM/EXIT) safety net for the duration of
# the window.  ``city_local_date`` and
# ``get_forecasts_for_city_local_window`` close the race by fetching
# each city's forecast under that city's *own* local date keys.
# ──────────────────────────────────────────────────────────────────────


def city_local_date(city: CityConfig, *, offset_days: int = 0) -> date:
    """Return the city's local calendar date, optionally offset by N days.

    Falls back to UTC with a warning if ``city.tz`` is empty or not a
    valid IANA timezone — the caller still gets *a* date, but the
    operator gets a loud signal that a misconfigured city is silently
    drifting back to the old UTC-anchored behaviour.  Discovery uses
    the same fallback, so a city with broken tz at least stays
    self-consistent (cache key matches market_date).
    """
    if not city.tz:
        logger.warning(
            "city_local_date: city=%s has no tz, falling back to UTC", city.name,
        )
        return (datetime.now(timezone.utc) + timedelta(days=offset_days)).date()
    try:
        tz = ZoneInfo(city.tz)
    except Exception as exc:  # ZoneInfoNotFoundError + ValueError
        logger.warning(
            "city_local_date: city=%s tz=%r invalid (%s), falling back to UTC",
            city.name, city.tz, exc,
        )
        return (datetime.now(timezone.utc) + timedelta(days=offset_days)).date()
    return (datetime.now(tz) + timedelta(days=offset_days)).date()


async def get_forecasts_for_city_local_window(
    cities: list[CityConfig],
    *,
    days: int = 3,
) -> dict[date, dict[str, Forecast]]:
    """Fetch forecasts across each city's *own* local today / D+1 / ... / D+(days-1).

    Returns a ``{date: {city_name: Forecast}}`` cache.  Cities in
    different time zones contribute to different sets of date keys:
    a NYC event with ``market_date=2026-04-25`` finds its forecast
    under ``cache[2026-04-25]["New York"]`` even when the bot's UTC
    clock has already rolled to 2026-04-26 and an LA event for the
    same UTC instant still resolves to ``market_date=2026-04-25``.

    Failures for individual (city, date) pairs are logged and skipped
    so callers can still trade for the cities that succeeded.
    """
    import asyncio

    if not cities or days <= 0:
        return {}

    plan: list[tuple[CityConfig, date]] = []
    for city in cities:
        for i in range(days):
            plan.append((city, city_local_date(city, offset_days=i)))

    async with httpx.AsyncClient(timeout=30) as client:
        tasks = [get_forecast(city, target, client) for city, target in plan]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    out: dict[date, dict[str, Forecast]] = {}
    for (city, target), result in zip(plan, results):
        if isinstance(result, Exception):
            logger.warning(
                "Forecast failed for %s on %s: %s", city.name, target, result,
            )
            continue
        out.setdefault(target, {})[city.name] = result
    return out
