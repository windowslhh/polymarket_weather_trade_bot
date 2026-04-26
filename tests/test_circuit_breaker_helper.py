"""C-6: shared `_check_circuit_breaker` helper.

Pre-fix the daily-loss circuit-breaker check was inlined in two
places (full rebalance + position-check) with subtly different
exception handling and log strings.  C-6 centralises the trip
CONDITION; call sites still decide what to DO on a trip.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import AppConfig, CityConfig, SchedulingConfig, StrategyConfig
from src.execution.executor import Executor
from src.portfolio.tracker import PortfolioTracker
from src.strategy.rebalancer import Rebalancer
from src.weather.metar import DailyMaxTracker


def _mk_rebalancer(daily_loss_limit: float = 75.0) -> Rebalancer:
    config = AppConfig(
        strategy=StrategyConfig(daily_loss_limit_usd=daily_loss_limit),
        scheduling=SchedulingConfig(),
        cities=[CityConfig("NYC", "KNYC", 40.7, -74.0, tz="America/New_York")],
        dry_run=True,
        db_path=Path("/tmp/test_cb_helper.db"),
    )
    portfolio = MagicMock(spec=PortfolioTracker)
    portfolio.get_all_open_positions = AsyncMock(return_value=[])
    portfolio.store = MagicMock()
    return Rebalancer(
        config=config, clob=MagicMock(), portfolio=portfolio,
        executor=MagicMock(spec=Executor), max_tracker=DailyMaxTracker(),
    )


@pytest.mark.asyncio
async def test_helper_returns_false_below_loss_limit():
    reb = _mk_rebalancer(daily_loss_limit=75.0)
    reb._portfolio.get_daily_pnl = AsyncMock(return_value=-10.0)  # well above limit
    tripped, pnl = await reb._check_circuit_breaker()
    assert tripped is False
    assert pnl == -10.0


@pytest.mark.asyncio
async def test_helper_trips_below_negative_limit():
    """daily_pnl < -limit → tripped=True."""
    reb = _mk_rebalancer(daily_loss_limit=75.0)
    reb._portfolio.get_daily_pnl = AsyncMock(return_value=-100.0)
    tripped, pnl = await reb._check_circuit_breaker()
    assert tripped is True
    assert pnl == -100.0


@pytest.mark.asyncio
async def test_helper_does_not_trip_at_exact_limit():
    """daily_pnl == -limit → still NOT tripped (strict less-than)."""
    reb = _mk_rebalancer(daily_loss_limit=75.0)
    reb._portfolio.get_daily_pnl = AsyncMock(return_value=-75.0)
    tripped, _pnl = await reb._check_circuit_breaker()
    assert tripped is False


@pytest.mark.asyncio
async def test_helper_treats_none_pnl_as_not_tripped():
    """No PnL row yet (fresh day) → not tripped."""
    reb = _mk_rebalancer(daily_loss_limit=75.0)
    reb._portfolio.get_daily_pnl = AsyncMock(return_value=None)
    tripped, pnl = await reb._check_circuit_breaker()
    assert tripped is False
    assert pnl is None


@pytest.mark.asyncio
async def test_helper_swallows_db_exception_returning_not_tripped():
    """A read-failure on get_daily_pnl must NOT halt the bot — return
    (False, None) so the caller keeps going.  Pre-fix the position-check
    site caught + continued, the full-rebalance site let the exception
    propagate; now both share the swallowing semantic."""
    reb = _mk_rebalancer(daily_loss_limit=75.0)
    reb._portfolio.get_daily_pnl = AsyncMock(
        side_effect=RuntimeError("db lock"),
    )
    tripped, pnl = await reb._check_circuit_breaker()
    assert tripped is False
    assert pnl is None
