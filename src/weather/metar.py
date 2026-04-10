"""Fetch real-time airport weather observations (METAR) from aviationweather.gov."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timezone

import httpx

from src.weather.models import Observation

logger = logging.getLogger(__name__)

METAR_URL = "https://aviationweather.gov/api/data/metar"


def _celsius_to_fahrenheit(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


async def get_latest_metar(
    icao: str,
    client: httpx.AsyncClient | None = None,
) -> Observation | None:
    """Fetch the most recent METAR observation for an airport station.

    Returns None if no data is available.
    """
    params = {"ids": icao, "format": "json"}

    should_close = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.get(METAR_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

        if not data:
            logger.warning("No METAR data for %s", icao)
            return None

        latest = data[0]
        temp_c = latest.get("temp")
        if temp_c is None:
            logger.warning("No temperature in METAR for %s", icao)
            return None

        obs_time_str = latest.get("reportTime", "")
        try:
            obs_time = datetime.fromisoformat(obs_time_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            obs_time = datetime.now(timezone.utc)

        return Observation(
            icao=icao,
            temp_f=_celsius_to_fahrenheit(float(temp_c)),
            observation_time=obs_time,
            raw_metar=latest.get("rawOb", ""),
        )
    except Exception:
        logger.exception("Failed to fetch METAR for %s", icao)
        return None
    finally:
        if should_close:
            await client.aclose()


class DailyMaxTracker:
    """Track the running daily maximum temperature per station."""

    def __init__(self) -> None:
        # {(icao, date_str): max_temp_f}
        self._maxes: dict[tuple[str, str], float] = defaultdict(lambda: -999.0)

    def update(self, obs: Observation) -> tuple[float, bool]:
        """Update with an observation and return (current_daily_max, is_new_high).

        is_new_high is True when this observation set a new daily maximum.
        """
        key = (obs.icao, obs.observation_time.date().isoformat())
        is_new_high = obs.temp_f > self._maxes[key]
        if is_new_high:
            self._maxes[key] = obs.temp_f
        return self._maxes[key], is_new_high

    def get_max(self, icao: str, day: date | None = None) -> float | None:
        """Get the current daily max. Returns None if no data."""
        d = (day or date.today()).isoformat()
        val = self._maxes.get((icao, d))
        return val if val != -999.0 else None

    def cleanup_old(self, keep_date: date | None = None) -> None:
        """Remove entries older than keep_date."""
        keep = (keep_date or date.today()).isoformat()
        to_remove = [k for k in self._maxes if k[1] < keep]
        for k in to_remove:
            del self._maxes[k]
