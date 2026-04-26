"""Fetch real-time airport weather observations (METAR) from aviationweather.gov."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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


async def get_today_metar_history(
    icao: str,
    tz_name: str,
    client: httpx.AsyncClient | None = None,
) -> list[Observation]:
    """Fetch all METAR observations for today (local date) from aviationweather.gov.

    Uses the `hours` parameter to request up to 24 hours of history, then filters
    to only observations that fall on today's local date.
    """
    params = {"ids": icao, "format": "json", "hours": 24}

    should_close = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.get(METAR_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

        if not data:
            return []

        tz = ZoneInfo(tz_name)
        today_local = datetime.now(tz).date()
        observations: list[Observation] = []

        for entry in data:
            temp_c = entry.get("temp")
            if temp_c is None:
                continue

            obs_time_str = entry.get("reportTime", "")
            try:
                obs_time = datetime.fromisoformat(obs_time_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue

            # Filter: only include observations from today's local date
            if obs_time.astimezone(tz).date() != today_local:
                continue

            observations.append(Observation(
                icao=icao,
                temp_f=_celsius_to_fahrenheit(float(temp_c)),
                observation_time=obs_time,
                raw_metar=entry.get("rawOb", ""),
            ))

        # API returns newest first — reverse to chronological order
        observations.sort(key=lambda o: o.observation_time)
        logger.info("METAR history for %s: %d observations for %s", icao, len(observations), today_local)
        return observations
    except Exception:
        logger.exception("Failed to fetch METAR history for %s", icao)
        return []
    finally:
        if should_close:
            await client.aclose()


class DailyMaxTracker:
    """Track the running daily maximum temperature per station.

    Dates are keyed by each station's **local** date (not UTC) to avoid
    cross-day contamination.  E.g. a KLAX observation at 00:23 UTC on
    April 12 is actually 5:23 PM PDT on April 11 and must be grouped
    under April 11.
    """

    def __init__(self) -> None:
        # {(icao, date_str): max_temp_f}
        self._maxes: dict[tuple[str, str], float] = defaultdict(lambda: -999.0)
        # {(icao, date_str): [(iso_timestamp, temp_f), ...]}
        self._observations: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
        # ICAO → local timezone (registered via register_timezone)
        self._tz_map: dict[str, ZoneInfo] = {}

    def register_timezone(self, icao: str, tz_name: str) -> None:
        """Register the local timezone for a station."""
        self._tz_map[icao] = ZoneInfo(tz_name)

    def _local_date_str(self, icao: str, utc_dt: datetime) -> str:
        """Convert a UTC datetime to the station's local date string."""
        tz = self._tz_map.get(icao)
        if tz and utc_dt.tzinfo is not None:
            return utc_dt.astimezone(tz).date().isoformat()
        # Fallback: use the datetime's own date (UTC or naive)
        return utc_dt.date().isoformat()

    def _local_today(self, icao: str) -> str:
        """Get today's date string in the station's local timezone.

        FIX-2P-10: when no tz mapping is registered for the ICAO, fall
        back to UTC instead of server-local (`date.today()`).  Container
        runs UTC by design, but a dev box in a non-UTC tz would
        otherwise silently key observations under the wrong date.
        """
        tz = self._tz_map.get(icao)
        if tz:
            return datetime.now(tz).date().isoformat()
        return datetime.now(timezone.utc).date().isoformat()

    def update(self, obs: Observation) -> tuple[float, bool]:
        """Update with an observation and return (current_daily_max, is_new_high).

        is_new_high is True when this observation set a new daily maximum.
        """
        key = (obs.icao, self._local_date_str(obs.icao, obs.observation_time))
        is_new_high = obs.temp_f > self._maxes[key]
        if is_new_high:
            self._maxes[key] = obs.temp_f

        # Record individual observation for time-series (deduplicate by timestamp)
        ts = obs.observation_time.isoformat()
        series = self._observations[key]
        if not series or series[-1][0] != ts:
            series.append((ts, obs.temp_f))

        return self._maxes[key], is_new_high

    def get_max(self, icao: str, *, day: date) -> float | None:
        """Get the current daily max. Returns None if no data."""
        val = self._maxes.get((icao, day.isoformat()))
        return val if val != -999.0 else None

    def get_observations(self, icao: str, *, day: date) -> list[tuple[str, float]]:
        """Get the observation time series for a station and date.

        Returns a list of (iso_timestamp, temp_f) tuples, or empty list if no data.
        """
        return list(self._observations.get((icao, day.isoformat()), []))

    def cleanup_old(self, keep_date: date | None = None) -> None:
        """Remove entries older than keep_date (with 1-day buffer for timezone safety).

        FIX-2P-10: keep_date defaults to UTC today (was server-local
        date.today()).  All timestamps in this tracker are derived from
        UTC observation times — anchoring the cleanup cursor in UTC
        keeps the buffer accounting correct.
        """
        # Subtract 1 day to avoid cleaning up entries for cities whose local
        # date is behind UTC (e.g. Pacific = UTC-7)
        keep = ((keep_date or datetime.now(timezone.utc).date()) - timedelta(days=1)).isoformat()
        to_remove = [k for k in self._maxes if k[1] < keep]
        for k in to_remove:
            del self._maxes[k]
        to_remove_obs = [k for k in self._observations if k[1] < keep]
        for k in to_remove_obs:
            del self._observations[k]
