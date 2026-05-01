"""GF-Block-4 / GF-Block-5 (2026-05-01 review): same-slot merge,
rollback safety, and idempotency of ``Store.recover_ghost_buy_fill``.

The production trigger was orders #521/#522 — both BUY against the
same (event_id, token_id, strategy) for Miami 90-91 May 1, both
ghost-filled on chain.  The first iteration of recover_ghost_buy_fill
only deduped on source_order_id, so the second recovery would crash
on the ``idx_positions_no_dup ON (event_id, token_id, strategy)
WHERE status='open'`` UNIQUE index.  Without explicit rollback, the
half-applied UPDATE would leak into the next caller's commit, leaving
the second order in 'filled' status with no position row — strictly
worse than the original ghost.

These tests pin:
- two ghosts on the same slot merge into one position (shares + size
  accumulate, match_price is shares-weighted)
- a forced INSERT failure rolls the UPDATE back so the orders row
  stays 'failed' and is re-attempted on the next recovery pass
- re-running recovery against an already-recovered order is a no-op
  (returns -1)
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.portfolio.store import Store


async def _mk_store():
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    return store


async def _seed_failed_buy(
    store: Store,
    *,
    order_id: str,
    event_id: str = "ev_ghost",
    token_id: str = "tok_ghost",
    strategy: str = "D",
    price: float = 0.80,
    size_usd: float = 4.80,
) -> int:
    """Insert a 'failed' BUY orders row that mimics the production
    delayed-kill case: order_id present, failure_reason mentions
    'delayed', no associated position."""
    cur = await store.db.execute(
        """INSERT INTO orders
             (order_id, event_id, token_id, side, price, size_usd,
              idempotency_key, status, failure_reason, strategy)
           VALUES (?, ?, ?, 'BUY', ?, ?, ?, 'failed',
                   'order not filled (status=delayed)', ?)""",
        (order_id, event_id, token_id, price, size_usd,
         f"ikey-{order_id}", strategy),
    )
    await store.db.commit()
    return cur.lastrowid  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_recover_ghost_buy_fill_merges_same_slot():
    """GF-Block-5: two ghost fills on same (event,token,strategy) merge
    into one position.  Shares + size + fee accumulate; match_price is
    shares-weighted."""
    store = await _mk_store()
    try:
        order_a = await _seed_failed_buy(store, order_id="0xaaa", price=0.80)
        order_b = await _seed_failed_buy(store, order_id="0xbbb", price=0.79)

        # First recovery: fresh slot insert.
        pos_a = await store.recover_ghost_buy_fill(
            failed_order_id=order_a, clob_order_id="0xaaa",
            event_id="ev_ghost", token_id="tok_ghost", token_type="NO",
            city="Miami", slot_label="90-91°F May 1",
            entry_price=0.80, size_usd=4.80, shares=6.0,
            strategy="D", buy_reason="ghost_recovered_test",
            entry_ev=0.18, entry_win_prob=0.99,
            match_price=0.80, fee_paid_usd=0.0,
        )
        assert pos_a > 0

        # Second recovery: same slot — must merge into existing.
        pos_b = await store.recover_ghost_buy_fill(
            failed_order_id=order_b, clob_order_id="0xbbb",
            event_id="ev_ghost", token_id="tok_ghost", token_type="NO",
            city="Miami", slot_label="90-91°F May 1",
            entry_price=0.79, size_usd=4.74, shares=6.0,
            strategy="D", buy_reason="ghost_recovered_test",
            entry_ev=0.19, entry_win_prob=0.99,
            match_price=0.79, fee_paid_usd=0.04,
        )
        assert pos_b == pos_a, "merge must return the existing position id"

        # Exactly one open position for this slot.
        async with store.db.execute(
            "SELECT shares, size_usd, match_price, fee_paid_usd, buy_reason "
            "FROM positions WHERE id = ?",
            (pos_a,),
        ) as cur:
            row = dict(await cur.fetchone())
        assert row["shares"] == pytest.approx(12.0)
        assert row["size_usd"] == pytest.approx(9.54)
        # weighted match: (6 * 0.80 + 6 * 0.79) / 12 = 0.795
        assert row["match_price"] == pytest.approx(0.795)
        assert row["fee_paid_usd"] == pytest.approx(0.04)
        assert "merged:0xbbb" in (row["buy_reason"] or "")

        # Both orders rows are now flipped to 'filled'.
        async with store.db.execute(
            "SELECT id, status, failure_reason FROM orders ORDER BY id"
        ) as cur:
            orders = [dict(r) for r in await cur.fetchall()]
        assert orders[0]["status"] == "filled"
        assert orders[0]["failure_reason"] == "ghost_recovered"
        assert orders[1]["status"] == "filled"
        assert orders[1]["failure_reason"] == "ghost_recovered_merged"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recover_ghost_buy_fill_idempotent_rerun():
    """Re-running recovery for the same clob_order_id is a no-op
    (returns -1).  Production scenario: GF-3 restart-time scan and the
    one-shot script both land on the same order — only one writes."""
    store = await _mk_store()
    try:
        oid = await _seed_failed_buy(store, order_id="0xidem")
        first = await store.recover_ghost_buy_fill(
            failed_order_id=oid, clob_order_id="0xidem",
            event_id="ev_ghost", token_id="tok_ghost", token_type="NO",
            city="Miami", slot_label="90-91°F",
            entry_price=0.80, size_usd=4.80, shares=6.0,
            strategy="D", buy_reason="ghost_recovered_test",
            match_price=0.80, fee_paid_usd=0.0,
        )
        assert first > 0

        # Re-run: position already exists → return -1, no write.
        second = await store.recover_ghost_buy_fill(
            failed_order_id=oid, clob_order_id="0xidem",
            event_id="ev_ghost", token_id="tok_ghost", token_type="NO",
            city="Miami", slot_label="90-91°F",
            entry_price=0.80, size_usd=4.80, shares=6.0,
            strategy="D", buy_reason="ghost_recovered_test",
            match_price=0.80, fee_paid_usd=0.0,
        )
        assert second == -1

        async with store.db.execute(
            "SELECT COUNT(*) FROM positions"
        ) as cur:
            (count,) = await cur.fetchone()
        assert count == 1, "no double-write on idempotent re-run"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recover_ghost_buy_fill_rollback_on_insert_failure():
    """GF-Block-4: forced INSERT failure must rollback the prior UPDATE.
    Otherwise the half-applied UPDATE leaks into the next caller's
    commit, leaving the orders row 'filled' with no position —
    corrupted state strictly worse than the original ghost.

    Trigger the failure with a SQLite NOT NULL violation by passing
    ``None`` for the ``city`` column (declared NOT NULL in the
    positions schema).  This is more robust than monkey-patching
    aiosqlite's dual-protocol execute() — it exercises the real SQL
    path and verifies the actual rollback semantics.
    """
    store = await _mk_store()
    try:
        oid = await _seed_failed_buy(store, order_id="0xrollback",
                                     event_id="ev_rollback", token_id="tok_rollback")

        # NOT NULL violation on `city` → INSERT fails after orders UPDATE
        # has executed.  Without rollback, the UPDATE lingers in the
        # implicit transaction.
        with pytest.raises(Exception):
            await store.recover_ghost_buy_fill(
                failed_order_id=oid, clob_order_id="0xrollback",
                event_id="ev_rollback", token_id="tok_rollback",
                token_type="NO",
                city=None,  # type: ignore[arg-type]  # forces NOT NULL violation
                slot_label="ghost",
                entry_price=0.80, size_usd=4.80, shares=6.0,
                strategy="D", buy_reason="ghost_recovered_test",
                match_price=0.80, fee_paid_usd=0.0,
            )

        # Orders row stayed 'failed' — UPDATE was rolled back.
        async with store.db.execute(
            "SELECT status, failure_reason FROM orders WHERE id = ?",
            (oid,),
        ) as cur:
            row = dict(await cur.fetchone())
        assert row["status"] == "failed", (
            f"orders row leaked into 'filled' state: {row}; rollback failed"
        )
        assert "delayed" in row["failure_reason"]
        # No position row was inserted.
        async with store.db.execute(
            "SELECT COUNT(*) FROM positions"
        ) as cur:
            (count,) = await cur.fetchone()
        assert count == 0

        # GF-Block-4 leakage scenario: an explicit commit should be a
        # no-op now (rollback already cleared the transaction).  If the
        # rollback didn't fire, this commit would push the UPDATE out
        # and the orders row would flip to 'filled'.
        await store.db.commit()
        async with store.db.execute(
            "SELECT status FROM orders WHERE id = ?", (oid,),
        ) as cur:
            (status,) = await cur.fetchone()
        assert status == "failed", (
            "rolled-back UPDATE leaked into next commit (Block-4 not fixed)"
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_get_failed_delayed_buy_orders_excludes_recovered():
    """The candidate query NOT EXISTS clause hides orders that already
    have an associated position — re-runs of the reconciler don't see
    fully-recovered ghosts."""
    store = await _mk_store()
    try:
        oid_recovered = await _seed_failed_buy(store, order_id="0xrec",
                                               token_id="tok_rec")
        oid_pending = await _seed_failed_buy(store, order_id="0xpending",
                                             token_id="tok_pending")

        # Recover the first one.
        await store.recover_ghost_buy_fill(
            failed_order_id=oid_recovered, clob_order_id="0xrec",
            event_id="ev_ghost", token_id="tok_rec", token_type="NO",
            city="Miami", slot_label="recovered",
            entry_price=0.80, size_usd=4.80, shares=6.0,
            strategy="D", buy_reason="ghost_recovered_test",
            match_price=0.80, fee_paid_usd=0.0,
        )

        candidates = await store.get_failed_delayed_buy_orders()
        ids = [c["id"] for c in candidates]
        assert oid_recovered not in ids, "recovered order must be excluded"
        assert oid_pending in ids, "still-failed order must be a candidate"
    finally:
        await store.close()
