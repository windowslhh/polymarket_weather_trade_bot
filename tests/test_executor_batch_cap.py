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


# ──────────────────────────────────────────────────────────────────────
# C-2: no input mutation + decision_log REJECT + explicit config injection
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_cap_does_not_mutate_input_signals():
    """C-2: pre-fix the executor zeroed `s.suggested_size_usd` on the
    caller's signal objects.  That broke audit / replay (you couldn't
    re-feed the same signals to retry a batch).  Post-fix the signals
    are untouched; trimming happens via a parallel skip set."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    config = SimpleNamespace(
        dry_run=False, paper=True,
        strategy=SimpleNamespace(max_total_exposure_usd=100.0),
    )
    clob = MagicMock()
    clob._config = None  # C-2: executor must use injected config, not _clob._config
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="ok", success=True),
    )

    sigs = [_mk_signal(50.0, "a"), _mk_signal(60.0, "b"), _mk_signal(50.0, "c")]
    original_sizes = [s.suggested_size_usd for s in sigs]

    executor = Executor(clob, tracker, config=config)
    await executor.execute_signals(sigs)

    # All three signals' suggested_size_usd retained their ORIGINAL values
    # — no mutation happened during trimming.
    for sig, orig in zip(sigs, original_sizes):
        assert sig.suggested_size_usd == orig, (
            f"C-2: input signal mutated! suggested_size_usd "
            f"{orig} → {sig.suggested_size_usd}"
        )
    await store.close()


@pytest.mark.asyncio
async def test_batch_cap_writes_BATCH_CAP_EXCEEDED_decision_log():
    """C-2: every trimmed signal must produce a decision_log row with
    reason starting BATCH_CAP_EXCEEDED so the dashboard can show
    'why didn't this trade fire?'."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    config = SimpleNamespace(
        dry_run=False, paper=True,
        strategy=SimpleNamespace(max_total_exposure_usd=100.0),
    )
    clob = MagicMock()
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="ok", success=True),
    )

    # trim_target = 100; a=50 keeps, b=60 trims (50+60>100), c=50 keeps (50+50<=100).
    sigs = [_mk_signal(50.0, "a"), _mk_signal(60.0, "b"), _mk_signal(50.0, "c")]
    executor = Executor(clob, tracker, config=config)
    await executor.execute_signals(sigs)

    async with store.db.execute(
        "SELECT reason, signal_type, action FROM decision_log "
        "WHERE reason LIKE '%BATCH_CAP_EXCEEDED%'"
    ) as cur:
        rows = await cur.fetchall()
    # Exactly one trimmed signal (b) → exactly one decision_log row
    assert len(rows) == 1
    assert rows[0][0].startswith("[B] REJECT: BATCH_CAP_EXCEEDED")
    assert rows[0][1] == "REJECT"
    assert rows[0][2] == "SKIP"
    await store.close()


@pytest.mark.asyncio
async def test_executor_uses_injected_config_not_clob_internal():
    """C-2: when an explicit config is injected, the executor must NOT
    fall back to self._clob._config.  Pin by attaching a *different*
    cap to clob._config and verifying the injected one wins."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    injected_config = SimpleNamespace(
        dry_run=False, paper=True,
        strategy=SimpleNamespace(max_total_exposure_usd=100.0),  # tight cap
    )
    clob = MagicMock()
    # If executor reads the WRONG source, it'd see this huge cap and
    # let everything through.
    clob._config = SimpleNamespace(
        dry_run=False, paper=True,
        strategy=SimpleNamespace(max_total_exposure_usd=1_000_000.0),
    )
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="ok", success=True),
    )

    sigs = [_mk_signal(80.0, "a"), _mk_signal(80.0, "b")]
    executor = Executor(clob, tracker, config=injected_config)
    await executor.execute_signals(sigs)

    # If executor used injected $100 cap correctly, only one signal lands.
    # If it leaked to clob._config's $1M cap, both would land.
    async with store.db.execute(
        "SELECT COUNT(*) FROM positions WHERE buy_reason = 'batch test'"
    ) as cur:
        (cnt,) = await cur.fetchone()
    assert cnt == 1, (
        "C-2: executor must prefer injected config over clob._config; "
        f"got {cnt} positions (expected 1 under $100 cap)"
    )
    await store.close()
