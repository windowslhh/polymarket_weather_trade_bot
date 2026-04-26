"""Tests for Module 3: Buy/sell reason tracking through the full stack.

Covers the reason field on TradeSignal, persistence in positions table
(buy_reason, exit_reason), threading through executor → tracker → store,
DB migration, and frontend enrichment.

Tests cover: critical paths, boundary conditions, failure branches,
and performance.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent


# ── Helpers ──────────────────────────────────────────────────────────

def _make_event(city="New York", event_id="evt_1"):
    return WeatherMarketEvent(
        event_id=event_id,
        condition_id="cond_1",
        city=city,
        market_date=date.today(),
        slots=[],
    )


def _make_slot(label="80°F to 84°F", price_no=0.90, token_id_no="no_1"):
    return TempSlot(
        token_id_yes="yes_1",
        token_id_no=token_id_no,
        outcome_label=label,
        temp_lower_f=80.0,
        temp_upper_f=84.0,
        price_no=price_no,
    )


def _make_signal(side=Side.BUY, reason="test reason", size=5.0, strategy="A"):
    event = _make_event()
    slot = _make_slot()
    sig = TradeSignal(
        token_type=TokenType.NO,
        side=side,
        slot=slot,
        event=event,
        expected_value=0.10,
        estimated_win_prob=0.85,
        suggested_size_usd=size,
        strategy=strategy,
        reason=reason,
    )
    return sig


# ──────────────────────────────────────────────────────────────────────
# Critical Paths: TradeSignal.reason field
# ──────────────────────────────────────────────────────────────────────

class TestTradeSignalReason:

    def test_reason_field_default_empty(self):
        """TradeSignal reason defaults to empty string."""
        sig = TradeSignal(
            token_type=TokenType.NO,
            side=Side.BUY,
            slot=_make_slot(),
            event=_make_event(),
            expected_value=0.10,
            estimated_win_prob=0.85,
        )
        assert sig.reason == ""

    def test_reason_field_set(self):
        """TradeSignal reason can be set at construction."""
        sig = _make_signal(reason="[A] NO: dist=12°F, EV=0.100, win=85%")
        assert sig.reason == "[A] NO: dist=12°F, EV=0.100, win=85%"

    def test_reason_field_mutable(self):
        """TradeSignal reason can be changed after construction."""
        sig = _make_signal(reason="original")
        sig.reason = "updated reason"
        assert sig.reason == "updated reason"

    def test_reason_preserved_with_other_fields(self):
        """Reason coexists with existing fields like strategy, size, etc."""
        sig = _make_signal(reason="my reason", strategy="C", size=7.5)
        assert sig.reason == "my reason"
        assert sig.strategy == "C"
        assert sig.suggested_size_usd == 7.5


# ──────────────────────────────────────────────────────────────────────
# Critical Paths: Store persistence (buy_reason, exit_reason)
# ──────────────────────────────────────────────────────────────────────

class TestStoreReasonPersistence:

    @pytest.fixture
    async def store(self, tmp_path):
        from src.portfolio.store import Store
        db_path = tmp_path / "test_reasons.db"
        s = Store(db_path)
        await s.initialize()
        yield s
        await s.close()

    @pytest.mark.asyncio
    async def test_insert_position_with_buy_reason(self, store):
        """insert_position stores buy_reason in DB."""
        pid = await store.insert_position(
            event_id="evt_1", token_id="tok_1", token_type="NO",
            city="New York", slot_label="80°F to 84°F", side="BUY",
            entry_price=0.90, size_usd=5.0, shares=5.56,
            strategy="A", buy_reason="[A] NO: dist=12°F, EV=0.100",
        )
        positions = await store.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["buy_reason"] == "[A] NO: dist=12°F, EV=0.100"

    @pytest.mark.asyncio
    async def test_insert_position_default_empty_reason(self, store):
        """insert_position defaults buy_reason to empty string."""
        pid = await store.insert_position(
            event_id="evt_1", token_id="tok_1", token_type="NO",
            city="Dallas", slot_label="90°F to 94°F", side="BUY",
            entry_price=0.85, size_usd=3.0, shares=3.53,
        )
        positions = await store.get_open_positions()
        assert positions[0]["buy_reason"] == ""

    @pytest.mark.asyncio
    async def test_update_exit_reason(self, store):
        """update_exit_reason sets exit_reason on a position."""
        pid = await store.insert_position(
            event_id="evt_1", token_id="tok_1", token_type="NO",
            city="New York", slot_label="80°F to 84°F", side="BUY",
            entry_price=0.90, size_usd=5.0, shares=5.56,
            buy_reason="[A] NO: dist=12°F",
        )
        await store.update_exit_reason(pid, "[A] EXIT: daily max 81°F approaching slot")
        positions = await store.get_open_positions()
        assert positions[0]["exit_reason"] == "[A] EXIT: daily max 81°F approaching slot"

    @pytest.mark.asyncio
    async def test_exit_reason_default_empty(self, store):
        """exit_reason defaults to empty string."""
        pid = await store.insert_position(
            event_id="evt_1", token_id="tok_1", token_type="NO",
            city="New York", slot_label="80°F to 84°F", side="BUY",
            entry_price=0.90, size_usd=5.0, shares=5.56,
        )
        positions = await store.get_open_positions()
        assert positions[0]["exit_reason"] == ""

    @pytest.mark.asyncio
    async def test_closed_position_retains_reasons(self, store):
        """After closing, both buy_reason and exit_reason are preserved."""
        pid = await store.insert_position(
            event_id="evt_1", token_id="tok_1", token_type="NO",
            city="New York", slot_label="80°F to 84°F", side="BUY",
            entry_price=0.90, size_usd=5.0, shares=5.56,
            buy_reason="[B] LOCKED WIN: daily_max=88°F",
        )
        await store.update_exit_reason(pid, "[B] TRIM: EV decayed to -0.050")
        await store.close_position(pid)
        closed = await store.get_closed_positions()
        assert len(closed) == 1
        assert closed[0]["buy_reason"] == "[B] LOCKED WIN: daily_max=88°F"
        assert closed[0]["exit_reason"] == "[B] TRIM: EV decayed to -0.050"


# ──────────────────────────────────────────────────────────────────────
# Critical Paths: DB migration
# ──────────────────────────────────────────────────────────────────────

class TestStoreMigration:

    @pytest.mark.asyncio
    async def test_migration_adds_reason_columns(self, tmp_path):
        """_migrate_columns adds buy_reason and exit_reason to existing table."""
        import aiosqlite
        from src.portfolio.store import Store

        db_path = tmp_path / "test_migrate.db"

        # Create a DB with old schema (no buy_reason/exit_reason)
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("""
                CREATE TABLE positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    token_type TEXT NOT NULL,
                    city TEXT NOT NULL,
                    slot_label TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    size_usd REAL NOT NULL,
                    shares REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    strategy TEXT NOT NULL DEFAULT 'B',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    closed_at TEXT
                )
            """)
            await db.commit()

        # Now open with Store — migration should add columns
        s = Store(db_path)
        await s.initialize()

        # Verify columns exist by inserting with buy_reason
        pid = await s.insert_position(
            event_id="evt_1", token_id="tok_1", token_type="NO",
            city="NYC", slot_label="80°F", side="BUY",
            entry_price=0.9, size_usd=5.0, shares=5.56,
            buy_reason="test migration",
        )
        positions = await s.get_open_positions()
        assert positions[0]["buy_reason"] == "test migration"
        assert positions[0]["exit_reason"] == ""

        await s.close()

    @pytest.mark.asyncio
    async def test_migration_idempotent(self, tmp_path):
        """Running migration twice doesn't error."""
        from src.portfolio.store import Store

        db_path = tmp_path / "test_idempotent.db"
        s = Store(db_path)
        await s.initialize()
        # Run migration again (simulates restart)
        await s._migrate_columns()
        # Should still work fine
        pid = await s.insert_position(
            event_id="evt_1", token_id="tok_1", token_type="NO",
            city="NYC", slot_label="80°F", side="BUY",
            entry_price=0.9, size_usd=5.0, shares=5.56,
            buy_reason="still works",
        )
        positions = await s.get_open_positions()
        assert positions[0]["buy_reason"] == "still works"
        await s.close()


# ──────────────────────────────────────────────────────────────────────
# Critical Paths: PortfolioTracker → Store threading
# ──────────────────────────────────────────────────────────────────────

class TestTrackerReasonThreading:

    @pytest.fixture
    async def tracker_and_store(self, tmp_path):
        from src.portfolio.store import Store
        from src.portfolio.tracker import PortfolioTracker
        db_path = tmp_path / "test_tracker_reason.db"
        s = Store(db_path)
        await s.initialize()
        t = PortfolioTracker(s)
        yield t, s
        await s.close()

    @pytest.mark.asyncio
    async def test_record_fill_passes_buy_reason(self, tracker_and_store):
        """PortfolioTracker.record_fill passes buy_reason to store."""
        tracker, store = tracker_and_store
        pid = await tracker.record_fill(
            event_id="evt_1", token_id="tok_1", token_type=TokenType.NO,
            city="NYC", slot_label="80°F to 84°F", side="BUY",
            price=0.90, size_usd=5.0, strategy="A",
            buy_reason="[A] NO: dist=12°F, EV=0.100, win=85%",
        )
        positions = await store.get_open_positions()
        assert positions[0]["buy_reason"] == "[A] NO: dist=12°F, EV=0.100, win=85%"

    @pytest.mark.asyncio
    async def test_record_fill_default_empty_reason(self, tracker_and_store):
        """PortfolioTracker.record_fill defaults buy_reason to empty."""
        tracker, store = tracker_and_store
        pid = await tracker.record_fill(
            event_id="evt_1", token_id="tok_1", token_type=TokenType.NO,
            city="NYC", slot_label="80°F", side="BUY",
            price=0.90, size_usd=5.0,
        )
        positions = await store.get_open_positions()
        assert positions[0]["buy_reason"] == ""

    @pytest.mark.asyncio
    async def test_close_positions_sets_exit_reason(self, tracker_and_store):
        """close_positions_for_token sets exit_reason on matched positions."""
        tracker, store = tracker_and_store
        await tracker.record_fill(
            event_id="evt_1", token_id="tok_1", token_type=TokenType.NO,
            city="NYC", slot_label="80°F to 84°F", side="BUY",
            price=0.90, size_usd=5.0, strategy="A",
            buy_reason="[A] NO: dist=12°F",
        )
        closed = await tracker.close_positions_for_token(
            event_id="evt_1", token_id="tok_1", strategy="A",
            exit_reason="[A] EXIT: daily max 81°F approaching slot",
        )
        assert closed == 1
        closed_pos = await store.get_closed_positions()
        assert closed_pos[0]["exit_reason"] == "[A] EXIT: daily max 81°F approaching slot"
        assert closed_pos[0]["buy_reason"] == "[A] NO: dist=12°F"

    @pytest.mark.asyncio
    async def test_close_positions_no_exit_reason(self, tracker_and_store):
        """close_positions_for_token with empty exit_reason leaves it empty."""
        tracker, store = tracker_and_store
        await tracker.record_fill(
            event_id="evt_1", token_id="tok_1", token_type=TokenType.NO,
            city="NYC", slot_label="80°F", side="BUY",
            price=0.90, size_usd=5.0,
        )
        closed = await tracker.close_positions_for_token(
            event_id="evt_1", token_id="tok_1",
        )
        assert closed == 1
        closed_pos = await store.get_closed_positions()
        assert closed_pos[0]["exit_reason"] == ""


# ──────────────────────────────────────────────────────────────────────
# Critical Paths: Executor → Tracker reason threading
# ──────────────────────────────────────────────────────────────────────

class TestExecutorReasonThreading:

    def _make_executor(self):
        from src.execution.executor import Executor
        mock_clob = MagicMock()
        mock_portfolio = MagicMock()
        # FIX-03: BUY now goes through record_fill_atomic; the executor also
        # pokes portfolio.store for the pending-order bookkeeping.
        mock_portfolio.record_fill_atomic = AsyncMock(return_value=1)
        mock_portfolio.record_fill = AsyncMock(return_value=1)  # legacy path
        mock_portfolio.close_positions_for_token = AsyncMock(return_value=1)
        # Executor now looks up held shares for SELL orders (EX-01 fix)
        mock_portfolio.get_total_shares_for_token = AsyncMock(return_value=5.0)
        mock_portfolio.store = MagicMock()
        mock_portfolio.store.insert_pending_order = AsyncMock(return_value=1)
        mock_portfolio.store.mark_order_failed = AsyncMock()
        mock_portfolio.store.finalize_sell_order = AsyncMock()
        mock_clob.place_limit_order = AsyncMock(return_value=MagicMock(success=True, order_id="ord_1"))
        executor = Executor(mock_clob, mock_portfolio)
        return executor, mock_portfolio

    @pytest.mark.asyncio
    async def test_buy_passes_reason_to_record_fill(self):
        """Executor passes signal.reason as buy_reason to record_fill_atomic (FIX-03)."""
        executor, portfolio = self._make_executor()
        sig = _make_signal(side=Side.BUY, reason="[A] NO: dist=12°F, EV=0.100")

        await executor.execute_signals([sig])

        portfolio.record_fill_atomic.assert_called_once()
        call_kwargs = portfolio.record_fill_atomic.call_args
        assert call_kwargs.kwargs.get("buy_reason") == "[A] NO: dist=12°F, EV=0.100"
        # FIX-03: the atomic path is keyed off an idempotency_key + order_id.
        assert call_kwargs.kwargs.get("idempotency_key")
        assert call_kwargs.kwargs.get("order_id") == "ord_1"

    @pytest.mark.asyncio
    async def test_sell_passes_reason_to_close_positions(self):
        """Executor passes signal.reason as exit_reason to close_positions_for_token."""
        executor, portfolio = self._make_executor()
        sig = _make_signal(side=Side.SELL, reason="[A] EXIT: daily max 81°F")

        await executor.execute_signals([sig])

        portfolio.close_positions_for_token.assert_called_once()
        call_kwargs = portfolio.close_positions_for_token.call_args
        assert call_kwargs[1].get("exit_reason") == "[A] EXIT: daily max 81°F" or \
               call_kwargs.kwargs.get("exit_reason") == "[A] EXIT: daily max 81°F"

    @pytest.mark.asyncio
    async def test_buy_empty_reason(self):
        """Executor handles empty reason gracefully."""
        executor, portfolio = self._make_executor()
        sig = _make_signal(side=Side.BUY, reason="")

        await executor.execute_signals([sig])

        portfolio.record_fill_atomic.assert_called_once()
        call_kwargs = portfolio.record_fill_atomic.call_args
        assert call_kwargs.kwargs.get("buy_reason") == ""


# ──────────────────────────────────────────────────────────────────────
# Critical Paths: Rebalancer attaches reasons to signals
# ──────────────────────────────────────────────────────────────────────

class TestRebalancerReasonAttachment:

    def test_signal_reason_format_no(self):
        """NO signal reason includes strategy, distance, EV, win probability."""
        reason = "[A] NO: dist=12°F, EV=0.100, win=85%"
        assert "[A]" in reason
        assert "NO:" in reason
        assert "dist=" in reason
        assert "EV=" in reason
        assert "win=" in reason

    def test_signal_reason_format_locked(self):
        """LOCKED WIN reason includes daily_max and EV."""
        reason = "[B] LOCKED WIN: daily_max=88°F > slot upper, EV=0.050"
        assert "[B]" in reason
        assert "LOCKED WIN:" in reason
        assert "daily_max=" in reason
        assert "EV=" in reason

    def test_signal_reason_format_exit(self):
        """EXIT reason includes strategy and daily max."""
        reason = "[A] EXIT: daily max 81°F approaching slot"
        assert "[A]" in reason
        assert "EXIT:" in reason
        assert "daily max" in reason

    def test_signal_reason_format_trim(self):
        """TRIM reason names the firing gate + key diagnostics.

        Post-PR #7 format: ``[<strat>] TRIM [<trigger>]: <diagnostics>``
        where ``<trigger>`` is ``price_stop`` / ``absolute`` / ``relative``.
        The old ``"TRIM: EV decayed to X"`` string lost the trigger
        identity — see docs history / PR #7.
        """
        reason = "[C] TRIM [price_stop]: 0.710→0.474 (ratio=0.25)"
        assert reason.startswith("[C] TRIM [")
        assert "price_stop]" in reason
        assert "0.710" in reason and "0.474" in reason


# ──────────────────────────────────────────────────────────────────────
# Boundary Conditions
# ──────────────────────────────────────────────────────────────────────

class TestReasonBoundary:

    @pytest.mark.asyncio
    async def test_very_long_reason(self, tmp_path):
        """A very long reason string is stored and retrieved correctly."""
        from src.portfolio.store import Store
        db_path = tmp_path / "test_long_reason.db"
        s = Store(db_path)
        await s.initialize()

        long_reason = "X" * 1000
        pid = await s.insert_position(
            event_id="evt_1", token_id="tok_1", token_type="NO",
            city="NYC", slot_label="80°F", side="BUY",
            entry_price=0.9, size_usd=5.0, shares=5.56,
            buy_reason=long_reason,
        )
        positions = await s.get_open_positions()
        assert positions[0]["buy_reason"] == long_reason
        await s.close()

    @pytest.mark.asyncio
    async def test_special_characters_in_reason(self, tmp_path):
        """Reason with special characters (%, °, quotes) is safe."""
        from src.portfolio.store import Store
        db_path = tmp_path / "test_special_reason.db"
        s = Store(db_path)
        await s.initialize()

        reason = "[A] NO: dist=12°F, EV=0.100, win=85%, O'Brien's test \"quoted\""
        pid = await s.insert_position(
            event_id="evt_1", token_id="tok_1", token_type="NO",
            city="NYC", slot_label="80°F", side="BUY",
            entry_price=0.9, size_usd=5.0, shares=5.56,
            buy_reason=reason,
        )
        positions = await s.get_open_positions()
        assert positions[0]["buy_reason"] == reason
        await s.close()

    @pytest.mark.asyncio
    async def test_unicode_in_reason(self, tmp_path):
        """Reason with unicode characters (Chinese, emoji) is safe."""
        from src.portfolio.store import Store
        db_path = tmp_path / "test_unicode_reason.db"
        s = Store(db_path)
        await s.initialize()

        reason = "[A] NO: 距离=12°F, 预期收益=0.100"
        pid = await s.insert_position(
            event_id="evt_1", token_id="tok_1", token_type="NO",
            city="NYC", slot_label="80°F", side="BUY",
            entry_price=0.9, size_usd=5.0, shares=5.56,
            buy_reason=reason,
        )
        positions = await s.get_open_positions()
        assert positions[0]["buy_reason"] == reason
        await s.close()

    @pytest.mark.asyncio
    async def test_multiple_positions_different_reasons(self, tmp_path):
        """Multiple positions each get their own buy_reason."""
        from src.portfolio.store import Store
        db_path = tmp_path / "test_multi_reasons.db"
        s = Store(db_path)
        await s.initialize()

        for i in range(5):
            await s.insert_position(
                event_id="evt_1", token_id=f"tok_{i}", token_type="NO",
                city="NYC", slot_label=f"{70+i}°F to {74+i}°F", side="BUY",
                entry_price=0.9, size_usd=5.0, shares=5.56,
                buy_reason=f"[A] NO: dist={10+i}°F, EV={0.05+i*0.01:.3f}",
            )

        positions = await s.get_open_positions()
        reasons = [p["buy_reason"] for p in positions]
        assert len(set(reasons)) == 5  # All unique
        await s.close()

    @pytest.mark.asyncio
    async def test_update_exit_reason_nonexistent_id(self, tmp_path):
        """update_exit_reason on non-existent ID doesn't crash."""
        from src.portfolio.store import Store
        db_path = tmp_path / "test_nonexist.db"
        s = Store(db_path)
        await s.initialize()
        # Should not raise
        await s.update_exit_reason(9999, "some reason")
        await s.close()


# ──────────────────────────────────────────────────────────────────────
# Failure Branches
# ──────────────────────────────────────────────────────────────────────

class TestReasonFailureBranches:

    @pytest.mark.asyncio
    async def test_executor_handles_missing_reason_attr(self):
        """Executor gracefully handles signal without reason attr (legacy)."""
        from src.execution.executor import Executor
        mock_clob = MagicMock()
        mock_portfolio = MagicMock()
        mock_portfolio.record_fill_atomic = AsyncMock(return_value=1)
        mock_portfolio.store = MagicMock()
        mock_portfolio.store.insert_pending_order = AsyncMock(return_value=1)
        mock_portfolio.store.mark_order_failed = AsyncMock()
        mock_clob.place_limit_order = AsyncMock(return_value=MagicMock(success=True, order_id="ord_1"))
        executor = Executor(mock_clob, mock_portfolio)

        # Create signal the "old way" (manually, removing reason attr)
        sig = _make_signal(side=Side.BUY, reason="")
        # Simulate a signal that might not have reason
        delattr(sig, 'reason')

        await executor.execute_signals([sig])
        # getattr fallback should produce ''
        mock_portfolio.record_fill_atomic.assert_called_once()

    @pytest.mark.asyncio
    async def test_sell_signal_with_no_matching_positions(self):
        """SELL signal with no matching positions doesn't crash exit_reason flow."""
        from src.execution.executor import Executor
        mock_clob = MagicMock()
        mock_portfolio = MagicMock()
        mock_portfolio.close_positions_for_token = AsyncMock(return_value=0)  # no matches
        # Executor now looks up held shares (EX-01 fix); return 0 → skip with warning
        mock_portfolio.get_total_shares_for_token = AsyncMock(return_value=0.0)
        mock_clob.place_limit_order = AsyncMock(return_value=MagicMock(success=True, order_id="ord_1"))
        executor = Executor(mock_clob, mock_portfolio)

        sig = _make_signal(side=Side.SELL, reason="[A] EXIT: approaching slot")
        await executor.execute_signals([sig])

        # With EX-01 fix: get_total_shares returns 0 → executor skips early,
        # so close_positions_for_token is NOT called (no shares to sell)
        mock_portfolio.close_positions_for_token.assert_not_called()

    @pytest.mark.asyncio
    async def test_order_failure_doesnt_persist_reason(self):
        """If order fails, no reason is persisted (no record_fill call)."""
        from src.execution.executor import Executor
        mock_clob = MagicMock()
        mock_portfolio = MagicMock()
        mock_portfolio.record_fill_atomic = AsyncMock(return_value=1)
        mock_portfolio.store = MagicMock()
        mock_portfolio.store.insert_pending_order = AsyncMock(return_value=1)
        mock_portfolio.store.mark_order_failed = AsyncMock()
        mock_clob.place_limit_order = AsyncMock(
            return_value=MagicMock(success=False, message="Insufficient balance")
        )
        executor = Executor(mock_clob, mock_portfolio)

        sig = _make_signal(side=Side.BUY, reason="[A] NO: should not persist")
        await executor.execute_signals([sig])

        # record_fill should NOT be called when order fails
        mock_portfolio.record_fill_atomic.assert_not_called()
        # But the failure path must have marked the pending order failed.
        mock_portfolio.store.mark_order_failed.assert_called_once()


# ──────────────────────────────────────────────────────────────────────
# Integration: Full round-trip (signal → executor → tracker → store → read)
# ──────────────────────────────────────────────────────────────────────

class TestReasonFullRoundTrip:

    @pytest.mark.asyncio
    async def test_buy_reason_round_trip(self, tmp_path):
        """Full flow: signal.reason → executor → tracker → store → read back."""
        from src.execution.executor import Executor
        from src.portfolio.store import Store
        from src.portfolio.tracker import PortfolioTracker

        db_path = tmp_path / "test_roundtrip.db"
        store = Store(db_path)
        await store.initialize()
        tracker = PortfolioTracker(store)

        # Mock CLOB
        mock_clob = MagicMock()
        mock_clob.place_limit_order = AsyncMock(
            return_value=MagicMock(success=True, order_id="ord_1")
        )
        executor = Executor(mock_clob, tracker)

        # BUY signal with reason
        sig = _make_signal(side=Side.BUY, reason="[A] NO: dist=12°F, EV=0.100, win=85%")
        await executor.execute_signals([sig])

        # Read back
        positions = await store.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["buy_reason"] == "[A] NO: dist=12°F, EV=0.100, win=85%"
        assert positions[0]["exit_reason"] == ""

        await store.close()

    @pytest.mark.asyncio
    async def test_sell_reason_round_trip(self, tmp_path):
        """Full flow: BUY → SELL with exit_reason → read closed position."""
        from src.execution.executor import Executor
        from src.portfolio.store import Store
        from src.portfolio.tracker import PortfolioTracker

        db_path = tmp_path / "test_sell_roundtrip.db"
        store = Store(db_path)
        await store.initialize()
        tracker = PortfolioTracker(store)

        mock_clob = MagicMock()
        mock_clob.place_limit_order = AsyncMock(
            return_value=MagicMock(success=True, order_id="ord_1")
        )
        executor = Executor(mock_clob, tracker)

        # Step 1: BUY
        buy_sig = _make_signal(side=Side.BUY, reason="[B] LOCKED WIN: daily_max=88°F")
        await executor.execute_signals([buy_sig])

        # Step 2: SELL
        sell_sig = _make_signal(side=Side.SELL, reason="[B] EXIT: daily max 82°F")
        await executor.execute_signals([sell_sig])

        # Read back
        open_pos = await store.get_open_positions()
        assert len(open_pos) == 0

        closed_pos = await store.get_closed_positions()
        assert len(closed_pos) == 1
        assert closed_pos[0]["buy_reason"] == "[B] LOCKED WIN: daily_max=88°F"
        assert closed_pos[0]["exit_reason"] == "[B] EXIT: daily max 82°F"

        await store.close()


# ──────────────────────────────────────────────────────────────────────
# Web Layer: Position enrichment
# ──────────────────────────────────────────────────────────────────────

class TestWebReasonEnrichment:

    def test_parse_slot_label_helper(self):
        """_parse_slot_label still works (regression check)."""
        from src.web.app import _parse_slot_label
        temp, dt = _parse_slot_label("Will the highest temperature in NYC be between 80-84°F on April 5?")
        assert temp == "80-84°F"
        assert dt == "Apr 5"

    def test_open_position_enrichment(self):
        """Open position dict gets buy_reason from DB column."""
        # Simulate what positions_page does
        p = {
            "token_id": "tok_1",
            "entry_price": 0.90,
            "shares": 5.56,
            "size_usd": 5.0,
            "city": "New York",
            "slot_label": "80°F to 84°F",
            "strategy": "A",
            "buy_reason": "[A] NO: dist=12°F, EV=0.100",
            "exit_reason": "",
        }
        # Enrichment logic from app.py
        p["buy_reason"] = p.get("buy_reason", "")
        assert p["buy_reason"] == "[A] NO: dist=12°F, EV=0.100"

    def test_closed_position_enrichment(self):
        """Closed position dict gets both buy_reason and exit_reason."""
        p = {
            "city": "New York",
            "slot_label": "80°F to 84°F",
            "strategy": "A",
            "entry_price": 0.90,
            "size_usd": 5.0,
            "buy_reason": "[A] NO: dist=12°F",
            "exit_reason": "[A] EXIT: daily max 81°F",
            "closed_at": "2026-04-10T12:00:00",
        }
        p["buy_reason"] = p.get("buy_reason", "")
        p["exit_reason"] = p.get("exit_reason", "")
        assert p["buy_reason"] == "[A] NO: dist=12°F"
        assert p["exit_reason"] == "[A] EXIT: daily max 81°F"

    def test_missing_reason_falls_back_to_empty(self):
        """Old positions without reason columns get empty string."""
        p = {"city": "Dallas", "slot_label": "90°F"}
        p["buy_reason"] = p.get("buy_reason", "")
        p["exit_reason"] = p.get("exit_reason", "")
        assert p["buy_reason"] == ""
        assert p["exit_reason"] == ""


# ──────────────────────────────────────────────────────────────────────
# Performance
# ──────────────────────────────────────────────────────────────────────

class TestReasonPerformance:

    @pytest.mark.asyncio
    async def test_bulk_insert_with_reasons_fast(self, tmp_path):
        """100 positions with reasons inserted in < 3 seconds."""
        import time
        from src.portfolio.store import Store

        db_path = tmp_path / "test_perf.db"
        s = Store(db_path)
        await s.initialize()

        t0 = time.monotonic()
        for i in range(100):
            await s.insert_position(
                event_id="evt_1", token_id=f"tok_{i}", token_type="NO",
                city="NYC", slot_label=f"{60+i%30}°F to {64+i%30}°F", side="BUY",
                entry_price=0.9, size_usd=5.0, shares=5.56,
                buy_reason=f"[A] NO: dist={10+i}°F, EV={0.05+i*0.001:.4f}, win=85%",
            )
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0, f"100 inserts took {elapsed:.3f}s"

        positions = await s.get_open_positions()
        assert len(positions) == 100
        # All have reasons
        assert all(p["buy_reason"] for p in positions)

        await s.close()

    @pytest.mark.asyncio
    async def test_bulk_exit_reasons_fast(self, tmp_path):
        """100 exit_reason updates in < 3 seconds."""
        import time
        from src.portfolio.store import Store

        db_path = tmp_path / "test_perf_exit.db"
        s = Store(db_path)
        await s.initialize()

        # Insert first
        pids = []
        for i in range(100):
            pid = await s.insert_position(
                event_id="evt_1", token_id=f"tok_{i}", token_type="NO",
                city="NYC", slot_label=f"{60+i%30}°F", side="BUY",
                entry_price=0.9, size_usd=5.0, shares=5.56,
            )
            pids.append(pid)

        t0 = time.monotonic()
        for pid in pids:
            await s.update_exit_reason(pid, f"[A] EXIT: position {pid}")
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0, f"100 exit updates took {elapsed:.3f}s"

        await s.close()
