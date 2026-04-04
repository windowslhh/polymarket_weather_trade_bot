"""Historical weather data and forecast error distribution analysis.

Uses Open-Meteo Archive API to build empirical forecast error distributions
per city, replacing the naive normal distribution assumption.
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from src.config import CityConfig

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_ARCHIVE_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"

# Default cache directory for historical data
_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "history"


class ForecastErrorDistribution:
    """Empirical forecast error distribution for a city.

    Built from historical (forecast_high - actual_high) pairs.
    Replaces the naive normal distribution with real-world data.
    """

    def __init__(self, city: str, errors: list[float]) -> None:
        self.city = city
        self._errors = sorted(errors)  # forecast - actual (positive = forecast was too high)
        self._count = len(errors)

        if self._count > 0:
            self.mean = sum(errors) / self._count
            self.std = math.sqrt(sum((e - self.mean) ** 2 for e in errors) / self._count)
            # Percentiles for quick lookup
            self._percentiles = self._compute_percentiles()
        else:
            self.mean = 0.0
            self.std = 4.0  # fallback
            self._percentiles = {}

    def _compute_percentiles(self) -> dict[int, float]:
        """Compute percentile values from the error distribution."""
        percentiles = {}
        for p in range(1, 100):
            idx = int(self._count * p / 100)
            idx = min(idx, self._count - 1)
            percentiles[p] = self._errors[idx]
        return percentiles

    def prob_actual_below(self, threshold_f: float, forecast_high_f: float) -> float:
        """P(actual_high < threshold) given the forecast high.

        Uses the empirical error distribution instead of normal assumption.
        error = forecast - actual → actual = forecast - error
        P(actual < threshold) = P(forecast - error < threshold)
                               = P(error > forecast - threshold)
        """
        cutoff = forecast_high_f - threshold_f
        # Count what fraction of historical errors exceed this cutoff
        count_above = sum(1 for e in self._errors if e > cutoff)
        return count_above / self._count if self._count > 0 else 0.5

    def prob_actual_above(self, threshold_f: float, forecast_high_f: float) -> float:
        """P(actual_high >= threshold) given the forecast high."""
        return 1.0 - self.prob_actual_below(threshold_f, forecast_high_f)

    def prob_actual_in_range(
        self, lower_f: float, upper_f: float, forecast_high_f: float
    ) -> float:
        """P(lower <= actual_high <= upper) given the forecast high.

        This is the key function: probability that the actual temperature
        lands in a specific slot's range.
        """
        p_below_upper = self.prob_actual_below(upper_f + 0.5, forecast_high_f)  # inclusive
        p_below_lower = self.prob_actual_below(lower_f - 0.5, forecast_high_f)  # exclusive
        return max(0.0, p_below_upper - p_below_lower)

    def prob_no_wins(
        self, slot_lower_f: float | None, slot_upper_f: float | None, forecast_high_f: float
    ) -> float:
        """Probability that NO wins for a slot = P(actual NOT in slot range).

        This replaces the old _estimate_no_win_probability function.
        """
        if slot_lower_f is not None and slot_upper_f is not None:
            p_in = self.prob_actual_in_range(slot_lower_f, slot_upper_f, forecast_high_f)
        elif slot_lower_f is not None:
            # "X°F or above" — NO wins if actual < X
            p_in = self.prob_actual_above(slot_lower_f, forecast_high_f)
        elif slot_upper_f is not None:
            # "Below X°F" — NO wins if actual >= X
            p_in = self.prob_actual_below(slot_upper_f, forecast_high_f)
        else:
            return 0.5

        return min(max(1.0 - p_in, 0.01), 0.99)

    def summary(self) -> dict:
        return {
            "city": self.city,
            "samples": self._count,
            "mean_error": round(self.mean, 2),
            "std_error": round(self.std, 2),
            "p5": round(self._percentiles.get(5, 0), 1),
            "p25": round(self._percentiles.get(25, 0), 1),
            "p50": round(self._percentiles.get(50, 0), 1),
            "p75": round(self._percentiles.get(75, 0), 1),
            "p95": round(self._percentiles.get(95, 0), 1),
        }


async def fetch_historical_actuals(
    city: CityConfig,
    start_date: date,
    end_date: date,
    client: httpx.AsyncClient | None = None,
) -> list[tuple[date, float]]:
    """Fetch historical actual daily high temperatures from Open-Meteo Archive.

    Returns list of (date, actual_high_f) pairs.
    """
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        resp = await client.get(ARCHIVE_URL, params={
            "latitude": city.lat,
            "longitude": city.lon,
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        })
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})
        times = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])

        results = []
        for t, h in zip(times, highs):
            if h is not None:
                results.append((date.fromisoformat(t), float(h)))
        return results
    except Exception:
        logger.exception("Failed to fetch historical actuals for %s", city.name)
        return []
    finally:
        if should_close:
            await client.aclose()


async def fetch_historical_forecasts(
    city: CityConfig,
    start_date: date,
    end_date: date,
    client: httpx.AsyncClient | None = None,
) -> list[tuple[date, float]]:
    """Fetch historical day-ahead forecast highs from Open-Meteo Previous Runs API.

    Returns list of (target_date, forecasted_high_f) pairs.
    The API returns forecasts that were made 1 day before the target date.
    """
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        resp = await client.get(FORECAST_ARCHIVE_URL, params={
            "latitude": city.lat,
            "longitude": city.lon,
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "past_days": 0,
        })
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})
        times = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])

        results = []
        for t, h in zip(times, highs):
            if h is not None:
                results.append((date.fromisoformat(t), float(h)))
        return results
    except Exception:
        logger.exception("Failed to fetch historical forecasts for %s", city.name)
        return []
    finally:
        if should_close:
            await client.aclose()


async def build_error_distribution(
    city: CityConfig,
    lookback_days: int = 730,
    client: httpx.AsyncClient | None = None,
    cache_dir: Path | None = None,
) -> ForecastErrorDistribution:
    """Build a forecast error distribution for a city from historical data.

    Fetches both historical forecasts and actuals, computes errors,
    and returns an empirical distribution.
    """
    cache_dir = cache_dir or _CACHE_DIR
    cache_file = cache_dir / f"{city.icao}_errors.json"

    # Check cache (refresh if older than 7 days)
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            cached_date = date.fromisoformat(cached["built_date"])
            if (date.today() - cached_date).days < 7:
                logger.info("Using cached error distribution for %s (%d samples)",
                           city.name, len(cached["errors"]))
                return ForecastErrorDistribution(city.name, cached["errors"])
        except (json.JSONDecodeError, KeyError):
            pass

    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=lookback_days)

    should_close = client is None
    client = client or httpx.AsyncClient(timeout=60)
    try:
        actuals = await fetch_historical_actuals(city, start, end, client)
        forecasts = await fetch_historical_forecasts(city, start, end, client)

        # Build date -> value maps
        actual_map = {d: h for d, h in actuals}
        forecast_map = {d: h for d, h in forecasts}

        # Compute errors for dates where we have both
        errors: list[float] = []
        for d in actual_map:
            if d in forecast_map:
                # error = forecast - actual (positive = forecast was too high)
                errors.append(forecast_map[d] - actual_map[d])

        if not errors:
            logger.warning("No paired forecast/actual data for %s, using actuals only", city.name)
            # Fallback: compute day-to-day variability as proxy
            sorted_actuals = sorted(actuals, key=lambda x: x[0])
            for i in range(1, len(sorted_actuals)):
                errors.append(sorted_actuals[i][1] - sorted_actuals[i - 1][1])

        # Cache results
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({
            "city": city.name,
            "icao": city.icao,
            "built_date": date.today().isoformat(),
            "lookback_days": lookback_days,
            "sample_count": len(errors),
            "errors": errors,
        }))

        dist = ForecastErrorDistribution(city.name, errors)
        logger.info(
            "Built error distribution for %s: %d samples, mean=%.2f°F, std=%.2f°F",
            city.name, len(errors), dist.mean, dist.std,
        )
        return dist
    finally:
        if should_close:
            await client.aclose()


async def build_all_distributions(
    cities: list[CityConfig],
    lookback_days: int = 730,
) -> dict[str, ForecastErrorDistribution]:
    """Build forecast error distributions for all cities."""
    import asyncio

    async with httpx.AsyncClient(timeout=60) as client:
        tasks = [build_error_distribution(city, lookback_days, client) for city in cities]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    distributions: dict[str, ForecastErrorDistribution] = {}
    for city, result in zip(cities, results):
        if isinstance(result, Exception):
            logger.error("Failed to build distribution for %s: %s", city.name, result)
            # Fallback: empty distribution (will use defaults)
            distributions[city.name] = ForecastErrorDistribution(city.name, [])
        else:
            distributions[city.name] = result
    return distributions
