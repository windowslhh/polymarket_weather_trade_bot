"""B1 (2026-04-28): cost-basis P&L paths must use ``effective_entry_price``.

Background: ``positions.entry_price`` is the limit price submitted to CLOB.
``positions.match_price`` (added in Fix B) is the actual fill (USDC paid /
shares received).  Pre-B1, every P&L path read ``entry_price`` directly,
so a 1-tick limit-vs-fill divergence (e.g. limit 0.69 → fill 0.685) would
systematically bias P&L on the side of the bot's submitted limit.

This test module pins each writeable P&L path to the helper:

* ``PortfolioTracker.close_positions_for_token`` (SELL → realized_pnl)
* ``settler._compute_position_pnl`` (settlement → daily_pnl)
* ``reconciler._reconcile_sell_hybrid_state`` (crash recovery → realized_pnl)

Each path is exercised twice with the same scenario but different
match_price values, asserting realized P&L diverges by exactly the gap
× shares and that NULL match_price falls back to entry_price untouched.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.alerts import Alerter
from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker
from src.portfolio.utils import effective_entry_price
from src.recovery.reconciler import reconcile_pending_orders
from src.settlement.settler import _compute_position_pnl
from unittest.mock import MagicMock


# ──────────────────────────────────────────────────────────────────────
# Helper itself.
# ──────────────────────────────────────────────────────────────────────


def test_helper_prefers_match_price():
    p = {"entry_price": 0.69, "match_price": 0.685, "shares": 5.0}
    assert effective_entry_price(p) == 0.685


def test_helper_falls_back_to_entry_when_match_is_none():
    p = {"entry_price": 0.69, "match_price": None, "shares": 5.0}
    assert effective_entry_price(p) == 0.69


def test_helper_falls_back_to_entry_when_match_is_missing():
    p = {"entry_price": 0.69, "shares": 5.0}
    assert effective_entry_price(p) == 0.69


def test_helper_accepts_dataclass_shape():
    class _Row:
        entry_price = 0.69
        match_price = 0.685

    assert effective_entry_price(_Row()) == 0.685


# ──────────────────────────────────────────────────────────────────────
# tracker.close_positions_for_token: realized P&L on SELL.
# ──────────────────────────────────────────────────────────────────────


async def _mk_store_with_position(match_price: float | None) -> tuple[Store, int]:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    pos_id = await store.insert_position(
        event_id="ev1", token_id="tok1", token_type="NO",
        city="Miami", slot_label="86-87°F", side="BUY",
        entry_price=0.69, size_usd=3.45, shares=5.0,  # 5 shares
        strategy="B", buy_reason="test",
        match_price=match_price, fee_paid_usd=None,
    )
    return store, pos_id


@pytest.mark.asyncio
async def test_close_positions_uses_match_price_when_present():
    """SELL at 0.75; entry limit 0.69, actual fill 0.685.  Realized P&L
    must use 0.685 → (0.75 - 0.685) × 5 = 0.325, not (0.75 - 0.69) × 5 = 0.30.
    """
    store, _pos_id = await _mk_store_with_position(match_price=0.685)
    tracker = PortfolioTracker(store)

    closed = await tracker.close_positions_for_token(
        event_id="ev1", token_id="tok1", strategy="B",
        exit_reason="test", exit_price=0.75,
    )
    assert closed == 1

    async with store.db.execute(
        "SELECT realized_pnl FROM positions WHERE event_id='ev1'"
    ) as cur:
        row = await cur.fetchone()
    assert abs(row[0] - (0.75 - 0.685) * 5.0) < 1e-9
    assert abs(row[0] - (0.75 - 0.69) * 5.0) > 1e-3, (
        "Sanity: 0.685 vs 0.69 must produce a meaningfully different P&L"
    )

    await store.close()


@pytest.mark.asyncio
async def test_close_positions_falls_back_to_entry_when_match_null():
    """Legacy / paper rows have match_price=NULL → must use entry_price."""
    store, _pos_id = await _mk_store_with_position(match_price=None)
    tracker = PortfolioTracker(store)

    await tracker.close_positions_for_token(
        event_id="ev1", token_id="tok1", strategy="B",
        exit_reason="test", exit_price=0.75,
    )

    async with store.db.execute(
        "SELECT realized_pnl FROM positions WHERE event_id='ev1'"
    ) as cur:
        row = await cur.fetchone()
    assert abs(row[0] - (0.75 - 0.69) * 5.0) < 1e-9

    await store.close()


# ──────────────────────────────────────────────────────────────────────
# settler._compute_position_pnl: settlement P&L.
# ──────────────────────────────────────────────────────────────────────


def test_settler_pnl_uses_match_price_on_no_win():
    """NO wins (yes_resolved=0): P&L = (1 - effective_entry) × shares.
    A 0.685 fill vs a 0.69 limit shifts P&L by +0.005 × shares = +0.025.
    """
    pos = {
        "slot_label": "86-87°F", "token_type": "NO",
        "entry_price": 0.69, "match_price": 0.685, "shares": 5.0,
        "token_id": "tok1",
    }
    label_prices = {"86-87°F": 0.0}  # NO wins
    pnl = _compute_position_pnl(pos, label_prices)
    assert abs(pnl - (1.0 - 0.685) * 5.0) < 1e-9


def test_settler_pnl_uses_match_price_on_no_lose():
    """NO loses (yes_resolved=1): P&L = -effective_entry × shares.
    A 0.685 fill vs 0.69 limit means losing 0.025 less.
    """
    pos = {
        "slot_label": "86-87°F", "token_type": "NO",
        "entry_price": 0.69, "match_price": 0.685, "shares": 5.0,
        "token_id": "tok1",
    }
    label_prices = {"86-87°F": 1.0}  # YES wins → NO loses
    pnl = _compute_position_pnl(pos, label_prices)
    assert abs(pnl - (-0.685 * 5.0)) < 1e-9


def test_settler_pnl_falls_back_when_match_null():
    pos = {
        "slot_label": "86-87°F", "token_type": "NO",
        "entry_price": 0.69, "match_price": None, "shares": 5.0,
        "token_id": "tok1",
    }
    label_prices = {"86-87°F": 0.0}  # NO wins
    pnl = _compute_position_pnl(pos, label_prices)
    assert abs(pnl - (1.0 - 0.69) * 5.0) < 1e-9


# ──────────────────────────────────────────────────────────────────────
# reconciler._reconcile_sell_hybrid_state: crash-recovery P&L.
# ──────────────────────────────────────────────────────────────────────


def _alerter() -> Alerter:
    a = MagicMock(spec=Alerter)

    async def _send(*args, **kwargs):
        return None

    a.send = MagicMock(side_effect=_send)
    return a


@pytest.mark.asyncio
async def test_reconciler_uses_match_price_for_realized_pnl():
    """Crash window: SELL filled, position still open.  Healing closes the
    position with realized = (sell_price - effective_entry) × shares.
    With match_price=0.685 and SELL @0.75 × 5 shares, realized = 0.325 (not 0.30)."""
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    alerter = _alerter()

    pos_id = await store.insert_position(
        event_id="ev_x", token_id="tok_x", token_type="NO",
        city="Miami", slot_label="86-87°F", side="BUY",
        entry_price=0.69, size_usd=3.45, shares=5.0,
        strategy="B", buy_reason="test",
        match_price=0.685, fee_paid_usd=None,
    )
    await store.db.execute(
        """INSERT INTO orders
           (order_id, event_id, token_id, side, price, size_usd,
            status, filled_at, idempotency_key, strategy)
           VALUES ('clob_sell_x', 'ev_x', 'tok_x', 'SELL', 0.75, 3.75,
                   'filled', datetime('now'), 'sell_key_x', 'B')""",
    )
    await store.db.commit()

    await reconcile_pending_orders(
        store, alerter, query_clob_order=None, is_paper=True,
    )

    async with store.db.execute(
        "SELECT status, realized_pnl FROM positions WHERE id=?", (pos_id,),
    ) as cur:
        row = dict(await cur.fetchone())
    assert row["status"] == "closed"
    assert abs(row["realized_pnl"] - (0.75 - 0.685) * 5.0) < 1e-9
    # Sanity vs the limit-price answer.
    assert abs(row["realized_pnl"] - (0.75 - 0.69) * 5.0) > 1e-3

    await store.close()


@pytest.mark.asyncio
async def test_reconciler_falls_back_to_entry_when_match_null():
    """Legacy row (no match_price) → reconciler still computes correctly via
    the helper's fallback branch."""
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    alerter = _alerter()

    pos_id = await store.insert_position(
        event_id="ev_y", token_id="tok_y", token_type="NO",
        city="Chicago", slot_label="80-81°F", side="BUY",
        entry_price=0.60, size_usd=6.0, shares=10.0,
        strategy="B", buy_reason="test",
        match_price=None, fee_paid_usd=None,
    )
    await store.db.execute(
        """INSERT INTO orders
           (order_id, event_id, token_id, side, price, size_usd,
            status, filled_at, idempotency_key, strategy)
           VALUES ('clob_sell_y', 'ev_y', 'tok_y', 'SELL', 0.75, 7.5,
                   'filled', datetime('now'), 'sell_key_y', 'B')""",
    )
    await store.db.commit()

    await reconcile_pending_orders(
        store, alerter, query_clob_order=None, is_paper=True,
    )

    async with store.db.execute(
        "SELECT realized_pnl FROM positions WHERE id=?", (pos_id,),
    ) as cur:
        row = dict(await cur.fetchone())
    # (0.75 - 0.60) * 10 = 1.5 — same as test_sell_hybrid_state_closed_on_reconcile.
    assert abs(row["realized_pnl"] - 1.5) < 1e-9

    await store.close()
