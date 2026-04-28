"""Atomicity tests for the BUY path (FIX-03).

Covers:
1. Happy path — pending order transitions to 'filled' and a position is inserted
   with source_order_id pointing at the CLOB handle.
2. CLOB returns empty order_id (FIX-M4) — no position is inserted; pending row
   flips to 'failed' so the reconciler knows.
3. CLOB raises — pending row survives as 'failed' with the exception message
   captured; no position is inserted.
4. Crash between CLOB success and record_fill — the pending row stays
   discoverable for FIX-05's reconciler.
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.execution.executor import Executor
from src.markets.clob_client import OrderResult
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker


def _build_signal() -> TradeSignal:
    slot = TempSlot(
        token_id_yes="tok_yes",
        token_id_no="tok_no",
        outcome_label="80-81°F",
        temp_lower_f=80.0,
        temp_upper_f=81.0,
        price_no=0.45,
    )
    event = WeatherMarketEvent(
        event_id="ev1",
        condition_id="cond1",
        city="Chicago",
        market_date=date(2026, 4, 25),
        slots=[slot],
    )
    return TradeSignal(
        token_type=TokenType.NO,
        side=Side.BUY,
        slot=slot,
        event=event,
        expected_value=0.03,
        estimated_win_prob=0.72,
        suggested_size_usd=10.0,
        strategy="B",
        reason="test",
    )


async def _mk_store() -> tuple[Store, Path]:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    return store, tmp


@pytest.mark.asyncio
async def test_happy_path_records_order_and_position():
    store, _ = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = AsyncMock()
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="clob_abc123", success=True),
    )
    # 2026-04-28: executor now calls get_fill_summary after a matched BUY to
    # record actual match_price + fee.  Pin a None return so the test stays
    # focused on the orders/positions atomicity contract; the FillSummary
    # plumbing is exercised in test_clob_resilience.
    clob.get_fill_summary = AsyncMock(return_value=None)
    executor = Executor(clob, tracker)

    await executor.execute_signals([_build_signal()])

    async with store.db.execute("SELECT * FROM orders") as cur:
        orders = [dict(r) for r in await cur.fetchall()]
    async with store.db.execute("SELECT * FROM positions") as cur:
        positions = [dict(r) for r in await cur.fetchall()]

    assert len(orders) == 1
    assert orders[0]["status"] == "filled"
    assert orders[0]["order_id"] == "clob_abc123"
    assert orders[0]["idempotency_key"]

    assert len(positions) == 1
    assert positions[0]["source_order_id"] == "clob_abc123"
    assert positions[0]["status"] == "open"

    await store.close()


@pytest.mark.asyncio
async def test_empty_order_id_leaves_pending_then_failed():
    """FIX-M4: CLOB returns empty order_id → treat as failure, no position.

    Empty order_id means the CLOB call never returned a handle.  Since the
    wrapper now sends FAK orders (any unfilled remainder is killed by the
    server), there is no resting state for the executor to cancel — the
    A1 self-cancel logic was removed when we switched to FAK.
    """
    store, _ = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = AsyncMock()
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="", success=False, message="empty"),
    )
    clob.cancel_order = AsyncMock(return_value=True)
    executor = Executor(clob, tracker)

    await executor.execute_signals([_build_signal()])

    async with store.db.execute("SELECT * FROM orders") as cur:
        orders = [dict(r) for r in await cur.fetchall()]
    async with store.db.execute("SELECT COUNT(*) FROM positions") as cur:
        row = await cur.fetchone()

    assert len(orders) == 1
    assert orders[0]["status"] == "failed"
    assert orders[0]["failure_reason"] == "empty"
    assert row[0] == 0  # no position
    # FAK: no resting order can exist for either branch, so the executor
    # never calls cancel.  This also pins the absence of the old A1 logic
    # — a regression that re-introduced the self-cancel would set this to 1.
    assert clob.cancel_order.call_count == 0

    await store.close()


@pytest.mark.asyncio
async def test_unfilled_order_is_not_cancelled():
    """2026-04-28: with FAK orders, a non-success result means the server
    has already killed any unfilled remainder server-side.  The executor
    must NOT attempt its own cancel — the previous A1 cancel was correct
    only for the GTC-era 'unmatched-and-resting' failure mode (which FAK
    eliminates) and was actively cancelling our own legitimate orders
    when we briefly accepted resting maker behaviour."""
    store, _ = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = AsyncMock()
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(
            order_id="0xKILLED", success=False,
            message="order not filled (status=cancelled)",
        ),
    )
    clob.cancel_order = AsyncMock(return_value=True)
    executor = Executor(clob, tracker)

    await executor.execute_signals([_build_signal()])

    async with store.db.execute("SELECT * FROM orders") as cur:
        orders = [dict(r) for r in await cur.fetchall()]
    async with store.db.execute("SELECT COUNT(*) FROM positions") as cur:
        (pos_count,) = await cur.fetchone()

    # Order row marked failed (no positions row).
    assert len(orders) == 1
    assert orders[0]["status"] == "failed"
    assert "not filled" in orders[0]["failure_reason"].lower()
    assert pos_count == 0
    # FAK guarantees the server has already cleaned up; our cancel must
    # not fire.  This pin is load-bearing — it guards against a regression
    # that re-introduces the A1 self-cancel.
    assert clob.cancel_order.call_count == 0

    await store.close()


@pytest.mark.asyncio
async def test_clob_raises_marks_failed():
    """Unexpected CLOB exception marks order failed and re-raises."""
    store, _ = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = AsyncMock()
    clob.place_limit_order = AsyncMock(side_effect=RuntimeError("network"))
    executor = Executor(clob, tracker)

    # execute_signals catches the exception in its outer loop; we just need to
    # confirm the failure bookkeeping happened.
    await executor.execute_signals([_build_signal()])

    async with store.db.execute("SELECT status, failure_reason FROM orders") as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    async with store.db.execute("SELECT COUNT(*) FROM positions") as cur:
        row = await cur.fetchone()

    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "network" in rows[0]["failure_reason"]
    assert row[0] == 0

    await store.close()


@pytest.mark.asyncio
async def test_crash_between_clob_and_record_fill_leaves_pending():
    """Simulate a crash AFTER CLOB returned success but BEFORE record_fill_atomic.

    The orders row must remain in 'pending' status with the idempotency_key so
    FIX-05's reconciler can discover it on startup.
    """
    store, _ = await _mk_store()
    tracker = PortfolioTracker(store)

    # We swap in a tracker whose record_fill_atomic raises AFTER the CLOB call.
    class CrashTracker(PortfolioTracker):
        async def record_fill_atomic(self, *args, **kwargs):  # type: ignore[override]
            raise RuntimeError("simulated crash mid-fill")

    crash_tracker = CrashTracker(store)
    clob = AsyncMock()
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="clob_xyz", success=True),
    )
    # CrashTracker.record_fill_atomic raises before consuming match data,
    # but the executor still calls get_fill_summary on the success path —
    # mock it to None so the call resolves instead of returning an
    # un-coercible AsyncMock.
    clob.get_fill_summary = AsyncMock(return_value=None)
    executor = Executor(clob, crash_tracker)

    # execute_signals logs but swallows the exception per-signal.
    await executor.execute_signals([_build_signal()])

    async with store.db.execute(
        "SELECT status, idempotency_key, order_id FROM orders"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    async with store.db.execute("SELECT COUNT(*) FROM positions") as cur:
        row = await cur.fetchone()

    assert len(rows) == 1
    assert rows[0]["status"] == "pending", (
        "Order must remain pending so the reconciler can recover it"
    )
    assert rows[0]["idempotency_key"]
    assert row[0] == 0  # no position was persisted

    await store.close()


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_to_orders():
    """Review 🟡 #7: dry-run mode must not pollute the orders table.

    Pre-fix every dry-run cycle added a pending row that was immediately
    marked 'failed' — hundreds of junk rows per day, plus it broke the
    reconciler's "pending means orphan" invariant on the first live
    startup.
    """
    store, _ = await _mk_store()
    tracker = PortfolioTracker(store)

    # A ClobClient-like mock with a dry_run=True config.
    class _DryRunClob:
        _config = SimpleNamespace(dry_run=True, paper=False)
        async def place_limit_order(self, **kw):
            return OrderResult(order_id="dry_run", success=False, message="dry")

    executor = Executor(_DryRunClob(), tracker)  # type: ignore[arg-type]

    await executor.execute_signals([_build_signal()])

    async with store.db.execute("SELECT COUNT(*) FROM orders") as cur:
        (count,) = await cur.fetchone()
    async with store.db.execute("SELECT COUNT(*) FROM positions") as cur:
        (pos_count,) = await cur.fetchone()
    assert count == 0
    assert pos_count == 0
    await store.close()


@pytest.mark.asyncio
async def test_legacy_positions_get_legacy_source_order_id():
    """Existing positions inserted without source_order_id default to 'legacy'."""
    store, _ = await _mk_store()

    # Use the non-atomic record_fill path (simulates a pre-FIX-03 insert).
    tracker = PortfolioTracker(store)
    await tracker.record_fill(
        event_id="ev_old",
        token_id="tok_old",
        token_type=TokenType.NO,
        city="Chicago",
        slot_label="80-81°F",
        side="BUY",
        price=0.4,
        size_usd=5.0,
        strategy="B",
    )

    async with store.db.execute(
        "SELECT source_order_id FROM positions WHERE event_id = 'ev_old'"
    ) as cur:
        row = await cur.fetchone()
    assert row["source_order_id"] == "legacy"

    await store.close()


if __name__ == "__main__":
    asyncio.run(test_happy_path_records_order_and_position())
