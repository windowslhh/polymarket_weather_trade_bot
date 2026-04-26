"""FIX-09: Executor must track in-flight trades and wait_until_idle must
respect a timeout.

Previously `main.py` called `scheduler.shutdown(wait=False)` and
closed the store — any trade mid-CLOB-post would get its Python state
torn down while the HTTP request kept flying.  Now Executor tracks
tasks and offers wait_until_idle so shutdown can drain before exit.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.executor import Executor


@pytest.mark.asyncio
async def test_wait_until_idle_returns_true_when_nothing_in_flight():
    executor = Executor(MagicMock(), MagicMock())
    assert await executor.wait_until_idle(timeout=1.0) is True


@pytest.mark.asyncio
async def test_wait_until_idle_waits_for_real_task():
    """Register a slow task directly and confirm wait_until_idle blocks
    until it completes."""
    executor = Executor(MagicMock(), MagicMock())

    done = asyncio.Event()

    async def _slow():
        await asyncio.sleep(0.2)
        done.set()

    task = asyncio.create_task(_slow())
    executor._in_flight.add(task)
    task.add_done_callback(executor._in_flight.discard)

    result = await executor.wait_until_idle(timeout=2.0)
    assert result is True
    assert done.is_set()


@pytest.mark.asyncio
async def test_wait_until_idle_returns_false_on_timeout():
    executor = Executor(MagicMock(), MagicMock())

    async def _forever():
        await asyncio.sleep(10)

    task = asyncio.create_task(_forever())
    executor._in_flight.add(task)
    task.add_done_callback(executor._in_flight.discard)

    result = await executor.wait_until_idle(timeout=0.05)
    assert result is False

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_execute_signals_registers_in_flight_during_run():
    """While _execute_one is awaiting CLOB, the task must show up in
    _in_flight; after it returns, it must be removed."""
    from src.markets.clob_client import OrderResult
    from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
    from datetime import date

    seen_count: list[int] = []

    async def _clob_slow(**kw):
        # Snapshot in-flight count while the executor is awaiting us.
        seen_count.append(len(executor._in_flight))
        return OrderResult(order_id="x", success=True)

    clob = MagicMock()
    clob._config = MagicMock(dry_run=False, paper=False)
    clob.place_limit_order = AsyncMock(side_effect=_clob_slow)

    portfolio = MagicMock()
    portfolio.record_fill_atomic = AsyncMock(return_value=1)
    portfolio.store = MagicMock()
    portfolio.store.insert_pending_order = AsyncMock(return_value=1)
    portfolio.store.mark_order_failed = AsyncMock()
    portfolio.store.finalize_sell_order = AsyncMock()

    executor = Executor(clob, portfolio)

    slot = TempSlot(
        token_id_yes="y", token_id_no="n", outcome_label="80°F",
        temp_lower_f=80.0, temp_upper_f=80.0, price_no=0.5,
    )
    event = WeatherMarketEvent(
        event_id="e", condition_id="c", city="NYC",
        market_date=date.today(), slots=[slot],
    )
    sig = TradeSignal(
        token_type=TokenType.NO, side=Side.BUY, slot=slot, event=event,
        expected_value=0.05, estimated_win_prob=0.8,
        suggested_size_usd=5.0, strategy="B", reason="t",
    )

    await executor.execute_signals([sig])

    # During execution, one task was in flight.
    assert seen_count == [1]
    # After return, the set is empty.
    assert not executor._in_flight
