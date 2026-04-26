"""Y4 (15-min refresh, 2026-04-26): refresh_forecasts must REPLACE the
by-name cache for cities-with-positions and EVICT entries for cities
no longer in that set.

Pre-fix the 15-min refresh did `_cached_forecasts.update(today_forecasts)`,
which only touched the cities being refreshed.  A city that left
the active set between the 60-min cycle and the 15-min refresh kept
its stale entry — and the by-date fallback path
`_cached_forecasts.get(city)` would read it.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.config import AppConfig, CityConfig, SchedulingConfig, StrategyConfig
from src.execution.executor import Executor
from src.portfolio.tracker import PortfolioTracker
from src.strategy.rebalancer import Rebalancer
from src.weather.metar import DailyMaxTracker
from src.weather.models import Forecast


def _mk_rebalancer(positions: list[dict]) -> Rebalancer:
    config = AppConfig(
        strategy=StrategyConfig(),
        scheduling=SchedulingConfig(),
        cities=[
            CityConfig("NYC", "KNYC", 40.7, -74.0, tz="America/New_York"),
            CityConfig("LA", "KLAX", 34.0, -118.0, tz="America/Los_Angeles"),
            CityConfig("Chicago", "KORD", 41.97, -87.9, tz="America/Chicago"),
        ],
        dry_run=True,
        db_path=Path("/tmp/test_y4.db"),
    )
    portfolio = MagicMock(spec=PortfolioTracker)
    portfolio.get_all_open_positions = AsyncMock(return_value=positions)
    portfolio.store = MagicMock()
    return Rebalancer(
        config=config, clob=MagicMock(), portfolio=portfolio,
        executor=MagicMock(spec=Executor), max_tracker=DailyMaxTracker(),
    )


def _fc(name: str, d: date, high: float = 70.0) -> Forecast:
    return Forecast(
        city=name, forecast_date=d,
        predicted_high_f=high, predicted_low_f=55.0,
        confidence_interval_f=3.0, source="stub",
        fetched_at=datetime.now(timezone.utc),
    )


def _pos(city: str) -> dict:
    return {"city": city, "token_id": f"t_{city}", "strategy": "B"}


@pytest.mark.asyncio
async def test_refresh_evicts_city_no_longer_in_active_positions(monkeypatch):
    """Y4: between cycles a city dropped out of the open-positions set.
    The 15-min refresh must remove its stale by-name entry."""
    # NYC and Chicago had positions at 60-min boundary; at 15-min only
    # NYC remains.
    reb = _mk_rebalancer(positions=[_pos("NYC")])

    today = date(2026, 4, 26)
    # Pre-seed by-name cache as if 60-min cycle had cached three cities
    reb._cached_forecasts = {
        "NYC": _fc("NYC", today, 70),
        "Chicago": _fc("Chicago", today, 60),  # stale: no longer has position
        "LA": _fc("LA", today, 75),            # stale: also no position
    }

    async def _stub_window(cities, **kw):
        # Refresh only returns NYC's forecast (it's the only city in the call)
        names = [c.name for c in cities]
        return {today: {n: _fc(n, today, 71) for n in names}} if names else {}

    # Block real httpx
    class _FastFail:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, *a, **kw):
            raise httpx.ConnectError("blocked")
    monkeypatch.setattr(httpx, "AsyncClient", _FastFail)

    with patch("src.strategy.rebalancer.get_forecasts_for_city_local_window",
               _stub_window), \
         patch("src.strategy.rebalancer.city_local_date", lambda c, **kw: today):
        await reb.refresh_forecasts()

    # Y4 invariants:
    assert "NYC" in reb._cached_forecasts
    assert reb._cached_forecasts["NYC"].predicted_high_f == 71  # fresher value
    assert "Chicago" not in reb._cached_forecasts, (
        "Y4: Chicago dropped from positions — its by-name entry must be evicted"
    )
    assert "LA" not in reb._cached_forecasts, (
        "Y4: LA dropped from positions — its by-name entry must be evicted"
    )


@pytest.mark.asyncio
async def test_refresh_keeps_city_with_positions_even_if_fetch_failed(monkeypatch):
    """Y4: if Open-Meteo fails for a city WITH positions, leave its
    previous by-name entry alone (better stale-by-15-min than empty)."""
    reb = _mk_rebalancer(positions=[_pos("NYC")])
    today = date(2026, 4, 26)
    reb._cached_forecasts = {"NYC": _fc("NYC", today, 70)}

    # Stub returns empty for NYC (fetch failed)
    async def _stub_window(cities, **kw):
        return {}

    class _FastFail:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, *a, **kw):
            raise httpx.ConnectError("blocked")
    monkeypatch.setattr(httpx, "AsyncClient", _FastFail)

    with patch("src.strategy.rebalancer.get_forecasts_for_city_local_window",
               _stub_window), \
         patch("src.strategy.rebalancer.city_local_date", lambda c, **kw: today):
        await reb.refresh_forecasts()

    # Empty window short-circuits before the eviction logic, so the
    # stale-by-15-min NYC entry is preserved.  This is the desired
    # safety property: an Open-Meteo blip doesn't blow away cached
    # forecasts for cities still under active management.
    assert "NYC" in reb._cached_forecasts


@pytest.mark.asyncio
async def test_refresh_skips_when_no_positions(monkeypatch):
    """Sanity: with no positions, refresh_forecasts is a no-op and the
    by-name cache is left untouched (60-min cycle handles its own clear)."""
    reb = _mk_rebalancer(positions=[])
    today = date(2026, 4, 26)
    reb._cached_forecasts = {"NYC": _fc("NYC", today, 70)}

    await reb.refresh_forecasts()
    # Cache unchanged — 60-min cycle is the cache rebuild authority
    assert "NYC" in reb._cached_forecasts
