"""FIX-05: startup reconciler for orphaned pending orders.

Each test sets up a pending order row (the output of the FIX-03 flow
crashing between CLOB call and the atomic fill) and asserts the
reconciler takes the documented action.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.alerts import Alerter
from src.portfolio.store import Store
from src.recovery.reconciler import (
    ClobOrderStatus,
    reconcile_pending_orders,
)


async def _mk_store_with_pending(key: str = "key123", order_id: str = "") -> Store:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    await store.insert_pending_order(
        idempotency_key=key,
        event_id="ev_1", token_id="tok_1",
        side="BUY", price=0.45, size_usd=10.0,
    )
    return store


def _alerter() -> Alerter:
    a = Alerter(webhook_url="")
    a.send = AsyncMock()  # type: ignore[method-assign]
    return a


@pytest.mark.asyncio
async def test_no_pending_orders_is_a_noop():
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    alerter = _alerter()

    await reconcile_pending_orders(
        store, alerter, query_clob_order=None, is_paper=True,
    )

    alerter.send.assert_not_called()
    await store.close()


@pytest.mark.asyncio
async def test_paper_mode_marks_all_pending_as_failed():
    store = await _mk_store_with_pending()
    alerter = _alerter()

    await reconcile_pending_orders(
        store, alerter, query_clob_order=None, is_paper=True,
    )

    async with store.db.execute(
        "SELECT status, failure_reason FROM orders"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "paper_mode_orphan" in rows[0]["failure_reason"]
    alerter.send.assert_called_once()
    await store.close()


@pytest.mark.asyncio
async def test_clob_filled_promotes_order_to_filled():
    store = await _mk_store_with_pending(key="keyFILL")
    alerter = _alerter()

    async def probe(row: dict) -> ClobOrderStatus:
        assert row["idempotency_key"] == "keyFILL"
        return ClobOrderStatus(
            state="filled", order_id="clob_ord_X",
            price=0.45, size=10.0 / 0.45,  # matches intended size
        )

    await reconcile_pending_orders(
        store, alerter, query_clob_order=probe, is_paper=False,
    )

    async with store.db.execute(
        "SELECT status, order_id FROM orders"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows[0]["status"] == "filled"
    assert rows[0]["order_id"] == "clob_ord_X"
    alerter.send.assert_called_once()
    args, _ = alerter.send.call_args
    assert args[0] == "warning"
    assert "promoted to 'filled'" in args[1]
    await store.close()


@pytest.mark.asyncio
async def test_clob_open_promotes_to_open():
    """Review Blocker #2: CLOB reports the order is still resting unfilled.

    We promote the DB row to 'open' (not 'failed') so it can be
    re-reconciled when it eventually fills or is cancelled.
    """
    store = await _mk_store_with_pending(key="keyOPEN")
    alerter = _alerter()

    async def probe(row: dict) -> ClobOrderStatus:
        return ClobOrderStatus(
            state="open", order_id="clob_live_99",
            price=0.45, size=10.0 / 0.45,
        )

    await reconcile_pending_orders(
        store, alerter, query_clob_order=probe, is_paper=False,
    )

    async with store.db.execute(
        "SELECT status, order_id FROM orders"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows[0]["status"] == "open"
    assert rows[0]["order_id"] == "clob_live_99"
    alerter.send.assert_called_once()
    assert alerter.send.call_args[0][0] == "info"
    await store.close()


@pytest.mark.asyncio
async def test_clob_cancelled_marks_failed():
    store = await _mk_store_with_pending(key="keyCAN")
    alerter = _alerter()

    async def probe(row: dict) -> ClobOrderStatus:
        return ClobOrderStatus(state="cancelled", message="user cancel")

    await reconcile_pending_orders(
        store, alerter, query_clob_order=probe, is_paper=False,
    )

    async with store.db.execute(
        "SELECT status, failure_reason FROM orders"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows[0]["status"] == "failed"
    assert "cancelled" in rows[0]["failure_reason"]
    await store.close()


@pytest.mark.asyncio
async def test_clob_unreachable_leaves_pending():
    store = await _mk_store_with_pending(key="keyHUH")
    alerter = _alerter()

    async def probe(row: dict) -> ClobOrderStatus:
        return ClobOrderStatus(state="unreachable", message="timeout")

    await reconcile_pending_orders(
        store, alerter, query_clob_order=probe, is_paper=False,
    )

    async with store.db.execute("SELECT status FROM orders") as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows[0]["status"] == "pending", (
        "Unreachable CLOB must not mutate state — retry on next startup"
    )
    alerter.send.assert_called_once()
    assert alerter.send.call_args[0][0] == "critical"
    await store.close()


@pytest.mark.asyncio
async def test_clob_price_mismatch_triggers_exit():
    store = await _mk_store_with_pending(key="keyBAD")
    alerter = _alerter()

    async def probe(row: dict) -> ClobOrderStatus:
        # Wildly different price — we do NOT want to auto-reconcile this.
        return ClobOrderStatus(
            state="filled", order_id="X", price=0.90, size=11.1,
        )

    # exit_on_mismatch=False so the test process survives; assert alert.
    await reconcile_pending_orders(
        store, alerter, query_clob_order=probe, is_paper=False,
        exit_on_mismatch=False,
    )

    async with store.db.execute("SELECT status FROM orders") as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    # Mismatch path: we do NOT promote to filled.  Row is still pending.
    assert rows[0]["status"] == "pending"
    alerter.send.assert_called_once()
    assert alerter.send.call_args[0][0] == "critical"
    assert "MISMATCH" in alerter.send.call_args[0][1]
    await store.close()


@pytest.mark.asyncio
async def test_probe_exception_leaves_pending_with_critical():
    store = await _mk_store_with_pending(key="keyERR")
    alerter = _alerter()

    async def probe(row: dict) -> ClobOrderStatus:
        raise RuntimeError("network blew up")

    await reconcile_pending_orders(
        store, alerter, query_clob_order=probe, is_paper=False,
    )

    async with store.db.execute("SELECT status FROM orders") as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows[0]["status"] == "pending"  # leave for next startup
    alerter.send.assert_called_once()
    assert alerter.send.call_args[0][0] == "critical"
    await store.close()


@pytest.mark.asyncio
async def test_sell_hybrid_state_closed_on_reconcile():
    """Review 🟡 #6: a filled SELL paired with an open position row — the
    crash window between finalize_sell_order and close_positions_for_token
    — must be healed by the reconciler.
    """
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    alerter = _alerter()

    # Seed a BUY position that should now be closed.
    pos_id = await store.insert_position(
        event_id="ev_x", token_id="tok_x", token_type="NO",
        city="Chicago", slot_label="80-81°F", side="BUY",
        entry_price=0.60, size_usd=6.0, shares=10.0,
        strategy="B", buy_reason="test",
    )
    # Seed the orders side: a filled SELL for same (event, token).
    await store.db.execute(
        """INSERT INTO orders
           (order_id, event_id, token_id, side, price, size_usd,
            status, filled_at, idempotency_key)
           VALUES ('clob_sell_1', 'ev_x', 'tok_x', 'SELL', 0.75, 7.5,
                   'filled', datetime('now'), 'sell_key_1')""",
    )
    await store.db.commit()

    await reconcile_pending_orders(
        store, alerter, query_clob_order=None, is_paper=True,
    )

    async with store.db.execute(
        "SELECT status, exit_price, realized_pnl FROM positions WHERE id=?",
        (pos_id,),
    ) as cur:
        row = dict(await cur.fetchone())
    assert row["status"] == "closed"
    assert row["exit_price"] == 0.75
    # Realized = (0.75 - 0.60) * 10 shares = 1.5
    assert abs(row["realized_pnl"] - 1.5) < 1e-6
    # Alerter receives one "warning" about the heal.
    call_levels = [c.args[0] for c in alerter.send.call_args_list]
    assert "warning" in call_levels
    await store.close()


@pytest.mark.asyncio
async def test_live_mode_without_probe_fails_and_criticals():
    store = await _mk_store_with_pending(key="keyNoprobe")
    alerter = _alerter()

    await reconcile_pending_orders(
        store, alerter, query_clob_order=None, is_paper=False,
    )

    async with store.db.execute("SELECT status FROM orders") as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows[0]["status"] == "failed"
    alerter.send.assert_called_once()
    assert alerter.send.call_args[0][0] == "critical"
    await store.close()
