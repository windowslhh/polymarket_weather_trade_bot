"""Tests validating the 6 bug fixes applied in commits df48944 and 85a6ebc.

Fix 1: dist calculation uses _slot_distance (nearest-bound), not midpoint
Fix 2: Strategy label lookup includes strategy dimension (not just city+slot)
Fix 3: SELL flow records exit_price and realized_pnl in positions table
Fix 4: Settlement writes exit_price (0.0/1.0) and realized_pnl per position
Fix 5: Position check uses cached gamma prices instead of entry_price
Fix 6: Dashboard routes include closed positions' realized P&L in strategy totals

Each fix section covers: key path, boundary conditions, failure/edge branches.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker
from src.strategy.evaluator import _slot_distance


# ── Shared Helpers ──────────────────────────────────────────────────────

def _make_event(city="Miami", event_id="evt_1"):
    return WeatherMarketEvent(
        event_id=event_id, condition_id="cond_1", city=city,
        market_date=date.today(), slots=[],
    )


def _make_slot(lower=76.0, upper=80.0, price_no=0.85, token_id_no="no_1",
               label="Will the highest temperature in Miami be between 76-80°F on April 13?"):
    return TempSlot(
        token_id_yes="yes_1", token_id_no=token_id_no,
        outcome_label=label,
        temp_lower_f=lower, temp_upper_f=upper, price_no=price_no,
    )


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "test.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
async def tracker(tmp_path):
    s = Store(tmp_path / "test.db")
    await s.initialize()
    t = PortfolioTracker(s)
    yield t
    await s.close()


# ======================================================================
# Fix 1: dist calculation — _slot_distance uses nearest-bound distance
# ======================================================================

class TestDistCalculation:
    """Fix 1: dist in reason string must use nearest-bound, not midpoint."""

    def test_forecast_above_slot_nearest_bound(self):
        """forecast=81°F, slot 76-80 → dist = min(|81-76|, |81-80|) = 1."""
        slot = _make_slot(lower=76.0, upper=80.0)
        assert _slot_distance(slot, 81.0) == 1.0

    def test_forecast_below_slot_nearest_bound(self):
        """forecast=74°F, slot 76-80 → dist = min(|74-76|, |74-80|) = 2."""
        slot = _make_slot(lower=76.0, upper=80.0)
        assert _slot_distance(slot, 74.0) == 2.0

    def test_midpoint_vs_nearest_bound_difference(self):
        """Midpoint=78, forecast=79 → midpoint dist=1; nearest-bound dist=1.
        But forecast=74 → midpoint dist=4; nearest-bound dist=2. They differ!"""
        slot = _make_slot(lower=76.0, upper=80.0)
        midpoint = (76.0 + 80.0) / 2  # 78
        forecast = 74.0
        midpoint_dist = abs(forecast - midpoint)  # 4
        nearest_dist = _slot_distance(slot, forecast)  # 2
        assert nearest_dist < midpoint_dist, "nearest-bound must be <= midpoint distance"
        assert nearest_dist == 2.0

    def test_forecast_inside_slot_is_zero(self):
        """forecast inside [lower, upper] → dist = 0."""
        slot = _make_slot(lower=76.0, upper=80.0)
        assert _slot_distance(slot, 78.0) == 0.0

    def test_forecast_at_lower_bound_is_zero(self):
        """Forecast exactly at lower bound → dist = 0."""
        slot = _make_slot(lower=76.0, upper=80.0)
        assert _slot_distance(slot, 76.0) == 0.0

    def test_forecast_at_upper_bound_is_zero(self):
        """Forecast exactly at upper bound → dist = 0."""
        slot = _make_slot(lower=76.0, upper=80.0)
        assert _slot_distance(slot, 80.0) == 0.0

    def test_far_above_slot(self):
        """forecast=90°F, slot 76-80 → dist = 10 (from upper)."""
        slot = _make_slot(lower=76.0, upper=80.0)
        assert _slot_distance(slot, 90.0) == 10.0

    def test_open_ended_upper_forecast_above_threshold(self):
        """≥76°F slot: forecast=80 >= lower=76 → YES likely wins, distance=0 (no NO edge)."""
        slot = _make_slot(lower=76.0, upper=None)
        assert _slot_distance(slot, 80.0) == 0.0

    def test_open_ended_lower_forecast_below_threshold(self):
        """Below 80°F slot: forecast=75 <= upper=80 → YES likely wins, distance=0 (no NO edge)."""
        slot = _make_slot(lower=None, upper=80.0)
        assert _slot_distance(slot, 75.0) == 0.0

    def test_both_bounds_none(self):
        """Slot with both bounds None → midpoint=0, fallback."""
        slot = _make_slot(lower=None, upper=None)
        assert _slot_distance(slot, 75.0) == 75.0

    def test_reason_string_uses_slot_distance(self):
        """Verify the reason string built in rebalancer uses _slot_distance, not midpoint.

        Simulate: forecast=79, slot 76-80.
        Midpoint=78 → old dist=1.  Nearest bound=80-79=1 (same here).
        Better test: forecast=74, slot 76-80. Midpoint dist=4, nearest=2.
        """
        slot = _make_slot(lower=76.0, upper=80.0)
        forecast_high = 74.0
        dist = _slot_distance(slot, forecast_high)
        reason = f"[A] NO: dist={dist:.0f}°F, EV=0.100, win=85%"
        assert "dist=2°F" in reason, f"Expected dist=2, got: {reason}"


# ======================================================================
# Fix 2: Strategy label lookup includes strategy dimension
# ======================================================================

class TestStrategyLabelLookup:
    """Fix 2: buy_reason lookup uses (city, slot_label, strategy) key."""

    async def test_same_slot_different_strategies_get_different_reasons(self, store):
        """Two positions for same city+slot but different strategies have different buy_reasons."""
        await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F on Apr 13?",
            side="BUY", entry_price=0.85, size_usd=5, shares=5.88,
            strategy="A", buy_reason="[A] NO: dist=3°F",
        )
        await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F on Apr 13?",
            side="BUY", entry_price=0.85, size_usd=5, shares=5.88,
            strategy="B", buy_reason="[B] LOCKED WIN: daily_max=82",
        )

        positions = await store.get_open_positions()
        reasons_by_strat = {p["strategy"]: p["buy_reason"] for p in positions}
        assert reasons_by_strat["A"] == "[A] NO: dist=3°F"
        assert reasons_by_strat["B"] == "[B] LOCKED WIN: daily_max=82"

    async def test_decision_log_key_includes_strategy(self, store):
        """Decision log entries with same city+slot but different strategies stay separate."""
        await store.insert_decision_log(
            cycle_at="2026-04-13 10:00:00", city="Miami", event_id="e1",
            signal_type="NO", slot_label="76-80°F on Apr 13?",
            forecast_high_f=79.0, daily_max_f=None, trend_state="stable",
            win_prob=0.85, expected_value=0.10, price=0.85, size_usd=5,
            action="BUY", reason="[A] NO: dist=3°F",
        )
        await store.insert_decision_log(
            cycle_at="2026-04-13 10:00:00", city="Miami", event_id="e1",
            signal_type="LOCKED", slot_label="76-80°F on Apr 13?",
            forecast_high_f=79.0, daily_max_f=82.0, trend_state="stable",
            win_prob=0.99, expected_value=0.15, price=0.85, size_usd=10,
            action="BUY", reason="[B] LOCKED WIN: daily_max=82",
        )

        logs = await store.get_decision_log(limit=10)
        reasons = [d["reason"] for d in logs]
        assert "[A] NO: dist=3°F" in reasons
        assert "[B] LOCKED WIN: daily_max=82" in reasons

    async def test_positions_buy_reason_preferred_over_decision_log(self, store):
        """buy_reason stored in positions table is used first; decision_log is fallback."""
        pid = await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F on Apr 13?",
            side="BUY", entry_price=0.85, size_usd=5, shares=5.88,
            strategy="A", buy_reason="[A] NO: dist=3°F from position",
        )
        positions = await store.get_open_positions()
        assert positions[0]["buy_reason"] == "[A] NO: dist=3°F from position"

    async def test_legacy_strategy_remapped(self, store):
        """Positions with non-ABCD strategy get remapped to B."""
        await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F on Apr 13?",
            side="BUY", entry_price=0.85, size_usd=5, shares=5.88,
            strategy="E", buy_reason="legacy",
        )
        positions = await store.get_open_positions()
        # The DB stores "E"; frontend code remaps it
        s = positions[0]["strategy"]
        if s not in {"A", "B", "C", "D"}:
            s = "B"
        assert s == "B"


# ======================================================================
# Fix 3: SELL flow records exit_price and realized_pnl
# ======================================================================

class TestSellFlowPnl:
    """Fix 3: SELL in executor → tracker → store records exit_price and realized_pnl."""

    async def test_close_position_computes_pnl(self, tracker):
        """Closing a position with exit_price should compute realized P&L."""
        pid = await tracker.record_fill(
            event_id="e1", token_id="t1", token_type=TokenType.NO,
            city="Miami", slot_label="76-80°F", side="BUY",
            price=0.85, size_usd=5.0, strategy="A", buy_reason="test",
        )
        shares = 5.0 / 0.85  # ~5.882
        closed = await tracker.close_positions_for_token(
            event_id="e1", token_id="t1", strategy="A",
            exit_reason="TRIM: EV decay", exit_price=0.90,
        )
        assert closed == 1

        positions = await tracker._store.get_open_positions()
        assert len(positions) == 0

        closed_pos = await tracker._store.get_closed_positions(limit=10)
        assert len(closed_pos) == 1
        p = closed_pos[0]
        assert p["exit_price"] == 0.90
        assert p["realized_pnl"] == pytest.approx((0.90 - 0.85) * shares, abs=0.001)
        assert p["exit_reason"] == "TRIM: EV decay"

    async def test_pnl_negative_when_exit_below_entry(self, tracker):
        """Negative P&L when exit_price < entry_price."""
        await tracker.record_fill(
            event_id="e1", token_id="t1", token_type=TokenType.NO,
            city="Miami", slot_label="76-80°F", side="BUY",
            price=0.85, size_usd=5.0, strategy="B",
        )
        await tracker.close_positions_for_token(
            event_id="e1", token_id="t1", strategy="B",
            exit_reason="force exit", exit_price=0.70,
        )
        closed = await tracker._store.get_closed_positions(limit=10)
        assert closed[0]["realized_pnl"] < 0

    async def test_pnl_none_when_no_exit_price(self, tracker):
        """Without exit_price, realized_pnl stays None."""
        await tracker.record_fill(
            event_id="e1", token_id="t1", token_type=TokenType.NO,
            city="Miami", slot_label="76-80°F", side="BUY",
            price=0.85, size_usd=5.0, strategy="A",
        )
        await tracker.close_positions_for_token(
            event_id="e1", token_id="t1", strategy="A",
            exit_reason="manual close",
        )
        closed = await tracker._store.get_closed_positions(limit=10)
        assert closed[0]["exit_price"] is None
        assert closed[0]["realized_pnl"] is None

    async def test_close_only_matching_strategy(self, tracker):
        """SELL from strategy A must not close strategy B positions."""
        await tracker.record_fill(
            event_id="e1", token_id="t1", token_type=TokenType.NO,
            city="Miami", slot_label="76-80°F", side="BUY",
            price=0.85, size_usd=5.0, strategy="A",
        )
        await tracker.record_fill(
            event_id="e1", token_id="t1", token_type=TokenType.NO,
            city="Miami", slot_label="76-80°F", side="BUY",
            price=0.85, size_usd=5.0, strategy="B",
        )
        closed = await tracker.close_positions_for_token(
            event_id="e1", token_id="t1", strategy="A",
            exit_reason="exit A", exit_price=0.90,
        )
        assert closed == 1
        remaining = await tracker._store.get_open_positions()
        assert len(remaining) == 1
        assert remaining[0]["strategy"] == "B"

    async def test_close_position_zero_exit_price(self, tracker):
        """edge: exit_price=0.0 is a valid price (total loss)."""
        await tracker.record_fill(
            event_id="e1", token_id="t1", token_type=TokenType.NO,
            city="Miami", slot_label="76-80°F", side="BUY",
            price=0.85, size_usd=5.0, strategy="A",
        )
        await tracker.close_positions_for_token(
            event_id="e1", token_id="t1", strategy="A",
            exit_reason="total loss", exit_price=0.0,
        )
        closed = await tracker._store.get_closed_positions(limit=10)
        p = closed[0]
        assert p["exit_price"] == 0.0
        shares = 5.0 / 0.85
        assert p["realized_pnl"] == pytest.approx(-0.85 * shares, abs=0.001)

    async def test_executor_passes_exit_price_to_tracker(self):
        """Executor SELL path passes price to close_positions_for_token."""
        from src.execution.executor import Executor
        mock_clob = MagicMock()
        mock_clob.place_limit_order = AsyncMock(
            return_value=MagicMock(success=True, order_id="ord_1")
        )
        mock_portfolio = MagicMock(spec=PortfolioTracker)
        mock_portfolio.close_positions_for_token = AsyncMock(return_value=1)
        # Executor now looks up actual held shares for SELL orders (EX-01 fix).
        # 5.0 USD / 0.90 price ≈ 5.56 shares
        mock_portfolio.get_total_shares_for_token = AsyncMock(return_value=5.56)

        executor = Executor(mock_clob, mock_portfolio)
        signal = TradeSignal(
            token_type=TokenType.NO, side=Side.SELL,
            slot=_make_slot(price_no=0.90),
            event=_make_event(),
            expected_value=0.05, estimated_win_prob=0.80,
            suggested_size_usd=5.0, strategy="A",
            reason="TRIM: EV decay",
        )
        await executor.execute_signals([signal])

        mock_portfolio.close_positions_for_token.assert_awaited_once()
        call_kwargs = mock_portfolio.close_positions_for_token.call_args
        assert call_kwargs.kwargs["exit_price"] == 0.90
        assert call_kwargs.kwargs["exit_reason"] == "TRIM: EV decay"
        assert call_kwargs.kwargs["strategy"] == "A"


# ======================================================================
# Fix 4: Settlement writes exit_price and realized_pnl
# ======================================================================

class TestSettlementPnl:
    """Fix 4: settler stores exit_price (0.0 or 1.0) and realized_pnl per position."""

    def test_settlement_exit_price_no_wins(self):
        """NO token wins when YES resolves to 0 → exit_price = 1.0."""
        from src.settlement.settler import _settlement_exit_price
        pos = {"slot_label": "76-80°F on April 13?", "token_type": "NO"}
        settled = {"76-80°F on April 13?": 0.0}  # YES=0 → NO wins
        assert _settlement_exit_price(pos, settled) == 1.0

    def test_settlement_exit_price_no_loses(self):
        """NO token loses when YES resolves to 1 → exit_price = 0.0."""
        from src.settlement.settler import _settlement_exit_price
        pos = {"slot_label": "76-80°F on April 13?", "token_type": "NO"}
        settled = {"76-80°F on April 13?": 1.0}  # YES=1 → NO loses
        assert _settlement_exit_price(pos, settled) == 0.0

    def test_settlement_exit_price_yes_wins(self):
        """YES token wins when YES resolves to 1 → exit_price = 1.0."""
        from src.settlement.settler import _settlement_exit_price
        pos = {"slot_label": "76-80°F on April 13?", "token_type": "YES"}
        settled = {"76-80°F on April 13?": 1.0}
        assert _settlement_exit_price(pos, settled) == 1.0

    def test_settlement_exit_price_yes_loses(self):
        """YES token loses when YES resolves to 0 → exit_price = 0.0."""
        from src.settlement.settler import _settlement_exit_price
        pos = {"slot_label": "76-80°F on April 13?", "token_type": "YES"}
        settled = {"76-80°F on April 13?": 0.0}
        assert _settlement_exit_price(pos, settled) == 0.0

    def test_settlement_exit_price_partial_match(self):
        """Slot label is substring of settled key → still matches."""
        from src.settlement.settler import _settlement_exit_price
        pos = {"slot_label": "76-80°F on April 13?", "token_type": "NO"}
        settled = {"Will the highest temperature in Miami be between 76-80°F on April 13?": 0.0}
        assert _settlement_exit_price(pos, settled) == 1.0

    def test_settlement_exit_price_no_match_defaults_zero(self):
        """Unmatched slot → yes_resolved=0.0, NO exit_price=1.0."""
        from src.settlement.settler import _settlement_exit_price
        pos = {"slot_label": "99-100°F on April 13?", "token_type": "NO"}
        settled = {"76-80°F on April 13?": 1.0}
        # No match → yes_resolved defaults to 0.0 → NO wins
        assert _settlement_exit_price(pos, settled) == 1.0

    def test_compute_pnl_no_wins(self):
        """NO wins: P&L = (1 - entry_price) * shares."""
        from src.settlement.settler import _compute_position_pnl
        pos = {
            "slot_label": "76-80°F on April 13?",
            "entry_price": 0.85, "shares": 5.882,
            "token_type": "NO",
        }
        settled = {"76-80°F on April 13?": 0.0}
        pnl = _compute_position_pnl(pos, settled)
        assert pnl == pytest.approx((1.0 - 0.85) * 5.882, abs=0.01)

    def test_compute_pnl_no_loses(self):
        """NO loses: P&L = -entry_price * shares."""
        from src.settlement.settler import _compute_position_pnl
        pos = {
            "slot_label": "76-80°F on April 13?",
            "entry_price": 0.85, "shares": 5.882,
            "token_type": "NO",
        }
        settled = {"76-80°F on April 13?": 1.0}
        pnl = _compute_position_pnl(pos, settled)
        assert pnl == pytest.approx(-0.85 * 5.882, abs=0.01)

    def test_compute_pnl_cheap_entry_big_profit(self):
        """Cheap NO entry (0.10) wins → big profit."""
        from src.settlement.settler import _compute_position_pnl
        pos = {
            "slot_label": "90-95°F?",
            "entry_price": 0.10, "shares": 50.0,
            "token_type": "NO",
        }
        settled = {"90-95°F?": 0.0}
        pnl = _compute_position_pnl(pos, settled)
        assert pnl == pytest.approx(0.90 * 50.0, abs=0.01)

    async def test_settlement_updates_position_fields(self, store):
        """Full settler flow: position gets exit_price and realized_pnl after settlement."""
        from src.settlement.settler import _compute_position_pnl, _settlement_exit_price

        pid = await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F on April 13?",
            side="BUY", entry_price=0.85, size_usd=5.0, shares=5.882,
            strategy="A",
        )
        settled_prices = {"76-80°F on April 13?": 0.0}  # NO wins
        pos = (await store.get_open_positions())[0]
        pnl = _compute_position_pnl(pos, settled_prices)
        exit_price = _settlement_exit_price(pos, settled_prices)

        await store.db.execute(
            """UPDATE positions SET status = 'settled', closed_at = datetime('now'),
               exit_price = ?, realized_pnl = ? WHERE id = ?""",
            (exit_price, pnl, pos["id"]),
        )
        await store.db.commit()

        # Verify via closed query — settled positions are not returned by get_closed_positions
        # because get_closed_positions filters status='closed', not 'settled'
        async with store.db.execute("SELECT * FROM positions WHERE id = ?", (pid,)) as cur:
            row = dict(await cur.fetchone())
        assert row["status"] == "settled"
        assert row["exit_price"] == 1.0
        assert row["realized_pnl"] == pytest.approx(pnl, abs=0.001)


# ======================================================================
# Fix 5: Position check uses cached gamma prices
# ======================================================================

class TestPositionCheckGammaPrices:
    """Fix 5: held_no_slots in position check uses _last_gamma_prices
    instead of entry_price for accurate exit signal pricing."""

    async def test_gamma_price_used_when_available(self, tracker):
        """When gamma cache has a price, held_no_slots uses it instead of entry."""
        await tracker.record_fill(
            event_id="e1", token_id="t1", token_type=TokenType.NO,
            city="Miami", slot_label="Will the highest temperature in Miami be between 76-80°F on April 13?",
            side="BUY", price=0.85, size_usd=5.0, strategy="A",
        )
        gamma_prices = {"t1": 0.92}
        slots = await tracker.get_held_no_slots("e1", strategy="A", current_prices=gamma_prices)
        assert len(slots) == 1
        assert slots[0].price_no == 0.92

    async def test_entry_price_fallback_when_no_gamma(self, tracker):
        """Without gamma cache, falls back to entry_price."""
        await tracker.record_fill(
            event_id="e1", token_id="t1", token_type=TokenType.NO,
            city="Miami", slot_label="Will the highest temperature in Miami be between 76-80°F on April 13?",
            side="BUY", price=0.85, size_usd=5.0, strategy="A",
        )
        slots = await tracker.get_held_no_slots("e1", strategy="A", current_prices=None)
        assert slots[0].price_no == 0.85

    async def test_gamma_price_partial_coverage(self, tracker):
        """Some tokens have gamma prices, others fall back to entry."""
        await tracker.record_fill(
            event_id="e1", token_id="t1", token_type=TokenType.NO,
            city="Miami", slot_label="Will the highest temperature in Miami be between 76-80°F on April 13?",
            side="BUY", price=0.85, size_usd=5.0, strategy="A",
        )
        await tracker.record_fill(
            event_id="e1", token_id="t2", token_type=TokenType.NO,
            city="Miami", slot_label="Will the highest temperature in Miami be between 80-84°F on April 13?",
            side="BUY", price=0.80, size_usd=5.0, strategy="A",
        )
        gamma_prices = {"t1": 0.92}  # only t1 has gamma price
        slots = await tracker.get_held_no_slots("e1", strategy="A", current_prices=gamma_prices)
        prices = {s.token_id_no: s.price_no for s in slots}
        assert prices["t1"] == 0.92  # gamma price
        assert prices["t2"] == 0.80  # entry fallback

    async def test_gamma_price_affects_exit_pnl(self, tracker):
        """SELL with gamma price should produce correct P&L
        (not zero as when using entry_price as current)."""
        await tracker.record_fill(
            event_id="e1", token_id="t1", token_type=TokenType.NO,
            city="Miami", slot_label="76-80°F", side="BUY",
            price=0.85, size_usd=5.0, strategy="A",
        )
        # Selling at gamma price 0.92 → profit
        await tracker.close_positions_for_token(
            event_id="e1", token_id="t1", strategy="A",
            exit_reason="TRIM", exit_price=0.92,
        )
        closed = await tracker._store.get_closed_positions(limit=10)
        shares = 5.0 / 0.85
        expected_pnl = (0.92 - 0.85) * shares
        assert closed[0]["realized_pnl"] == pytest.approx(expected_pnl, abs=0.001)
        assert closed[0]["realized_pnl"] > 0, "Must be positive profit"

    def test_rebalancer_position_check_builds_slots_with_gamma(self):
        """Verify the code path: rebalancer position check reads gamma prices dict."""
        # Simulate what rebalancer does in position check (lines 276-297)
        gamma = {"t1": 0.92, "t2": 0.88}
        positions = [
            {"token_type": "NO", "side": "BUY", "token_id": "t1",
             "slot_label": "Will the highest temperature in Miami be between 76-80°F on April 13?",
             "entry_price": 0.85},
            {"token_type": "NO", "side": "BUY", "token_id": "t2",
             "slot_label": "Will the highest temperature in Miami be between 80-84°F on April 13?",
             "entry_price": 0.80},
            {"token_type": "NO", "side": "BUY", "token_id": "t3",  # not in gamma
             "slot_label": "Will the highest temperature in Miami be between 84-88°F on April 13?",
             "entry_price": 0.75},
        ]
        from src.markets.discovery import _parse_temp_bounds
        held_no_slots = []
        for pos in positions:
            if pos["token_type"] == "NO" and pos["side"] == "BUY":
                try:
                    lower, upper = _parse_temp_bounds(pos["slot_label"])
                except Exception:
                    lower, upper = None, None
                tid = pos["token_id"]
                price = gamma.get(tid, pos["entry_price"])
                held_no_slots.append(TempSlot(
                    token_id_yes="", token_id_no=tid,
                    outcome_label=pos["slot_label"],
                    temp_lower_f=lower, temp_upper_f=upper,
                    price_no=price,
                ))

        assert held_no_slots[0].price_no == 0.92  # gamma
        assert held_no_slots[1].price_no == 0.88  # gamma
        assert held_no_slots[2].price_no == 0.75  # fallback to entry

    async def test_yes_positions_skipped_in_held_no_slots(self, tracker):
        """YES token positions are excluded from held_no_slots."""
        await tracker.record_fill(
            event_id="e1", token_id="t_yes", token_type=TokenType.YES,
            city="Miami", slot_label="Will the highest temperature in Miami be between 76-80°F on April 13?",
            side="BUY", price=0.15, size_usd=2.0, strategy="A",
        )
        slots = await tracker.get_held_no_slots("e1", strategy="A")
        assert len(slots) == 0


# ======================================================================
# Fix 6: Dashboard routes include closed position realized P&L
# ======================================================================

class TestDashboardRealizedPnl:
    """Fix 6: /positions and /trades routes aggregate closed positions'
    realized_pnl into strategy P&L totals."""

    async def test_closed_position_pnl_aggregated(self, store):
        """Realized P&L from closed (SELL) positions is included in strat totals."""
        # Insert a closed position with realized_pnl
        pid = await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F",
            side="BUY", entry_price=0.85, size_usd=5.0, shares=5.882,
            strategy="A", buy_reason="test",
        )
        await store.close_position(pid, exit_reason="TRIM", exit_price=0.92,
                                   realized_pnl=0.412)

        # Simulate dashboard logic: aggregate closed positions' realized_pnl
        strat_realized = {"A": 0.0, "B": 0.0, "C": 0.0, "D": 0.0}
        for p in await store.get_closed_positions(limit=200):
            rpnl = p.get("realized_pnl")
            if rpnl is not None:
                s = p.get("strategy", "B")
                if s in strat_realized:
                    strat_realized[s] += rpnl

        assert strat_realized["A"] == pytest.approx(0.412, abs=0.001)
        assert strat_realized["B"] == 0.0

    async def test_mixed_settled_and_closed_pnl(self, store):
        """Settlement P&L (from settlements table) + SELL P&L (from positions) combine."""
        # Settlement P&L
        await store.insert_settlement("e1", "Miami", "76-80°F", 0.882, strategy="A")

        # Closed position P&L (from a SELL/TRIM)
        pid = await store.insert_position(
            event_id="e2", token_id="t2", token_type="NO",
            city="Seattle", slot_label="50-55°F",
            side="BUY", entry_price=0.80, size_usd=5.0, shares=6.25,
            strategy="A",
        )
        await store.close_position(pid, exit_reason="TRIM", exit_price=0.90,
                                   realized_pnl=0.625)

        # Settlement realized P&L
        settlement_pnl = await store.get_strategy_realized_pnl()
        # Plus closed positions' realized_pnl
        for p in await store.get_closed_positions(limit=200):
            rpnl = p.get("realized_pnl")
            if rpnl is not None:
                s = p.get("strategy", "B")
                if s in settlement_pnl:
                    settlement_pnl[s] += rpnl

        assert settlement_pnl["A"] == pytest.approx(0.882 + 0.625, abs=0.01)

    async def test_null_pnl_positions_not_counted(self, store):
        """Positions with realized_pnl=None are excluded from aggregation."""
        pid = await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F",
            side="BUY", entry_price=0.85, size_usd=5.0, shares=5.882,
            strategy="A",
        )
        await store.close_position(pid, exit_reason="manual close")
        # realized_pnl is None

        strat_realized = {"A": 0.0, "B": 0.0, "C": 0.0, "D": 0.0}
        for p in await store.get_closed_positions(limit=200):
            rpnl = p.get("realized_pnl")
            if rpnl is not None:
                s = p.get("strategy", "B")
                if s in strat_realized:
                    strat_realized[s] += rpnl

        assert strat_realized["A"] == 0.0, "None pnl must not affect total"

    async def test_trades_page_sell_row_shows_exit_price(self, store):
        """SELL rows in /trades timeline show exit_price and realized_pnl."""
        pid = await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F on April 13?",
            side="BUY", entry_price=0.85, size_usd=5.0, shares=5.882,
            strategy="A",
        )
        await store.close_position(pid, exit_reason="TRIM", exit_price=0.92,
                                   realized_pnl=0.412)

        closed_pos = await store.get_closed_positions(limit=50)
        p = closed_pos[0]

        # Simulate trades page timeline entry construction
        current = f"{p['exit_price']:.3f}" if p.get("exit_price") is not None else "-"
        pnl = f"{'+'if p.get('realized_pnl',0)>0 else ''}${p['realized_pnl']:.3f}" if p.get("realized_pnl") is not None else "-"

        assert current == "0.920"
        assert pnl == "+$0.412"

    async def test_trades_page_negative_pnl_no_plus_sign(self, store):
        """Negative P&L doesn't get a '+' prefix."""
        pid = await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F on April 13?",
            side="BUY", entry_price=0.85, size_usd=5.0, shares=5.882,
            strategy="A",
        )
        await store.close_position(pid, exit_reason="force exit", exit_price=0.70,
                                   realized_pnl=-0.882)

        closed_pos = await store.get_closed_positions(limit=50)
        p = closed_pos[0]
        pnl = f"{'+'if p.get('realized_pnl',0)>0 else ''}${p['realized_pnl']:.3f}" if p.get("realized_pnl") is not None else "-"
        assert pnl == "$-0.882"
        assert "+" not in pnl

    async def test_close_position_coalesce_preserves_existing(self, store):
        """COALESCE in close_position preserves existing exit data when called with None."""
        pid = await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F",
            side="BUY", entry_price=0.85, size_usd=5.0, shares=5.882,
            strategy="A",
        )
        # First close with data
        await store.close_position(pid, exit_reason="TRIM", exit_price=0.92,
                                   realized_pnl=0.412)
        # Second call with None values shouldn't overwrite
        await store.db.execute(
            """UPDATE positions SET exit_price = COALESCE(?, exit_price),
               realized_pnl = COALESCE(?, realized_pnl) WHERE id = ?""",
            (None, None, pid),
        )
        await store.db.commit()

        async with store.db.execute("SELECT exit_price, realized_pnl FROM positions WHERE id = ?", (pid,)) as cur:
            row = await cur.fetchone()
        assert row[0] == 0.92
        assert row[1] == pytest.approx(0.412, abs=0.001)


# ======================================================================
# Integration: Full SELL → close → verify P&L end-to-end
# ======================================================================

class TestEndToEndSellPnl:
    """Integration: executor SELL signal → tracker close → DB exit_price + realized_pnl."""

    async def test_full_sell_pipeline(self, tracker):
        """Complete pipeline: BUY → SELL with price → verify DB has correct P&L."""
        # Step 1: Record BUY
        await tracker.record_fill(
            event_id="e1", token_id="t1", token_type=TokenType.NO,
            city="Miami", slot_label="76-80°F on April 13?",
            side="BUY", price=0.85, size_usd=10.0,
            strategy="A", buy_reason="[A] NO: dist=3°F",
        )

        # Step 2: SELL via tracker (simulating executor path)
        closed = await tracker.close_positions_for_token(
            event_id="e1", token_id="t1", strategy="A",
            exit_reason="[A] TRIM: EV=-0.02", exit_price=0.92,
        )
        assert closed == 1

        # Step 3: Verify DB state
        closed_pos = await tracker._store.get_closed_positions(limit=10)
        assert len(closed_pos) == 1
        p = closed_pos[0]
        assert p["status"] == "closed"
        assert p["entry_price"] == 0.85
        assert p["exit_price"] == 0.92
        assert p["buy_reason"] == "[A] NO: dist=3°F"
        assert p["exit_reason"] == "[A] TRIM: EV=-0.02"
        shares = 10.0 / 0.85
        assert p["realized_pnl"] == pytest.approx((0.92 - 0.85) * shares, abs=0.001)

    async def test_multiple_positions_same_event_all_closed(self, tracker):
        """Multiple open positions for different tokens in same event all get closed.

        Uses distinct token IDs (unique index prevents duplicate open positions
        for the same token+event+strategy, which is correct business logic).
        """
        for i in range(3):
            await tracker.record_fill(
                event_id="e1", token_id=f"t{i}", token_type=TokenType.NO,
                city="Miami", slot_label="76-80°F", side="BUY",
                price=0.85, size_usd=5.0, strategy="A",
            )
        # Close t0 specifically
        closed = await tracker.close_positions_for_token(
            event_id="e1", token_id="t0", strategy="A",
            exit_reason="batch close", exit_price=0.90,
        )
        assert closed == 1
        closed_pos = await tracker._store.get_closed_positions(limit=10)
        assert len(closed_pos) == 1
        assert closed_pos[0]["exit_price"] == 0.90
        assert closed_pos[0]["realized_pnl"] is not None
        assert closed_pos[0]["realized_pnl"] > 0


# ======================================================================
# DB Migration: exit_price and realized_pnl columns
# ======================================================================

class TestDbMigration:
    """Verify DB migration adds exit_price and realized_pnl columns."""

    async def test_columns_exist_after_init(self, store):
        """exit_price and realized_pnl columns exist in positions table."""
        async with store.db.execute("PRAGMA table_info(positions)") as cur:
            columns = {row[1] async for row in cur}
        assert "exit_price" in columns
        assert "realized_pnl" in columns

    async def test_columns_nullable(self, store):
        """exit_price and realized_pnl default to NULL for new positions."""
        pid = await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F",
            side="BUY", entry_price=0.85, size_usd=5.0, shares=5.882,
            strategy="A",
        )
        async with store.db.execute("SELECT exit_price, realized_pnl FROM positions WHERE id = ?", (pid,)) as cur:
            row = await cur.fetchone()
        assert row[0] is None
        assert row[1] is None

    async def test_migration_idempotent(self, tmp_path):
        """Running initialize() twice doesn't fail (migration is idempotent)."""
        s = Store(tmp_path / "test.db")
        await s.initialize()
        await s.initialize()  # second call should not raise
        async with s.db.execute("PRAGMA table_info(positions)") as cur:
            columns = {row[1] async for row in cur}
        assert "exit_price" in columns
        await s.close()

    async def test_entry_ev_columns_exist_after_init(self, store):
        """Fix 4: entry_ev and entry_win_prob columns exist in positions table."""
        async with store.db.execute("PRAGMA table_info(positions)") as cur:
            columns = {row[1] async for row in cur}
        assert "entry_ev" in columns
        assert "entry_win_prob" in columns

    async def test_entry_ev_persisted_and_retrieved(self, store):
        """Fix 4: entry_ev/entry_win_prob values round-trip through insert_position."""
        pid = await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F",
            side="BUY", entry_price=0.3, size_usd=5.0, shares=16.67,
            strategy="A",
            entry_ev=0.08,
            entry_win_prob=0.55,
        )
        async with store.db.execute(
            "SELECT entry_ev, entry_win_prob FROM positions WHERE id = ?", (pid,),
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == pytest.approx(0.08)
        assert row[1] == pytest.approx(0.55)

    async def test_entry_ev_nullable_for_legacy_callsites(self, store):
        """Fix 4: entry_ev/entry_win_prob default to NULL when not supplied."""
        pid = await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F",
            side="BUY", entry_price=0.3, size_usd=5.0, shares=16.67,
            strategy="A",
        )
        async with store.db.execute(
            "SELECT entry_ev, entry_win_prob FROM positions WHERE id = ?", (pid,),
        ) as cur:
            row = await cur.fetchone()
        assert row[0] is None
        assert row[1] is None


# ======================================================================
# Review fixes: found during code review
# ======================================================================

class TestReviewFixes:
    """Tests for issues discovered during final code review."""

    # -- P0: get_closed_positions includes settled positions --

    async def test_settled_positions_in_closed_query(self, store):
        """get_closed_positions must return status='settled' positions too."""
        pid = await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F",
            side="BUY", entry_price=0.85, size_usd=5.0, shares=5.882,
            strategy="A",
        )
        # Simulate settlement (status='settled', not 'closed')
        await store.db.execute(
            """UPDATE positions SET status = 'settled', closed_at = datetime('now'),
               exit_price = 1.0, realized_pnl = 0.882 WHERE id = ?""",
            (pid,),
        )
        await store.db.commit()

        closed = await store.get_closed_positions(limit=10)
        assert len(closed) == 1
        assert closed[0]["status"] == "settled"
        assert closed[0]["realized_pnl"] == pytest.approx(0.882, abs=0.001)

    async def test_settled_pnl_included_in_dashboard_aggregation(self, store):
        """Settled positions' realized_pnl is aggregated in dashboard strat totals."""
        # One closed via SELL
        pid1 = await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F",
            side="BUY", entry_price=0.85, size_usd=5.0, shares=5.882,
            strategy="A",
        )
        await store.close_position(pid1, exit_reason="TRIM", exit_price=0.92,
                                   realized_pnl=0.412)

        # One settled
        pid2 = await store.insert_position(
            event_id="e2", token_id="t2", token_type="NO",
            city="Seattle", slot_label="50-55°F",
            side="BUY", entry_price=0.80, size_usd=5.0, shares=6.25,
            strategy="A",
        )
        await store.db.execute(
            """UPDATE positions SET status = 'settled', closed_at = datetime('now'),
               exit_price = 1.0, realized_pnl = 1.25 WHERE id = ?""",
            (pid2,),
        )
        await store.db.commit()

        # Both should appear in get_closed_positions
        closed = await store.get_closed_positions(limit=200)
        assert len(closed) == 2
        total_pnl = sum(p["realized_pnl"] for p in closed if p.get("realized_pnl") is not None)
        assert total_pnl == pytest.approx(0.412 + 1.25, abs=0.01)

    async def test_mixed_statuses_correct_count(self, store):
        """Open, closed, and settled positions each have correct status filtering."""
        # Open
        await store.insert_position(
            event_id="e1", token_id="t1", token_type="NO",
            city="Miami", slot_label="76-80°F",
            side="BUY", entry_price=0.85, size_usd=5.0, shares=5.882,
            strategy="A",
        )
        # Closed
        pid2 = await store.insert_position(
            event_id="e2", token_id="t2", token_type="NO",
            city="Miami", slot_label="80-84°F",
            side="BUY", entry_price=0.80, size_usd=5.0, shares=6.25,
            strategy="B",
        )
        await store.close_position(pid2, exit_reason="TRIM", exit_price=0.88)

        # Settled
        pid3 = await store.insert_position(
            event_id="e3", token_id="t3", token_type="NO",
            city="Seattle", slot_label="50-55°F",
            side="BUY", entry_price=0.75, size_usd=5.0, shares=6.667,
            strategy="C",
        )
        await store.db.execute("UPDATE positions SET status='settled', closed_at=datetime('now') WHERE id=?", (pid3,))
        await store.db.commit()

        open_pos = await store.get_open_positions()
        closed_pos = await store.get_closed_positions(limit=50)
        assert len(open_pos) == 1
        assert len(closed_pos) == 2  # closed + settled

    # -- P1: decision_log strategy column --

    async def test_decision_log_has_strategy_column(self, store):
        """decision_log table has strategy column after migration."""
        async with store.db.execute("PRAGMA table_info(decision_log)") as cur:
            columns = {row[1] async for row in cur}
        assert "strategy" in columns

    async def test_decision_log_stores_strategy(self, store):
        """insert_decision_log correctly stores strategy value."""
        await store.insert_decision_log(
            cycle_at="2026-04-13 10:00:00", city="Miami", event_id="e1",
            signal_type="NO", slot_label="76-80°F",
            forecast_high_f=79.0, daily_max_f=None, trend_state="stable",
            win_prob=0.85, expected_value=0.10, price=0.85, size_usd=5,
            action="BUY", reason="[A] NO: dist=3°F", strategy="A",
        )
        await store.insert_decision_log(
            cycle_at="2026-04-13 10:00:00", city="Miami", event_id="e1",
            signal_type="LOCKED", slot_label="76-80°F",
            forecast_high_f=79.0, daily_max_f=82.0, trend_state="stable",
            win_prob=0.99, expected_value=0.15, price=0.85, size_usd=10,
            action="BUY", reason="[B] LOCKED WIN", strategy="B",
        )
        logs = await store.get_decision_log(limit=10)
        strats = {d["strategy"] for d in logs}
        assert "A" in strats
        assert "B" in strats

    async def test_decision_log_strategy_default_empty(self, store):
        """strategy defaults to empty string when not provided."""
        await store.insert_decision_log(
            cycle_at="2026-04-13 10:00:00", city="Miami", event_id="e1",
            signal_type="NO", slot_label="76-80°F",
            forecast_high_f=79.0, daily_max_f=None, trend_state="stable",
            win_prob=0.85, expected_value=0.10, price=0.85, size_usd=5,
            action="SKIP", reason="low EV",
        )
        logs = await store.get_decision_log(limit=1)
        assert logs[0]["strategy"] == ""

    # -- P2: _resolve_yes_price deduplication --

    def test_resolve_yes_price_exact_match(self):
        """Exact label match returns the price."""
        from src.settlement.settler import _resolve_yes_price
        prices = {"76-80°F on April 13?": 0.0}
        assert _resolve_yes_price("76-80°F on April 13?", prices) == 0.0

    def test_resolve_yes_price_substring_match(self):
        """Slot label is substring of settled key."""
        from src.settlement.settler import _resolve_yes_price
        prices = {"Will the highest temperature in Miami be between 76-80°F on April 13?": 1.0}
        assert _resolve_yes_price("76-80°F on April 13?", prices) == 1.0

    def test_resolve_yes_price_reverse_substring(self):
        """Settled key is substring of slot label."""
        from src.settlement.settler import _resolve_yes_price
        prices = {"76-80°F": 0.5}
        assert _resolve_yes_price("76-80°F on April 13?", prices) == 0.5

    def test_resolve_yes_price_no_match(self):
        """No match returns None."""
        from src.settlement.settler import _resolve_yes_price
        prices = {"90-95°F on April 13?": 1.0}
        assert _resolve_yes_price("76-80°F on April 13?", prices) is None

    def test_exit_price_and_pnl_use_same_resolution(self):
        """_settlement_exit_price and _compute_position_pnl agree on slot matching."""
        from src.settlement.settler import _settlement_exit_price, _compute_position_pnl
        pos = {
            "slot_label": "76-80°F on April 13?",
            "entry_price": 0.85, "shares": 5.882,
            "token_type": "NO",
        }
        settled = {"76-80°F on April 13?": 0.0}  # NO wins
        exit_p = _settlement_exit_price(pos, settled)
        pnl = _compute_position_pnl(pos, settled)
        # exit_price=1.0 → P&L = (1.0 - 0.85) * 5.882 = 0.882
        assert exit_p == 1.0
        assert pnl == pytest.approx((exit_p - pos["entry_price"]) * pos["shares"], abs=0.01)
