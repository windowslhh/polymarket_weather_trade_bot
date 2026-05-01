"""SELL partial-fill detection (2026-05-02): mirror of GF-1 BUY probe.

Production trigger: position #16 (Miami 90-91 May 1, 19.06 NO shares
recovered via GF-3 merge) is in a structurally thin slot.  When EXIT
fires, FAK SELL may fill only part of the requested shares — pre-fix
the success branch ran ``close_positions_for_token`` unconditionally
which marked the whole position closed at the limit price.  The killed
remainder stayed on chain (settler later redeems it), but realized P&L
was misallocated and dashboard showed a closed slot while chain showed
shares still held.

These tests pin:
- SELL full fill (actual ≥ planned × 0.95) → full close at match_price
- SELL partial fill (actual < planned × 0.95) → position decremented,
  status stays 'open', realized P&L lands in daily_pnl
- SELL with get_fill_summary returning None → fallback to legacy close
  (uses limit price; no harm regression for paper / SDK-error paths)
"""
from __future__ import annotations

import tempfile
from datetime import date, datetime, timezone
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
    clob = AsyncMock()
    clob._config = SimpleNamespace(
        dry_run=dry_run, paper=paper, strategy=None,
    )
    # Default: chain has 10 shares (matches our seeded position default).
    clob.get_conditional_balance = AsyncMock(return_value=10_000_000)
    return clob


def _build_sell_signal(
    token_id: str, price_no: float = 0.45, *, strategy: str = "D",
) -> TradeSignal:
    slot = TempSlot(
        token_id_yes="yes_z", token_id_no=token_id,
        outcome_label="90-91°F",
        temp_lower_f=90.0, temp_upper_f=91.0,
        price_no=price_no,
    )
    event = WeatherMarketEvent(
        event_id="ev_sell", condition_id="cs", city="Miami",
        market_date=date(2026, 5, 1), slots=[slot],
    )
    return TradeSignal(
        token_type=TokenType.NO, side=Side.SELL, slot=slot, event=event,
        expected_value=-0.10, estimated_win_prob=0.30,
        suggested_size_usd=0.0, strategy=strategy, reason="EXIT_test",
    )


async def _seed_open_position(
    store, token_id: str, *, shares: float = 10.0,
    entry_price: float = 0.80, match_price: float = 0.78,
    strategy: str = "D",
):
    """Seed an open position with both entry_price and match_price set
    so realized-P&L assertions can use ``effective_entry_price`` directly.
    """
    pos_id = await store.insert_position(
        event_id="ev_sell", token_id=token_id, token_type="NO", city="Miami",
        slot_label="90-91°F", side="BUY",
        entry_price=entry_price, size_usd=shares * entry_price,
        shares=shares, strategy=strategy, buy_reason="seed",
    )
    # Add match_price (insert_position doesn't take it).
    await store.db.execute(
        "UPDATE positions SET match_price = ? WHERE id = ?",
        (match_price, pos_id),
    )
    await store.db.commit()
    return pos_id


@pytest.fixture
def fast_sleep(monkeypatch):
    async def _noop(*_args, **_kwargs):
        return None
    monkeypatch.setattr(executor_mod.asyncio, "sleep", _noop)


# ---------------------------------------------------------------------------
# 1. Full fill (≥95%) → full close at match_price (not limit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_full_fill_closes_at_match_price(fast_sleep):
    """Planned 10 shares, actual 9.97 (99.7%, ≥95% threshold) →
    full close.  ``exit_price`` must be the actual match (0.50), not
    the limit (0.45)."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = _mk_clob_mock()
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="0xfullfill", success=True),
    )
    clob.get_fill_summary = AsyncMock(
        return_value=FillSummary(
            shares=9.97, match_price=0.50,
            fee_paid_usd=0.0, net_shares=9.97,
        ),
    )

    await _seed_open_position(store, "tok_full", shares=10.0)
    executor = Executor(clob, tracker)
    await executor.execute_signals([_build_sell_signal("tok_full", 0.45)])

    # Position closed at the actual match price (0.50), not limit (0.45).
    async with store.db.execute(
        "SELECT status, exit_price, realized_pnl FROM positions"
    ) as cur:
        positions = [dict(r) for r in await cur.fetchall()]
    assert positions[0]["status"] == "closed"
    assert positions[0]["exit_price"] == pytest.approx(0.50)
    # realized = (0.50 - 0.78) * 10 = -2.80 (effective_entry uses match_price=0.78)
    assert positions[0]["realized_pnl"] == pytest.approx(-2.80, abs=0.01)
    await store.close()


# ---------------------------------------------------------------------------
# 2. Partial fill (<95%) → decrement, keep open, realized P&L → daily_pnl
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_partial_fill_decrements_position(fast_sleep):
    """Planned 10 shares, actual 4.0 (40%) → partial-close path:
    - position.shares decreases 10 → 6
    - position.size_usd shrinks proportionally (8.0 → 4.8)
    - position.status stays 'open'
    - realized P&L = (match_price - effective_entry) × sold_shares
      lands in daily_pnl.realized_pnl
    """
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = _mk_clob_mock()
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="0xpartial", success=True),
    )
    # net_shares=4.0 vs planned 10.0 → 40% fill, well under 95%.
    clob.get_fill_summary = AsyncMock(
        return_value=FillSummary(
            shares=4.0, match_price=0.50,
            fee_paid_usd=0.0, net_shares=4.0,
        ),
    )

    pos_id = await _seed_open_position(
        store, "tok_partial", shares=10.0,
        entry_price=0.80, match_price=0.78,
    )
    executor = Executor(clob, tracker)
    await executor.execute_signals([_build_sell_signal("tok_partial", 0.45)])

    async with store.db.execute(
        "SELECT status, shares, size_usd, exit_price, realized_pnl, buy_reason "
        "FROM positions WHERE id = ?",
        (pos_id,),
    ) as cur:
        pos = dict(await cur.fetchone())

    # Status stays open — leftover is still on chain.
    assert pos["status"] == "open"
    # 10 - 4 = 6 shares remain.
    assert pos["shares"] == pytest.approx(6.0)
    # size_usd shrinks proportionally: 8.0 × (6/10) = 4.8.
    assert pos["size_usd"] == pytest.approx(4.8)
    # exit_price + realized_pnl on the row stay NULL (audit clean).
    assert pos["exit_price"] is None
    assert pos["realized_pnl"] is None
    # Audit breadcrumb in buy_reason.
    assert "partial_sell" in (pos["buy_reason"] or "")

    # Realized P&L on partial sale: (0.50 - 0.78) × 4 = -1.12,
    # written to daily_pnl.realized_pnl.
    today = datetime.now(timezone.utc).date().isoformat()
    realized = await store.get_daily_pnl(today)
    assert realized == pytest.approx(-1.12, abs=0.01)

    # Order row went to 'filled' (no longer 'pending') so it doesn't
    # leak into the GF-3 reconciler candidate pool.
    async with store.db.execute(
        "SELECT status FROM orders"
    ) as cur:
        orders = [dict(r) for r in await cur.fetchall()]
    assert orders[0]["status"] == "filled"

    # PARTIAL_SELL decision_log row written for grep-ability.
    async with store.db.execute(
        "SELECT action, reason FROM decision_log"
    ) as cur:
        logs = [dict(r) for r in await cur.fetchall()]
    assert any(
        log["action"] == "SKIP" and "PARTIAL_SELL" in log["reason"]
        for log in logs
    ), f"PARTIAL_SELL decision_log missing: {logs}"
    await store.close()


# ---------------------------------------------------------------------------
# 3. get_fill_summary fails → legacy fallback (full close at limit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_no_summary_falls_back_to_legacy_close(fast_sleep):
    """If /data/trades is unreachable (paper mode, SDK error,
    propagation delay), get_fill_summary returns None.  Behaviour
    must match pre-fix: full close at limit price.  Otherwise we'd
    regress paper-mode users and crash on every transient API blip.
    """
    store = await _mk_store()
    tracker = PortfolioTracker(store)
    clob = _mk_clob_mock()
    clob.place_limit_order = AsyncMock(
        return_value=OrderResult(order_id="0xnosummary", success=True),
    )
    clob.get_fill_summary = AsyncMock(return_value=None)

    await _seed_open_position(store, "tok_nosummary", shares=10.0)
    executor = Executor(clob, tracker)
    await executor.execute_signals(
        [_build_sell_signal("tok_nosummary", 0.45)],
    )

    async with store.db.execute(
        "SELECT status, exit_price FROM positions"
    ) as cur:
        pos = dict(await cur.fetchone())
    # Full close at limit (0.45) — legacy behaviour preserved.
    assert pos["status"] == "closed"
    assert pos["exit_price"] == pytest.approx(0.45)
    await store.close()


# ---------------------------------------------------------------------------
# 4. Tracker.partial_close_positions_for_token writes daily_pnl correctly
# ---------------------------------------------------------------------------
#
# (Multi-position proportional split is defensive code only — the live
# schema's ``idx_positions_no_dup ON (event_id, token_id, strategy)
# WHERE status='open'`` index makes the scenario physically impossible
# to reproduce in a test against the real schema.  The proportional
# loop in ``partial_close_positions_for_token`` is kept for legacy data
# and cross-strategy edge cases; single-position semantics are
# exercised exhaustively by tests 1-3 above.)


@pytest.mark.asyncio
async def test_partial_close_writes_daily_pnl(fast_sleep):
    """Direct call: verify partial_close_positions_for_token decrements
    a single position correctly, returns realized P&L, and updates
    ``daily_pnl.realized_pnl`` for the UTC date bucket."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    pid = await _seed_open_position(
        store, "tok_direct", shares=10.0,
        entry_price=0.80, match_price=0.80,
    )

    touched, realized = await tracker.partial_close_positions_for_token(
        event_id="ev_sell", token_id="tok_direct", strategy="D",
        actual_shares_sold=4.0, match_price=0.50,
        exit_reason="EXIT_test",
    )
    assert touched == 1
    # Realized = (0.50 - 0.80) × 4 = -1.20
    assert realized == pytest.approx(-1.20, abs=0.01)

    async with store.db.execute(
        "SELECT shares, size_usd, status FROM positions WHERE id = ?",
        (pid,),
    ) as cur:
        pos = dict(await cur.fetchone())
    assert pos["status"] == "open"
    assert pos["shares"] == pytest.approx(6.0)
    assert pos["size_usd"] == pytest.approx(4.8)  # 8.0 × (6/10)

    # daily_pnl.realized_pnl for today's UTC bucket got the partial P&L.
    today = datetime.now(timezone.utc).date().isoformat()
    realized_db = await store.get_daily_pnl(today)
    assert realized_db == pytest.approx(-1.20, abs=0.01)
    await store.close()
