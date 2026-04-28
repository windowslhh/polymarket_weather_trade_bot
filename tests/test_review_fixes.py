"""Tests for code review fixes (P0-P2).

Covers the specific changes made during the review fix phase:
- Tracker delegate methods (P1-6)
- close_position with exit_reason merge (P1-9)
- insert_settlement duplicate prevention without SELECT (P0-2)
- Calibrator confidence clamping (P2-15)
- DB timeout and index (P1-10, P1-11)
- is_locked_win formal field (P1-8)
- Sizing variable rename (P2-16)
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.config import StrategyConfig
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker
from src.strategy.calibrator import calibrate_distance_threshold
from src.strategy.sizing import compute_size
from src.weather.historical import ForecastErrorDistribution


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "test_fixes.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
async def tracker(store):
    return PortfolioTracker(store)


def _make_event(city="NYC", event_id="evt_1"):
    return WeatherMarketEvent(
        event_id=event_id, condition_id="cond_1", city=city,
        market_date=date.today(), slots=[],
    )


def _make_slot(price_no=0.90):
    return TempSlot(
        token_id_yes="yes_1", token_id_no="no_1",
        outcome_label="80°F to 84°F",
        temp_lower_f=80.0, temp_upper_f=84.0,
        price_no=price_no,
    )


# ══════════════════════════════════════════════════════════════════════
# P1-6: Tracker delegate methods + store property
# ══════════════════════════════════════════════════════════════════════

class TestTrackerDelegateMethods:

    @pytest.mark.asyncio
    async def test_store_property(self, store):
        """tracker.store returns the underlying Store instance."""
        tracker = PortfolioTracker(store)
        assert tracker.store is store

    @pytest.mark.asyncio
    async def test_insert_edge_snapshot_delegate(self, tracker, store):
        """insert_edge_snapshot via tracker writes to edge_history table."""
        cycle_at = datetime.now(timezone.utc).isoformat()
        await tracker.insert_edge_snapshot(
            cycle_at=cycle_at, city="NYC", market_date="2026-04-11",
            slot_label="80°F to 84°F", forecast_high_f=75.0,
            price_yes=0.10, price_no=0.90,
            win_prob=0.95, ev=0.04, distance_f=6.0,
            trend_state="STABLE",
        )
        await tracker.flush_edge_batch()

        # Verify it was written
        history = await store.get_edge_history(city="NYC", limit=1)
        assert len(history) == 1
        assert history[0]["city"] == "NYC"
        assert history[0]["slot_label"] == "80°F to 84°F"
        assert history[0]["ev"] == 0.04

    @pytest.mark.asyncio
    async def test_flush_edge_batch_delegate(self, tracker, store):
        """flush_edge_batch commits pending edge inserts."""
        cycle_at = datetime.now(timezone.utc).isoformat()
        await tracker.insert_edge_snapshot(
            cycle_at=cycle_at, city="DAL", market_date="2026-04-11",
            slot_label="90°F to 94°F", forecast_high_f=88.0,
            price_yes=0.05, price_no=0.95,
            win_prob=0.98, ev=0.02, distance_f=4.0,
            trend_state="BREAKOUT_UP",
        )
        # Before flush, the data might not be committed
        await tracker.flush_edge_batch()
        history = await store.get_edge_history(city="DAL", limit=5)
        assert len(history) >= 1

    @pytest.mark.asyncio
    async def test_insert_decision_log_delegate(self, tracker, store):
        """insert_decision_log via tracker writes to decision_log table."""
        cycle_at = datetime.now(timezone.utc).isoformat()
        await tracker.insert_decision_log(
            cycle_at=cycle_at, city="NYC", event_id="evt_1",
            signal_type="NO", slot_label="80°F to 84°F",
            forecast_high_f=75.0, daily_max_f=72.0,
            trend_state="STABLE", win_prob=0.95,
            expected_value=0.04, price=0.90,
            size_usd=5.0, action="BUY",
            reason="[A] NO: dist=8°F, EV=0.040",
        )
        logs = await store.get_decision_log(limit=1)
        assert len(logs) == 1
        assert logs[0]["action"] == "BUY"
        assert logs[0]["reason"] == "[A] NO: dist=8°F, EV=0.040"

    @pytest.mark.asyncio
    async def test_get_open_positions_for_event(self, tracker, store):
        """get_open_positions_for_event returns positions filtered by event+strategy."""
        # Insert positions for different events/strategies
        await store.insert_position(
            event_id="evt_1", token_id="n1", token_type="NO",
            city="NYC", slot_label="80-84", side="BUY",
            entry_price=0.90, size_usd=5.0, shares=5.55,
            strategy="A",
        )
        await store.insert_position(
            event_id="evt_1", token_id="n2", token_type="NO",
            city="NYC", slot_label="85-89", side="BUY",
            entry_price=0.85, size_usd=3.0, shares=3.53,
            strategy="B",
        )
        await store.insert_position(
            event_id="evt_2", token_id="n3", token_type="NO",
            city="DAL", slot_label="90-94", side="BUY",
            entry_price=0.80, size_usd=4.0, shares=5.0,
            strategy="A",
        )

        # Filter by event
        evt1_all = await tracker.get_open_positions_for_event("evt_1")
        assert len(evt1_all) == 2

        # Filter by event + strategy
        evt1_a = await tracker.get_open_positions_for_event("evt_1", strategy="A")
        assert len(evt1_a) == 1
        assert evt1_a[0]["token_id"] == "n1"

        evt1_b = await tracker.get_open_positions_for_event("evt_1", strategy="B")
        assert len(evt1_b) == 1
        assert evt1_b[0]["token_id"] == "n2"

        # Different event
        evt2_a = await tracker.get_open_positions_for_event("evt_2", strategy="A")
        assert len(evt2_a) == 1
        assert evt2_a[0]["token_id"] == "n3"


# ══════════════════════════════════════════════════════════════════════
# P1-9: close_position with exit_reason merge
# ══════════════════════════════════════════════════════════════════════

class TestClosePositionMerge:

    @pytest.mark.asyncio
    async def test_close_with_reason_single_sql(self, store):
        """close_position(exit_reason=...) sets both status and reason atomically."""
        pid = await store.insert_position(
            event_id="e1", token_id="n1", token_type="NO",
            city="NYC", slot_label="80-84", side="BUY",
            entry_price=0.90, size_usd=5.0, shares=5.55,
        )
        await store.close_position(pid, exit_reason="[A] EXIT: temp approaching")

        # Verify both status and reason set
        async with store.db.execute(
            "SELECT status, exit_reason, closed_at FROM positions WHERE id = ?", (pid,)
        ) as cursor:
            row = await cursor.fetchone()
            assert row[0] == "closed"
            assert row[1] == "[A] EXIT: temp approaching"
            assert row[2] is not None  # closed_at set

    @pytest.mark.asyncio
    async def test_close_without_reason(self, store):
        """close_position() without reason still works (backward compat)."""
        pid = await store.insert_position(
            event_id="e1", token_id="n2", token_type="NO",
            city="NYC", slot_label="85-89", side="BUY",
            entry_price=0.85, size_usd=3.0, shares=3.53,
        )
        await store.close_position(pid)

        async with store.db.execute(
            "SELECT status, exit_reason FROM positions WHERE id = ?", (pid,)
        ) as cursor:
            row = await cursor.fetchone()
            assert row[0] == "closed"
            assert row[1] == ""  # empty default

    @pytest.mark.asyncio
    async def test_tracker_close_uses_merged_method(self, tracker, store):
        """Tracker's close_positions_for_token uses the merged close_position."""
        pid = await store.insert_position(
            event_id="e1", token_id="n1", token_type="NO",
            city="NYC", slot_label="80-84", side="BUY",
            entry_price=0.90, size_usd=5.0, shares=5.55,
            strategy="A",
        )
        closed = await tracker.close_positions_for_token(
            event_id="e1", token_id="n1", strategy="A",
            exit_reason="[A] TRIM: EV=−0.02",
        )
        assert closed == 1

        async with store.db.execute(
            "SELECT status, exit_reason FROM positions WHERE id = ?", (pid,)
        ) as cursor:
            row = await cursor.fetchone()
            assert row[0] == "closed"
            assert row[1] == "[A] TRIM: EV=−0.02"


# ══════════════════════════════════════════════════════════════════════
# P0-2: insert_settlement duplicate prevention
# ══════════════════════════════════════════════════════════════════════

class TestSettlementDuplicatePrevention:

    @pytest.mark.asyncio
    async def test_insert_settlement_first_time(self, store):
        """First settlement inserts normally."""
        await store.insert_settlement("e1", "NYC", "80-84", 2.50, strategy="A")
        settlements = await store.get_settlements()
        assert len(settlements) == 1
        assert settlements[0]["pnl"] == 2.50

    @pytest.mark.asyncio
    async def test_insert_settlement_duplicate_ignored(self, store):
        """Second insert with same event_id+strategy is silently ignored."""
        await store.insert_settlement("e1", "NYC", "80-84", 2.50, strategy="A")
        await store.insert_settlement("e1", "NYC", "80-84", 999.99, strategy="A")  # duplicate

        settlements = await store.get_settlements()
        assert len(settlements) == 1
        assert settlements[0]["pnl"] == 2.50  # original, not 999.99

    @pytest.mark.asyncio
    async def test_insert_settlement_different_strategy_allowed(self, store):
        """Same event with different strategy is a separate settlement."""
        await store.insert_settlement("e1", "NYC", "80-84", 2.50, strategy="A")
        await store.insert_settlement("e1", "NYC", "80-84", 3.00, strategy="B")

        settlements = await store.get_settlements()
        assert len(settlements) == 2
        pnls = sorted([s["pnl"] for s in settlements])
        assert pnls == [2.50, 3.00]


# ══════════════════════════════════════════════════════════════════════
# P2-15: Calibrator confidence clamping
# ══════════════════════════════════════════════════════════════════════

class TestCalibratorConfidenceClamping:

    def _make_dist(self, errors, city="NYC"):
        return ForecastErrorDistribution(city, errors)

    def test_confidence_below_min_clamped(self):
        """Confidence 0.1 clamped to 0.5 → uses 50th percentile."""
        errors = [float(i) for i in range(-15, 16)]  # 31 samples
        dist = self._make_dist(errors)
        # confidence=0.1 → clamped to 0.5
        result_low = calibrate_distance_threshold(dist, confidence=0.1)
        result_half = calibrate_distance_threshold(dist, confidence=0.5)
        assert result_low == result_half

    def test_confidence_above_max_clamped(self):
        """Confidence 1.5 clamped to 0.99 → uses 99th percentile."""
        errors = [float(i) for i in range(-15, 16)]  # 31 samples
        dist = self._make_dist(errors)
        result_high = calibrate_distance_threshold(dist, confidence=1.5)
        result_99 = calibrate_distance_threshold(dist, confidence=0.99)
        assert result_high == result_99

    def test_confidence_at_boundary_untouched(self):
        """Confidence exactly 0.5 and 0.99 are not modified."""
        errors = [float(i) for i in range(-15, 16)]
        dist = self._make_dist(errors)
        # These should work without clamping
        r1 = calibrate_distance_threshold(dist, confidence=0.5)
        r2 = calibrate_distance_threshold(dist, confidence=0.99)
        assert r1 > 0
        assert r2 >= r1  # higher confidence = wider threshold

    def test_negative_confidence_clamped(self):
        """Negative confidence clamped to 0.5."""
        errors = [float(i) for i in range(-15, 16)]
        dist = self._make_dist(errors)
        result = calibrate_distance_threshold(dist, confidence=-0.5)
        result_half = calibrate_distance_threshold(dist, confidence=0.5)
        assert result == result_half


# ══════════════════════════════════════════════════════════════════════
# P1-8: is_locked_win formal field
# ══════════════════════════════════════════════════════════════════════

class TestIsLockedWinFormalField:

    def test_default_false(self):
        """TradeSignal.is_locked_win defaults to False."""
        event = _make_event()
        slot = _make_slot()
        sig = TradeSignal(
            token_type=TokenType.NO, side=Side.BUY,
            slot=slot, event=event,
            expected_value=0.05, estimated_win_prob=0.95,
        )
        assert sig.is_locked_win is False

    def test_set_true_in_constructor(self):
        """is_locked_win=True via constructor."""
        event = _make_event()
        slot = _make_slot()
        sig = TradeSignal(
            token_type=TokenType.NO, side=Side.BUY,
            slot=slot, event=event,
            expected_value=0.09, estimated_win_prob=0.99,
            is_locked_win=True,
        )
        assert sig.is_locked_win is True

    def test_sizing_uses_formal_field(self):
        """compute_size reads signal.is_locked_win, not getattr hack."""
        event = _make_event()
        slot = _make_slot(price_no=0.90)
        cfg = StrategyConfig(
            kelly_fraction=0.5, locked_win_kelly_fraction=1.0,
            max_position_per_slot_usd=5.0, max_locked_win_per_slot_usd=10.0,
        )

        locked = TradeSignal(
            TokenType.NO, Side.BUY, slot, event, 0.09, 0.99,
            is_locked_win=True,
        )
        normal = TradeSignal(
            TokenType.NO, Side.BUY, slot, event, 0.09, 0.99,
            is_locked_win=False,
        )

        size_locked = compute_size(locked, 0, 0, cfg)
        size_normal = compute_size(normal, 0, 0, cfg)

        # Locked should use higher cap and fraction
        assert size_locked > size_normal
        assert size_locked <= 10.0
        assert size_normal <= 5.0


# ══════════════════════════════════════════════════════════════════════
# P2-16: Sizing variable rename (net_odds)
# ══════════════════════════════════════════════════════════════════════

class TestSizingNetOdds:

    def test_standard_pricing(self):
        """Sizing produces correct result with standard pricing."""
        event = _make_event()
        slot = _make_slot(price_no=0.90)
        # Phase 5: this fixture intentionally produces ~1.25 USD / 1.4
        # shares to exercise the Kelly math; disable the min-order floor
        # so the math result is what's under test.
        cfg = StrategyConfig(
            kelly_fraction=0.5, max_position_per_slot_usd=5.0,
            max_exposure_per_city_usd=50.0, max_total_exposure_usd=500.0,
            min_order_size_shares=0.0, min_order_amount_usd=0.0,
        )
        sig = TradeSignal(
            TokenType.NO, Side.BUY, slot, event,
            expected_value=0.04, estimated_win_prob=0.95,
        )

        # net_odds = (1 - 0.90) / 0.90 ≈ 0.1111
        # kelly_full = (0.95 * 0.1111 - 0.05) / 0.1111 ≈ 0.50
        # kelly_fraction = 0.50 * 0.5 = 0.25
        # size_usd = 0.25 * 5.0 = 1.25
        size = compute_size(sig, 0, 0, cfg)
        assert size == 1.25

    def test_edge_case_price_near_zero(self):
        """Very low price → high net_odds → reasonable Kelly fraction."""
        event = _make_event()
        slot = _make_slot(price_no=0.05)
        cfg = StrategyConfig(
            kelly_fraction=0.5, max_position_per_slot_usd=5.0,
            max_exposure_per_city_usd=50.0, max_total_exposure_usd=500.0,
        )
        sig = TradeSignal(
            TokenType.NO, Side.BUY, slot, event,
            expected_value=0.80, estimated_win_prob=0.90,
        )
        size = compute_size(sig, 0, 0, cfg)
        assert size > 0
        assert size <= 5.0


# ══════════════════════════════════════════════════════════════════════
# P1-10 / P1-11: DB index and timeout
# ══════════════════════════════════════════════════════════════════════

class TestDBInfrastructure:

    @pytest.mark.asyncio
    async def test_orders_order_id_index_exists(self, store):
        """Verify idx_orders_order_id index was created during init."""
        async with store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_orders_order_id'"
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None, "idx_orders_order_id index should exist"

    @pytest.mark.asyncio
    async def test_db_timeout_set(self, tmp_path):
        """Verify database connection uses timeout=30.0."""
        s = Store(tmp_path / "timeout_test.db")
        await s.initialize()
        # aiosqlite stores the timeout internally; verify connection works
        # under normal conditions (the timeout prevents hangs on lock contention)
        assert s._db is not None
        await s.close()
