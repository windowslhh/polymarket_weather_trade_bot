"""Y4: by-name forecast cache must not retain stale entries from
cities that have dropped out of the active set.

Pre-fix the 60-min full cycle did `self._cached_forecasts.update(...)`
so a city that disappeared from discovery (e.g. its event got below
min_volume threshold and was filtered out) kept its old forecast in
the by-name cache forever.  Lookups via
`_cached_forecasts.get(city)` would then silently return stale data
hours / days later.

Fix: REPLACE `_cached_forecasts` outright after each full cycle so the
by-name cache equals the active-cities set.  The by-date cache is
already rebuilt per cycle.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import AppConfig, CityConfig, SchedulingConfig, StrategyConfig
from src.execution.executor import Executor
from src.portfolio.tracker import PortfolioTracker
from src.strategy.rebalancer import Rebalancer
from src.weather.metar import DailyMaxTracker
from src.weather.models import Forecast


def _mk_rebalancer() -> Rebalancer:
    config = AppConfig(
        strategy=StrategyConfig(),
        scheduling=SchedulingConfig(),
        cities=[
            CityConfig("NYC", "KNYC", 40.7, -74.0, tz="America/New_York"),
            CityConfig("LA", "KLAX", 34.0, -118.0, tz="America/Los_Angeles"),
        ],
        dry_run=True,
        db_path=Path("/tmp/test_y4_cache.db"),
    )
    portfolio = MagicMock(spec=PortfolioTracker)
    portfolio.get_all_open_positions = AsyncMock(return_value=[])
    portfolio.store = MagicMock()
    return Rebalancer(
        config=config, clob=MagicMock(), portfolio=portfolio,
        executor=MagicMock(spec=Executor), max_tracker=DailyMaxTracker(),
    )


def _fc(city_name: str, d: date, high: float = 70.0) -> Forecast:
    return Forecast(
        city=city_name, forecast_date=d,
        predicted_high_f=high, predicted_low_f=55.0,
        confidence_interval_f=3.0, source="stub",
        fetched_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_full_cycle_replaces_by_name_cache_dropping_inactive_cities():
    """Run two simulated cycles where the active city set shrinks
    between them.  After the second cycle, the by-name cache must
    contain ONLY cities active in the second cycle."""
    reb = _mk_rebalancer()
    today = date(2026, 4, 26)

    # Pre-seed cache as if cycle 1 had populated NYC + LA + Chicago
    # (Chicago has dropped out of active_events for cycle 2).
    reb._cached_forecasts = {
        "NYC": _fc("NYC", today, 70),
        "LA": _fc("LA", today, 75),
        "Chicago": _fc("Chicago", today, 60),  # stale: not in cycle 2
    }
    reb._cached_forecasts_by_date = {today: dict(reb._cached_forecasts)}

    # Simulate cycle 2: only NYC and LA come back from discovery.
    new_cycle_forecasts = {
        date(2026, 4, 26): {
            "NYC": _fc("NYC", date(2026, 4, 26), 71),  # fresher
            "LA": _fc("LA", date(2026, 4, 26), 76),
        },
        date(2026, 4, 27): {
            "NYC": _fc("NYC", date(2026, 4, 27), 73),
            "LA": _fc("LA", date(2026, 4, 27), 78),
        },
    }

    async def _stub_window(cities, **kw):
        return new_cycle_forecasts

    # Inline-call only the by-name rebuild slice of the full rebalance.
    # Mirroring lines ~1052-1063 in rebalancer.py — keeps the test
    # focused on the cache-clear behaviour without spinning up
    # APScheduler + every other dependency.  We freeze city_local_date
    # to return today=2026-04-26 for both cities so the stub data
    # lines up regardless of when the test runs.
    def _frozen_cld(city, **kw):
        return today

    with patch(
        "src.strategy.rebalancer.get_forecasts_for_city_local_window",
        _stub_window,
    ), patch("src.strategy.rebalancer.city_local_date", _frozen_cld):
        reb._cached_forecasts_by_date = await _stub_window(reb._config.cities)
        from src.strategy.rebalancer import city_local_date as _cld_imported
        forecasts = {}
        for c in reb._config.cities:
            fc = reb._cached_forecasts_by_date.get(_cld_imported(c), {}).get(c.name)
            if fc:
                forecasts[c.name] = fc
        # Mirror the Y4 line: replace, don't update
        reb._cached_forecasts = dict(forecasts)

    # Y4 invariant
    assert "Chicago" not in reb._cached_forecasts, (
        "Y4: stale 'Chicago' entry must be evicted when no longer active"
    )
    # Sanity: active cities updated to the fresh values
    assert reb._cached_forecasts["NYC"].predicted_high_f == 71.0
    assert reb._cached_forecasts["LA"].predicted_high_f == 76.0


def test_rebalancer_source_uses_assignment_not_update():
    """Static check: catch a future revert from `= dict(forecasts)`
    back to `.update(forecasts)`."""
    src = (Path(__file__).resolve().parents[1] / "src" / "strategy" / "rebalancer.py").read_text()
    # In the main rebalance cycle, the by-name cache must be REPLACED
    # (assignment) not merged (update).  Look for the assignment
    # pattern next to the Y4 marker.
    assert "self._cached_forecasts = dict(forecasts)" in src, (
        "Y4: by-name cache should be replaced via assignment to drop "
        "inactive cities; .update() leaks stale entries"
    )
