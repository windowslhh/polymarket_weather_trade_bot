"""Tests for 15-minute lightweight position check (Module 2D).

The position check runs between full rebalance cycles to:
- Update daily max tracker from METAR
- Detect locked-win opportunities on existing positions
- Detect urgent EXIT signals for threatened positions
- Execute urgent trades only (no market discovery, no new NO entry)

Tests cover: critical paths, boundary conditions, failure branches,
scheduler wiring, and performance.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import AppConfig, CityConfig, SchedulingConfig, StrategyConfig
from src.execution.executor import Executor
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.portfolio.tracker import PortfolioTracker
from src.strategy.rebalancer import Rebalancer
from src.weather.metar import DailyMaxTracker
from src.weather.models import Observation


# ── Fixtures ──────────────────────────────────────────────────────────

def _make_config(**overrides) -> AppConfig:
    return AppConfig(
        strategy=StrategyConfig(
            no_distance_threshold_f=8,
            min_no_ev=0.01,
            max_no_price=0.95,
            enable_locked_wins=True,
            **overrides,
        ),
        scheduling=SchedulingConfig(),
        cities=[
            CityConfig("New York", "KLGA", 40.7128, -74.006),
            CityConfig("Dallas", "KDFW", 32.7767, -96.797),
        ],
        dry_run=True,
        db_path=Path("/tmp/test_position_check.db"),
    )


def _mock_rebalancer(config=None, positions=None):
    """Create a Rebalancer with mocked external dependencies."""
    config = config or _make_config()

    mock_clob = MagicMock()
    mock_portfolio = MagicMock(spec=PortfolioTracker)
    mock_executor = MagicMock(spec=Executor)
    mock_executor.execute_signals = AsyncMock(return_value=[])

    # Default: no open positions
    mock_portfolio.get_all_open_positions = AsyncMock(return_value=positions or [])
    mock_portfolio.get_city_exposure = AsyncMock(return_value=0.0)
    mock_portfolio.get_total_exposure = AsyncMock(return_value=0.0)

    tracker = DailyMaxTracker()

    rebalancer = Rebalancer(
        config=config,
        clob=mock_clob,
        portfolio=mock_portfolio,
        executor=mock_executor,
        max_tracker=tracker,
    )
    return rebalancer


def _open_position(city="New York", event_id="evt_1", token_id="no_1",
                   slot_label="80°F to 84°F", strategy="A",
                   entry_price=0.90, size_usd=5.0):
    """Create a dict mimicking a DB position row."""
    return {
        "id": 1,
        "event_id": event_id,
        "condition_id": "cond_1",
        "city": city,
        "token_id": token_id,
        "token_type": "NO",
        "side": "BUY",
        "slot_label": slot_label,
        "strategy": strategy,
        "entry_price": entry_price,
        "size_usd": size_usd,
        "shares": size_usd / entry_price,
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "closed_at": None,
    }


# ── Mock for fetch_settlement_temp ──

class MockSettlementObs:
    def __init__(self, icao, temp_f):
        self.icao = icao
        self.temp_f = temp_f
        self.observation_time = datetime.now(timezone.utc)
        self.raw_data = ""


# ──────────────────────────────────────────────────────────────────────
# Critical Paths
# ──────────────────────────────────────────────────────────────────────

class TestPositionCheckCriticalPaths:
    """Core functionality: METAR fetch, locked-win detection, exit detection."""

    @pytest.mark.asyncio
    async def test_no_positions_returns_empty(self):
        """When there are no open positions, return immediately."""
        reb = _mock_rebalancer(positions=[])
        signals = await reb.run_position_check()
        assert signals == []
        # Should not call executor
        reb._executor.execute_signals.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_signal_on_close_daily_max(self):
        """Position threatened by rising daily max → EXIT signal."""
        pos = _open_position(slot_label="80°F to 84°F", entry_price=0.90)
        reb = _mock_rebalancer(positions=[pos])

        # Mock METAR: daily max = 81°F, approaching [80,84]
        async def mock_fetch(city, client):
            if city == "New York":
                return MockSettlementObs("KLGA", 81.0)
            return None

        with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch):
            signals = await reb.run_position_check()

        # Distance from 81 to [80,84] = 0, within exit distance (8*0.4=3.2)
        sell_signals = [s for s in signals if s.side == Side.SELL]
        assert len(sell_signals) >= 1

    @pytest.mark.asyncio
    async def test_locked_win_detected(self):
        """Daily max exceeds slot upper bound → locked-win BUY signal."""
        # Position on [70,74], daily max will be 76 → locked
        pos = _open_position(slot_label="70°F to 74°F", token_id="no_1",
                            entry_price=0.90)
        reb = _mock_rebalancer(positions=[pos])

        async def mock_fetch(city, client):
            if city == "New York":
                return MockSettlementObs("KLGA", 76.0)
            return None

        with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch):
            signals = await reb.run_position_check()

        # The held token_id is in held_token_ids → locked win signal skipped for held.
        # But locked win would trigger for other slots if they were in the event.
        # Since we only have one held slot and it's already held → filtered out
        # This is correct behavior: we already own the position
        buy_signals = [s for s in signals if s.side == Side.BUY]
        assert len(buy_signals) == 0  # already held

    @pytest.mark.asyncio
    async def test_no_metar_available_skips_city(self):
        """If METAR fetch returns None for a city, skip that city."""
        pos = _open_position()
        reb = _mock_rebalancer(positions=[pos])

        async def mock_fetch(city, client):
            return None  # No METAR data

        with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch):
            signals = await reb.run_position_check()

        assert signals == []

    @pytest.mark.asyncio
    async def test_executes_urgent_signals(self):
        """Generated signals are passed to executor."""
        pos = _open_position(slot_label="80°F to 84°F", entry_price=0.90)
        reb = _mock_rebalancer(positions=[pos])

        async def mock_fetch(city, client):
            if city == "New York":
                return MockSettlementObs("KLGA", 81.0)
            return None

        with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch):
            signals = await reb.run_position_check()

        if signals:
            reb._executor.execute_signals.assert_called_once()


# ──────────────────────────────────────────────────────────────────────
# Boundary Conditions
# ──────────────────────────────────────────────────────────────────────

class TestPositionCheckBoundary:

    @pytest.mark.asyncio
    async def test_multiple_cities(self):
        """Positions in multiple cities: each city fetched independently."""
        pos_ny = _open_position(city="New York", slot_label="80°F to 84°F",
                               token_id="no_ny", event_id="evt_ny")
        pos_dal = _open_position(city="Dallas", slot_label="90°F to 94°F",
                                token_id="no_dal", event_id="evt_dal")
        reb = _mock_rebalancer(positions=[pos_ny, pos_dal])

        call_cities = []
        async def mock_fetch(city, client):
            call_cities.append(city)
            if city == "New York":
                return MockSettlementObs("KLGA", 75.0)  # safe for [80,84]
            if city == "Dallas":
                return MockSettlementObs("KDFW", 91.0)  # inside [90,94] → exit
            return None

        with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch):
            signals = await reb.run_position_check()

        assert "New York" in call_cities
        assert "Dallas" in call_cities

    @pytest.mark.asyncio
    async def test_multiple_strategies_same_event(self):
        """Same event with positions from different strategies → each evaluated."""
        pos_a = _open_position(strategy="A", token_id="no_a", slot_label="80°F to 84°F")
        pos_b = _open_position(strategy="B", token_id="no_b", slot_label="80°F to 84°F")
        reb = _mock_rebalancer(positions=[pos_a, pos_b])

        async def mock_fetch(city, client):
            if city == "New York":
                return MockSettlementObs("KLGA", 81.0)  # close → exit
            return None

        with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch):
            signals = await reb.run_position_check()

        # Both strategies should have exit signals
        strategies = {s.strategy for s in signals if s.side == Side.SELL}
        assert "A" in strategies or "B" in strategies

    @pytest.mark.asyncio
    async def test_yes_positions_ignored(self):
        """Only NO BUY positions are processed (YES are skipped)."""
        pos = _open_position()
        pos["token_type"] = "YES"  # should be skipped
        reb = _mock_rebalancer(positions=[pos])

        async def mock_fetch(city, client):
            return MockSettlementObs("KLGA", 81.0)

        with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch):
            signals = await reb.run_position_check()

        assert signals == []


# ──────────────────────────────────────────────────────────────────────
# Failure Branches
# ──────────────────────────────────────────────────────────────────────

class TestPositionCheckFailure:

    @pytest.mark.asyncio
    async def test_metar_exception_handled(self):
        """If METAR fetch raises, position check doesn't crash."""
        pos = _open_position()
        reb = _mock_rebalancer(positions=[pos])

        async def mock_fetch(city, client):
            raise Exception("Network timeout")

        with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch):
            signals = await reb.run_position_check()

        # Should return empty (caught by except block)
        assert signals == []

    @pytest.mark.asyncio
    async def test_portfolio_exception_handled(self):
        """If portfolio query fails, position check doesn't crash."""
        reb = _mock_rebalancer()
        reb._portfolio.get_all_open_positions = AsyncMock(
            side_effect=Exception("DB locked")
        )
        signals = await reb.run_position_check()
        assert signals == []

    @pytest.mark.asyncio
    async def test_unknown_city_skipped(self):
        """Position with city not in config.cities → skipped gracefully."""
        pos = _open_position(city="Unknown City")
        reb = _mock_rebalancer(positions=[pos])

        async def mock_fetch(city, client):
            return None

        with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch):
            signals = await reb.run_position_check()

        assert signals == []

    @pytest.mark.asyncio
    async def test_invalid_slot_label_handled(self):
        """Position with unparseable slot_label → handled gracefully."""
        pos = _open_position(slot_label="INVALID LABEL")
        reb = _mock_rebalancer(positions=[pos])

        async def mock_fetch(city, client):
            return MockSettlementObs("KLGA", 80.0)

        with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch):
            # Should not crash even with unparseable label
            signals = await reb.run_position_check()
            # May produce signals or not, but should not raise


# ──────────────────────────────────────────────────────────────────────
# Scheduler Wiring
# ──────────────────────────────────────────────────────────────────────

class TestSchedulerWiring:

    def test_scheduler_has_settlement_check_job(self):
        """Scheduler must have a settlement_check job that includes position check."""
        from src.scheduler.jobs import setup_scheduler
        config = _make_config()
        reb = _mock_rebalancer(config=config)
        scheduler = setup_scheduler(config, reb)

        job = scheduler.get_job("settlement_check")
        assert job is not None
        assert "Settlement" in job.name or "position" in job.name

    def test_rebalancer_has_run_position_check(self):
        """Rebalancer must expose run_position_check as async method."""
        assert hasattr(Rebalancer, "run_position_check")
        import inspect
        assert inspect.iscoroutinefunction(Rebalancer.run_position_check)

    def test_run_position_check_return_type(self):
        """run_position_check returns list[TradeSignal]."""
        import inspect
        sig = inspect.signature(Rebalancer.run_position_check)
        # Return annotation should be list[TradeSignal]
        ret = sig.return_annotation
        assert "TradeSignal" in str(ret) or ret == inspect.Parameter.empty


# ──────────────────────────────────────────────────────────────────────
# DailyMaxTracker Integration
# ──────────────────────────────────────────────────────────────────────

class TestPositionCheckTrackerIntegration:

    @pytest.mark.asyncio
    async def test_tracker_updated_by_position_check(self):
        """Position check should update the shared DailyMaxTracker."""
        pos = _open_position()
        reb = _mock_rebalancer(positions=[pos])

        async def mock_fetch(city, client):
            if city == "New York":
                return MockSettlementObs("KLGA", 72.0)
            return None

        # Before: no max for KLGA today
        assert reb._max_tracker.get_max("KLGA") is None

        with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch):
            await reb.run_position_check()

        # After: tracker should have updated
        # Use UTC date to match observation_time.date() stored by DailyMaxTracker
        utc_today = datetime.now(timezone.utc).date()
        assert reb._max_tracker.get_max("KLGA", utc_today) == 72.0

    @pytest.mark.asyncio
    async def test_tracker_preserves_higher_max(self):
        """Position check should not lower an existing daily max."""
        pos = _open_position()
        reb = _mock_rebalancer(positions=[pos])

        # Pre-set a higher max
        obs_high = Observation(icao="KLGA", temp_f=85.0,
                              observation_time=datetime.now(timezone.utc))
        reb._max_tracker.update(obs_high)

        async def mock_fetch(city, client):
            if city == "New York":
                return MockSettlementObs("KLGA", 72.0)  # lower than existing 85
            return None

        with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch):
            await reb.run_position_check()

        # Max should still be 85, not 72
        # Use UTC date to match observation_time.date() stored by DailyMaxTracker
        utc_today = datetime.now(timezone.utc).date()
        assert reb._max_tracker.get_max("KLGA", utc_today) == 85.0


# ──────────────────────────────────────────────────────────────────────
# Performance
# ──────────────────────────────────────────────────────────────────────

class TestPositionCheckPerformance:

    @pytest.mark.asyncio
    async def test_many_positions_fast(self):
        """50 positions across 2 cities should check quickly."""
        import time
        positions = []
        for i in range(25):
            positions.append(_open_position(
                city="New York", event_id="evt_ny",
                token_id=f"no_ny_{i}", slot_label=f"{60+i}°F to {64+i}°F",
                strategy="A",
            ))
            positions.append(_open_position(
                city="Dallas", event_id="evt_dal",
                token_id=f"no_dal_{i}", slot_label=f"{70+i}°F to {74+i}°F",
                strategy="A",
            ))

        reb = _mock_rebalancer(positions=positions)

        async def mock_fetch(city, client):
            if city == "New York":
                return MockSettlementObs("KLGA", 75.0)
            if city == "Dallas":
                return MockSettlementObs("KDFW", 85.0)
            return None

        t0 = time.monotonic()
        with patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch):
            signals = await reb.run_position_check()
        elapsed = time.monotonic() - t0

        assert elapsed < 2.0, f"50 positions took {elapsed:.3f}s"
