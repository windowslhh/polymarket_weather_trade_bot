"""Plan A α (2026-04-30): SELL late-fill probe.

Polymarket's FAK matcher returns ``200 status=delayed`` for SELL orders
that hit a quoted-but-not-crossing book — ``ClobClient.place_limit_order``
flags this as ``success=False`` immediately, but the server continues to
match in an async window and the trade may still land on chain.  Without
the probe, the executor marks the order failed, the position stays open
in DB, and the next cycle sees ``balance=0`` from the on-chain clamp
(Bug C) — silently leaking realized P&L (audit on 2026-04-30 found 3
SELLs ghost-filled, $4.46 unrecorded).

These tests pin:
- Probe runs only on SELL ``status=delayed`` (not on deterministic skips).
- A late fill recovers via the same ``finalize_sell_order`` +
  ``close_positions_for_token`` path the success branch uses, with
  ``exit_price`` from the actual ``/data/trades`` match price.
- A real kill (probe exhausts attempts) marks the order failed and
  persists the CLOB-returned order id.
- The BUY path is untouched.
"""
from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.execution import executor as executor_mod
from src.execution.executor import Executor
from src.markets.clob_client import FillSummary, OrderResult
from src.markets.models import (
    Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent,
)
from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _mk_store():
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    return store


def _mk_clob_mock(*, paper: bool = False, dry_run: bool = False) -> AsyncMock:
    """Async-mock ClobClient with the shape executor reads off it."""
    clob = AsyncMock()
    clob._config = SimpleNamespace(
        dry_run=dry_run, paper=paper, strategy=None,
    )
    clob.get_conditional_balance = AsyncMock(return_value=10_000_000)  # 10 sh
    return clob


def _build_sell_signal(token_id: str, price_no: float = 0.45) -> TradeSignal:
    slot = TempSlot(
        token_id_yes="yes_z", token_id_no=token_id,
        outcome_label="80-81°F",
        temp_lower_f=80.0, temp_upper_f=81.0,
        price_no=price_no,
    )
    event = WeatherMarketEvent(
        event_id="ev_sell", condition_id="cs", city="Chicago",
        market_date=date(2026, 4, 28), slots=[slot],
    )
    return TradeSignal(
        token_type=TokenType.NO, side=Side.SELL, slot=slot, event=event,
        expected_value=-0.10, estimated_win_prob=0.30,
        suggested_size_usd=0.0, strategy="B", reason="exit_test",
    )


def _build_buy_signal(token_id: str, price_no: float = 0.45) -> TradeSignal:
    slot = TempSlot(
        token_id_yes="yes_z", token_id_no=token_id,
        outcome_label="80-81°F",
        temp_lower_f=80.0, temp_upper_f=81.0,
        price_no=price_no,
    )
    event = WeatherMarketEvent(
        event_id="ev_buy", condition_id="cs", city="Chicago",
        market_date=date(2026, 4, 28), slots=[slot],
    )
    return TradeSignal(
        token_type=TokenType.NO, side=Side.BUY, slot=slot, event=event,
        expected_value=0.05, estimated_win_prob=0.72,
        suggested_size_usd=10.0, strategy="B", reason="entry_test",
    )


async def _seed_open_position(store, token_id: str, shares: float = 10.0):
    await store.insert_position(
        event_id="ev_sell", token_id=token_id, token_type="NO", city="Chicago",
        slot_label="80-81°F", side="BUY", entry_price=0.30, size_usd=3.0,
        shares=shares, strategy="B", buy_reason="seed",
    )


@pytest.fixture
def fast_sleep(monkeypatch):
    """Skip the 10s probe backoff in tests so the suite stays fast."""
    async def _noop(*_args, **_kwargs):
        return None
    monkeypatch.setattr(executor_mod.asyncio, "sleep", _noop)


# ---------------------------------------------------------------------------
# 1. Late-fill recovery: probe finds a fill on attempt 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_delayed_then_filled_via_probe(fast_sleep):
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = _mk_clob_mock()
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(
            order_id="0xabc", success=False,
            message="order not filled (status=delayed)",
        ),
    )
    # Attempt 0 (immediate) returns None; attempt 1 (after 10s sleep)
    # finds the trade.  Mirrors the production timing where /data/trades
    # propagation lags by a few seconds.
    clob.get_fill_summary = AsyncMock(side_effect=[
        None,
        FillSummary(shares=5.0, match_price=0.45,
                    fee_paid_usd=0.05, net_shares=4.95),
    ])

    await _seed_open_position(store, "tok_late", shares=5.0)
    executor = Executor(clob, tracker)
    await executor.execute_signals([_build_sell_signal("tok_late", 0.45)])

    # Order should be marked filled (not failed) with the real CLOB id.
    async with store.db.execute(
        "SELECT status, order_id, side FROM orders"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1
    assert rows[0]["status"] == "filled"
    assert rows[0]["order_id"] == "0xabc"
    assert rows[0]["side"] == "SELL"

    # Position should be closed with realized P&L computed from the
    # actual match price (0.45), not the mid (also 0.45 here, but the
    # plumbing is the bit being tested).
    async with store.db.execute(
        "SELECT status, exit_price FROM positions"
    ) as cur:
        positions = [dict(r) for r in await cur.fetchall()]
    assert positions[0]["status"] == "closed"
    assert positions[0]["exit_price"] == 0.45

    # Two probe attempts before the match.
    assert clob.get_fill_summary.call_count == 2
    await store.close()


# ---------------------------------------------------------------------------
# 2. Real kill: probe exhausts, order persists as failed with real id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_delayed_then_killed_via_probe(fast_sleep):
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = _mk_clob_mock()
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(
            order_id="0xdef", success=False,
            message="order not filled (status=delayed)",
        ),
    )
    # All three probe attempts return None — server async-killed the order.
    clob.get_fill_summary = AsyncMock(return_value=None)

    await _seed_open_position(store, "tok_killed", shares=5.0)
    executor = Executor(clob, tracker)
    await executor.execute_signals([_build_sell_signal("tok_killed", 0.45)])

    # Order is failed (not filled), with the real CLOB order_id persisted.
    async with store.db.execute(
        "SELECT status, order_id, failure_reason FROM orders"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows[0]["status"] == "failed"
    assert rows[0]["order_id"] == "0xdef"
    assert "delayed" in rows[0]["failure_reason"]

    # Position stays open — no close on a real kill.
    async with store.db.execute(
        "SELECT status FROM positions"
    ) as cur:
        positions = [dict(r) for r in await cur.fetchall()]
    assert positions[0]["status"] == "open"

    # All three attempts ran.
    assert clob.get_fill_summary.call_count == 3
    await store.close()


# ---------------------------------------------------------------------------
# 3-5. Probe is skipped for deterministic failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_thin_liquidity_does_not_probe(fast_sleep):
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = _mk_clob_mock()
    # THIN_LIQUIDITY_NO_BID has empty order_id (the order was never
    # submitted), and its message has no "delayed" — both gates skip
    # the probe.
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(
            order_id="", success=False, message="THIN_LIQUIDITY_NO_BID",
        ),
    )
    clob.get_fill_summary = AsyncMock()

    await _seed_open_position(store, "tok_thin", shares=5.0)
    executor = Executor(clob, tracker)
    await executor.execute_signals([_build_sell_signal("tok_thin", 0.45)])

    assert clob.get_fill_summary.call_count == 0
    async with store.db.execute(
        "SELECT status FROM orders"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows[0]["status"] == "failed"
    await store.close()


@pytest.mark.asyncio
async def test_sell_slippage_does_not_probe(fast_sleep):
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = _mk_clob_mock()
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(
            order_id="", success=False,
            message="SLIPPAGE_TOO_HIGH bid=0.0010 mid=0.4500",
        ),
    )
    clob.get_fill_summary = AsyncMock()

    await _seed_open_position(store, "tok_slip", shares=5.0)
    executor = Executor(clob, tracker)
    await executor.execute_signals([_build_sell_signal("tok_slip", 0.45)])

    assert clob.get_fill_summary.call_count == 0
    await store.close()


@pytest.mark.asyncio
async def test_sell_price_too_low_does_not_probe(fast_sleep):
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = _mk_clob_mock()
    # Cold-start guard fires before the wrapper even checks the book —
    # no order_id, no chance of a fill, no reason to probe.
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(
            order_id="", success=False, message="PRICE_TOO_LOW_FAK_GUARD",
        ),
    )
    clob.get_fill_summary = AsyncMock()

    await _seed_open_position(store, "tok_zero", shares=5.0)
    executor = Executor(clob, tracker)
    # Caller passes mid=0.45 (above tick) but place_limit_order is mocked
    # so the actual price doesn't matter — the message branch is what's
    # under test.
    await executor.execute_signals([_build_sell_signal("tok_zero", 0.45)])

    assert clob.get_fill_summary.call_count == 0
    await store.close()


# ---------------------------------------------------------------------------
# 6. mark_order_failed persists order_id on the row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_order_failed_persists_order_id():
    """Direct test on Store.mark_order_failed: the new optional ``order_id``
    kwarg writes through to the column so the FIX-05 reconciler doesn't
    have to grep logs to recover the CLOB id of a failed delayed order.
    """
    store = await _mk_store()
    await store.insert_pending_order(
        idempotency_key="key-1", event_id="ev1", token_id="tok",
        side="SELL", price=0.45, size_usd=5.0, strategy="B",
    )

    # Default call (no order_id) leaves the column empty — back-compat.
    await store.mark_order_failed("key-1", "test reason")
    async with store.db.execute(
        "SELECT order_id FROM orders WHERE idempotency_key = 'key-1'"
    ) as cur:
        (oid,) = await cur.fetchone()
    assert oid == ""

    # Now mark a second order with order_id passed.
    await store.insert_pending_order(
        idempotency_key="key-2", event_id="ev1", token_id="tok",
        side="SELL", price=0.45, size_usd=5.0, strategy="B",
    )
    await store.mark_order_failed(
        "key-2", "delayed-kill", order_id="0xrealid",
    )
    async with store.db.execute(
        "SELECT order_id, status, failure_reason FROM orders "
        "WHERE idempotency_key = 'key-2'"
    ) as cur:
        row = dict(await cur.fetchone())
    assert row["order_id"] == "0xrealid"
    assert row["status"] == "failed"
    assert row["failure_reason"] == "delayed-kill"
    await store.close()


# ---------------------------------------------------------------------------
# 7. BUY status=delayed triggers GF-1 late-fill probe (mirror of SELL).
#    Probe negative → mark failed + token cooldown.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_branch_probes_when_delayed(fast_sleep):
    """GF-1 (2026-05-01): BUY status=delayed now triggers a late-fill
    probe (the original "BUY late-fills don't happen" assumption was
    falsified by orders #521/#522 ghost-filling on chain on
    2026-05-01).  Probe negative path: mark failed + cool token."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = _mk_clob_mock()
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(
            order_id="0xbuy", success=False,
            message="order not filled (status=delayed)",
        ),
    )
    # G-1 revalidate path: get_top_of_book returns (None, None) → skip
    clob.get_top_of_book = AsyncMock(return_value=(None, None))
    # GF-1 probe: returns None — no late fill on chain
    clob.get_fill_summary = AsyncMock(return_value=None)

    executor = Executor(clob, tracker)
    await executor.execute_signals([_build_buy_signal("tok_buy", 0.45)])

    # GF-1: probe ran on BUY (call_count >= 1; max_attempts default = 3)
    assert clob.get_fill_summary.call_count >= 1
    async with store.db.execute(
        "SELECT status, order_id, side FROM orders"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows[0]["status"] == "failed"
    assert rows[0]["side"] == "BUY"
    assert rows[0]["order_id"] == "0xbuy"
    # G-3 extension: BUY-side delayed-kill cools the token
    assert executor._is_token_cooling("tok_buy")
    await store.close()


@pytest.mark.asyncio
async def test_buy_branch_recovers_ghost_fill(fast_sleep):
    """GF-1 positive path: BUY status=delayed but server actually filled
    it asynchronously → probe finds the fill → record_fill_atomic
    materialises the position; orders row goes 'filled'; no token
    cooldown (the order genuinely succeeded)."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = _mk_clob_mock()
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(
            order_id="0xrecover", success=False,
            message="order not filled (status=delayed)",
        ),
    )
    clob.get_top_of_book = AsyncMock(return_value=(None, None))
    clob.get_fill_summary = AsyncMock(
        return_value=FillSummary(
            shares=10.0, match_price=0.46,
            fee_paid_usd=0.02, net_shares=9.95,
        ),
    )

    executor = Executor(clob, tracker)
    await executor.execute_signals([_build_buy_signal("tok_recover", 0.45)])

    # Order promoted to 'filled' (not 'failed') — recovered path
    async with store.db.execute(
        "SELECT status FROM orders"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows[0]["status"] == "filled"
    # Position materialised with on-chain fill data
    async with store.db.execute(
        "SELECT status, shares, match_price FROM positions"
    ) as cur:
        positions = [dict(r) for r in await cur.fetchall()]
    assert len(positions) == 1
    assert positions[0]["status"] == "open"
    assert positions[0]["shares"] == 9.95
    assert positions[0]["match_price"] == 0.46
    # Token must NOT be in cooldown — order succeeded
    assert not executor._is_token_cooling("tok_recover")
    await store.close()


# ---------------------------------------------------------------------------
# 8. SELL success path: get_fill_summary is now consulted (2026-05-02
#    SELL partial-fill detection) but on a clean full fill the position
#    still flips to closed at the actual match_price.  Probe is the
#    late-fill probe — it's separate, only fires on success=False.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_success_path_unchanged(fast_sleep):
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = _mk_clob_mock()
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="0xfast", success=True),
    )
    # Full fill: net_shares (5.0) ≥ planned (5.0) × 0.95 → close path.
    clob.get_fill_summary = AsyncMock(
        return_value=FillSummary(
            shares=5.0, match_price=0.46,
            fee_paid_usd=0.0, net_shares=5.0,
        ),
    )

    await _seed_open_position(store, "tok_fast", shares=5.0)
    executor = Executor(clob, tracker)
    await executor.execute_signals([_build_sell_signal("tok_fast", 0.45)])

    # get_fill_summary IS called now (one call from the SELL success
    # branch's partial-fill check; the late-fill probe is a separate
    # codepath that only fires on success=False).
    assert clob.get_fill_summary.call_count == 1
    async with store.db.execute(
        "SELECT status, order_id FROM orders"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows[0]["status"] == "filled"
    assert rows[0]["order_id"] == "0xfast"
    async with store.db.execute(
        "SELECT status, exit_price FROM positions"
    ) as cur:
        positions = [dict(r) for r in await cur.fetchall()]
    assert positions[0]["status"] == "closed"
    # exit_price is the actual match (0.46), not the limit (0.45).
    assert positions[0]["exit_price"] == pytest.approx(0.46)
    await store.close()


# ---------------------------------------------------------------------------
# 9. Probe parameters come from StrategyConfig (config.yaml-tunable)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_attempts_and_backoff_read_from_strategy_config(
    fast_sleep, monkeypatch,
):
    """``late_fill_probe_attempts`` / ``late_fill_probe_backoff_s`` move
    from module constants to ``StrategyConfig`` fields so they're tunable
    via ``config.yaml`` without redeploy.  Pin that the executor reads
    them off ``clob._config.strategy`` and threads them into
    ``_poll_for_late_fill`` — not the ``_DEFAULT_*`` fallbacks.
    """
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = _mk_clob_mock()
    # 5 attempts × 2.5s backoff is intentionally different from the
    # defaults (3 / 10.0) so a regression that ignores strategy_config
    # surfaces as a wrong call_count.
    clob._config = SimpleNamespace(
        dry_run=False, paper=False,
        strategy=SimpleNamespace(
            late_fill_probe_attempts=5,
            late_fill_probe_backoff_s=2.5,
        ),
    )
    clob.get_conditional_balance = AsyncMock(return_value=10_000_000)
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(
            order_id="0xtune", success=False,
            message="order not filled (status=delayed)",
        ),
    )
    clob.get_fill_summary = AsyncMock(return_value=None)

    # Spy on _poll_for_late_fill so we can assert the resolved kwargs
    # without depending on the probe loop's internal sleep cadence.
    captured: dict = {}
    real_poll = Executor._poll_for_late_fill

    async def _spy(self, **kwargs):
        captured.update(kwargs)
        return await real_poll(self, **kwargs)

    monkeypatch.setattr(Executor, "_poll_for_late_fill", _spy)

    await _seed_open_position(store, "tok_tune", shares=5.0)
    executor = Executor(clob, tracker)
    await executor.execute_signals([_build_sell_signal("tok_tune", 0.45)])

    assert captured["max_attempts"] == 5
    assert captured["backoff_seconds"] == 2.5
    assert clob.get_fill_summary.call_count == 5
    await store.close()
