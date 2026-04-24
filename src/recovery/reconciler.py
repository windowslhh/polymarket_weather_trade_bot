"""FIX-05: pair orphaned pending orders with CLOB state on startup.

Flow:
1. Read `orders WHERE status='pending'` (FIX-03 leaves these behind if we
   crashed between CLOB fill and the atomic position insert).
2. For each pending row, ask CLOB what it knows (by idempotency_key stored
   client-side, NOT by order_id — the order_id field is empty until the
   CLOB call returns).
3. Decide:
     - CLOB says filled  → finalize the position atomically, alert info.
     - CLOB says cancelled/unknown → mark the DB row 'failed', alert info.
     - CLOB API unreachable → alert critical, leave the row 'pending'
       (next startup tries again; operator investigates).
     - DB/CLOB disagree on substantive fields (e.g. price, size) → alert
       critical and sys.exit(3).  Better to refuse to trade than to trade
       from a state we don't trust.

In paper/dry-run mode there is no CLOB to query, so all pending rows are
marked 'failed' with a `paper_mode_orphan` reason.  The bot then proceeds.

This module is intentionally decoupled from clob_client's py-clob-client
dependency: callers pass an async `query_clob_order(idempotency_key)` that
returns a `ClobOrderStatus`.  Tests stub this directly.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from src.alerts import Alerter
from src.portfolio.store import Store

logger = logging.getLogger(__name__)


@dataclass
class ClobOrderStatus:
    """Outcome of asking CLOB about a single pending order row."""
    state: str  # one of 'filled', 'open', 'cancelled', 'unknown', 'unreachable'
    order_id: str = ""
    price: float | None = None
    size: float | None = None
    message: str = ""


# Signature for the CLOB probe.  Takes the full orders row (dict) so the
# probe can match on token/side/price/size — py-clob-client doesn't know
# about our idempotency_key, so we need the whole intent to find the
# corresponding CLOB record.
QueryFn = Callable[[dict], Awaitable[ClobOrderStatus]]


async def reconcile_pending_orders(
    store: Store,
    alerter: Alerter,
    query_clob_order: QueryFn | None,
    *,
    is_paper: bool = False,
    exit_on_mismatch: bool = True,
) -> None:
    """Resolve orphaned pending orders before the bot starts trading.

    Parameters
    ----------
    store
        Initialised Store — must already have the orders table.
    alerter
        Wired-up Alerter.  All state-changing decisions fan out here.
    query_clob_order
        Async callable taking an idempotency_key and returning
        ClobOrderStatus.  Ignored in paper mode.  When ``None`` and
        not paper mode, all pending orders are marked failed with an
        "unreachable" reason and a critical alert fires.
    is_paper
        If True, skip CLOB lookup and mark all pending orders failed.
    exit_on_mismatch
        Keep True in production so the bot refuses to start on
        substantive DB/CLOB disagreements.  Tests set False to assert
        the alert path without terminating the test process.
    """
    # Review 🟡 #6: resolve the SELL hybrid-state window BEFORE touching
    # pending BUYs.  A SELL order that finalized but crashed before
    # close_positions_for_token leaves the position open; fix that first
    # so subsequent size calculations reflect the truth.
    await _reconcile_sell_hybrid_state(store, alerter)

    pending = await store.get_pending_orders()
    if not pending:
        logger.info("Reconciler: no pending orders to reconcile")
        return

    logger.info("Reconciler: found %d pending order(s), resolving...", len(pending))

    for row in pending:
        key = row.get("idempotency_key")
        if not key:
            # Pre-FIX-03 orphan without a key — nothing we can probe.
            # Safest thing is to fail it and alert so ops sees the handoff.
            await store.mark_order_failed(
                idempotency_key="",  # keyless: we fall back to order_id
                reason="pre-FIX-03 pending order, no idempotency_key",
            )
            # The mark_order_failed(key='') above won't match anything; null
            # out by DB id instead so these don't linger across restarts.
            await _force_fail_by_id(store, row["id"], "pre-FIX-03 pending order")
            await alerter.send(
                "warning",
                f"Reconciler: legacy pending order id={row['id']} marked failed "
                "(no idempotency_key to probe CLOB with)",
            )
            continue

        if is_paper:
            await store.mark_order_failed(key, "paper_mode_orphan (no CLOB to reconcile)")
            await alerter.send(
                "info",
                f"Reconciler: paper-mode orphan key={key[:8]} marked failed",
            )
            continue

        if query_clob_order is None:
            await store.mark_order_failed(key, "CLOB unreachable (no probe configured)")
            await alerter.send(
                "critical",
                f"Reconciler: pending order key={key[:8]} marked failed — "
                "no CLOB probe configured in live mode.  Verify the order "
                "did not fill on CLOB before trusting this reconciliation.",
            )
            continue

        try:
            status = await query_clob_order(row)
        except Exception as exc:
            logger.exception("CLOB probe raised for key=%s", key)
            await alerter.send(
                "critical",
                f"Reconciler: CLOB probe raised for key={key[:8]} — {exc}. "
                "Leaving order pending; next startup retries.",
            )
            # Don't mutate — next restart tries again.
            continue

        await _apply_status(
            store=store, alerter=alerter, row=row, status=status,
            exit_on_mismatch=exit_on_mismatch,
        )

    logger.info("Reconciler: done")


async def _apply_status(
    *,
    store: Store,
    alerter: Alerter,
    row: dict[str, Any],
    status: ClobOrderStatus,
    exit_on_mismatch: bool,
) -> None:
    key = row["idempotency_key"]
    if status.state == "filled":
        # Before promoting to 'filled', sanity-check price/size.  Bigger
        # drift means the CLOB fill does NOT correspond to our intent —
        # a bug we don't want to silently paper over.
        if _substantive_mismatch(row, status):
            msg = (
                f"Reconciler MISMATCH: DB order key={key[:8]} "
                f"db(price={row['price']:.4f},size_usd={row['size_usd']:.2f}) vs "
                f"clob(price={status.price},size={status.size})"
            )
            logger.error(msg)
            await alerter.send("critical", msg)
            if exit_on_mismatch:
                sys.exit(3)
            return
        # Promote-only at the orders level; we do NOT insert the position
        # here because (a) we don't have full signal metadata (strategy,
        # reason, slot_label) in the orders row, and (b) the CLOB fill
        # pre-dates our visibility.  The operator manually creates the
        # position from the CLOB fill record if needed.  Use the dedicated
        # orphan helper so audit queries can tell this apart from a normal
        # SELL-side finalization.
        await store.mark_order_filled_orphan(key, status.order_id)
        await alerter.send(
            "warning",
            f"Reconciler: CLOB-filled orphan key={key[:8]} promoted to 'filled' "
            "in DB; position NOT auto-created — operator must reconcile "
            "positions table manually from CLOB fill history.",
        )
        return

    if status.state == "open":
        # CLOB still has it as a resting limit order.  Promote to 'open' so
        # the bot doesn't treat this as a pending placement that needs
        # re-attempting, but also so the next reconciler pass can match it
        # when it eventually fills (or an operator cancels it).
        await store.mark_order_open(key, status.order_id)
        await alerter.send(
            "info",
            f"Reconciler: orphan key={key[:8]} is open on CLOB (order_id={status.order_id[:10]}); "
            "left as 'open' in DB for later fill-reconciliation.",
        )
        return

    if status.state in ("cancelled", "unknown"):
        await store.mark_order_failed(
            key, f"CLOB {status.state}: {status.message}"[:500],
        )
        await alerter.send(
            "info",
            f"Reconciler: orphan key={key[:8]} marked failed ({status.state})",
        )
        return

    if status.state == "unreachable":
        await alerter.send(
            "critical",
            f"Reconciler: CLOB unreachable for key={key[:8]} — leaving pending",
        )
        return

    # Truly unexpected state — fail closed.
    msg = f"Reconciler: unknown CLOB state {status.state!r} for key={key[:8]}"
    logger.error(msg)
    await alerter.send("critical", msg)
    if exit_on_mismatch:
        sys.exit(3)


def _substantive_mismatch(row: dict[str, Any], status: ClobOrderStatus) -> bool:
    """Flag mismatches that should block a clean reconcile.

    We're permissive on tiny float drift (CLOB may round differently) but
    strict on anything suggesting a different order entirely.
    Widened to 0.01 (10 ticks) alongside probe_order_status — H-3.
    """
    if status.price is not None and abs(status.price - float(row["price"])) > 0.01:
        return True
    if status.size is not None:
        # size from CLOB is in shares; row.size_usd is dollars.  Re-derive
        # intended shares from the row before comparing.
        intended_shares = (
            float(row["size_usd"]) / float(row["price"]) if row["price"] > 0 else 0
        )
        if abs(status.size - intended_shares) > 0.5:  # 0.5 share slack
            return True
    return False


async def _force_fail_by_id(store: Store, row_id: int, reason: str) -> None:
    """Flip a specific orders row to 'failed' by primary key when we have
    no idempotency_key to use.  Used for legacy pre-FIX-03 pending rows."""
    await store.db.execute(
        "UPDATE orders SET status='failed', failure_reason=? WHERE id=?",
        (reason[:500], row_id),
    )
    await store.db.commit()


async def _reconcile_sell_hybrid_state(
    store: Store, alerter: Alerter,
) -> None:
    """Close positions whose latest SELL order is filled but whose
    position row is still open — the exact crash-between-commits window.

    Heal by closing at the SELL price.  Realized P&L is computed as
    (sell_price − entry_price) × shares, same as
    close_positions_for_token does.
    """
    rows = await store.get_filled_sells_with_open_positions()
    if not rows:
        return
    logger.warning(
        "Reconciler: %d position(s) have a filled SELL but are still open — "
        "closing now to heal SELL hybrid state",
        len(rows),
    )
    for r in rows:
        entry = float(r["entry_price"])
        sell = float(r["price"])
        shares = float(r["shares"])
        realized = (sell - entry) * shares
        await store.close_position(
            position_id=r["position_id"],
            exit_reason="reconciler: filled SELL orphan close",
            exit_price=sell,
            realized_pnl=realized,
        )
        await alerter.send(
            "warning",
            f"Reconciler: closed orphaned position id={r['position_id']} "
            f"(event={r['event_id'][:10]}, token={r['token_id'][:10]}) "
            f"at sell_price={sell:.4f}, realized_pnl=${realized:.2f}. "
            f"This should be rare — crash after SELL fill, before close_position.",
        )
