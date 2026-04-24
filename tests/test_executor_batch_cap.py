"""FIX-M2: Executor must trim a BUY batch that would exceed the
max_total_exposure_usd cap even though each signal individually passed
the per-signal sizer.
"""
from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.executor import Executor
from src.markets.clob_client import OrderResult
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker


def _mk_signal(size_usd: float, token_suffix: str = "a"):
    slot = TempSlot(
        token_id_yes=f"y_{token_suffix}", token_id_no=f"n_{token_suffix}",
        outcome_label="80°F", temp_lower_f=80.0, temp_upper_f=80.0,
        price_no=0.5,
    )
    event = WeatherMarketEvent(
        event_id=f"ev_{token_suffix}", condition_id="c", city="NYC",
        market_date=date(2026, 4, 25), slots=[slot],
    )
    return TradeSignal(
        token_type=TokenType.NO, side=Side.BUY, slot=slot, event=event,
        expected_value=0.05, estimated_win_prob=0.7,
        suggested_size_usd=size_usd, strategy="B", reason="batch test",
    )


async def _mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    return store


@pytest.mark.asyncio
async def test_batch_trims_when_sum_exceeds_cap():
    """Three $50 BUYs where existing exposure = $800, cap = $1000.
    Only $200 of new exposure fits; second signal trims to 0, third to 0."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    clob = MagicMock()
    clob._config = SimpleNamespace(
        dry_run=False, paper=True,
        strategy=SimpleNamespace(max_total_exposure_usd=1000.0),
    )
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="ok", success=True),
    )

    # Seed $800 of existing exposure via direct insert.
    await store.insert_position(
        event_id="preload", token_id="pre", token_type="NO",
        city="NYC", slot_label="50°F", side="BUY",
        entry_price=0.5, size_usd=800.0, shares=1600,
        strategy="B", buy_reason="preload",
    )

    sigs = [_mk_signal(50.0, "a"), _mk_signal(200.0, "b"), _mk_signal(50.0, "c")]
    executor = Executor(clob, tracker)
    await executor.execute_signals(sigs)

    # Signal-a (50) fits, signal-b (200) DOES NOT fit because running +
    # b = 50+200 > 200 trim target → zeroed.  Signal-c (50) also
    # doesn't fit because running is already at 50 and 50+50 > 200?
    # Actually: trim_target = 1000 - 800 = 200. Walk: a=50 (running
    # 50), b=200 would push to 250>200 so zeroed, c=50 would push to
    # 100 which is <= 200 so kept. Final: a + c = 100 accepted.
    async with store.db.execute(
        "SELECT COUNT(*) FROM positions WHERE buy_reason = 'batch test'"
    ) as cur:
        (cnt,) = await cur.fetchone()
    # Two non-preload positions landed (a and c).
    assert cnt == 2
    await store.close()


@pytest.mark.asyncio
async def test_batch_under_cap_all_fire():
    """All signals comfortably under the cap → all land unchanged."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    clob = MagicMock()
    clob._config = SimpleNamespace(
        dry_run=False, paper=True,
        strategy=SimpleNamespace(max_total_exposure_usd=1000.0),
    )
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="ok", success=True),
    )

    sigs = [_mk_signal(20.0, "a"), _mk_signal(20.0, "b")]
    executor = Executor(clob, tracker)
    await executor.execute_signals(sigs)

    async with store.db.execute(
        "SELECT COUNT(*) FROM positions WHERE buy_reason = 'batch test'"
    ) as cur:
        (cnt,) = await cur.fetchone()
    assert cnt == 2
    await store.close()
