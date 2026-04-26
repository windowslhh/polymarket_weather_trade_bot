"""Scheduled job definitions for the trading bot."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.alerts import Alerter
from src.config import AppConfig
from src.recovery.reconciler import reconcile_pending_orders
from src.strategy.rebalancer import Rebalancer

logger = logging.getLogger(__name__)

# G-1' (2026-04-26): how often to re-run the reconciler post-startup.
# Pre-fix it ran only on boot, so a CLOB write that timed out mid-cycle
# (Python-side asyncio.timeout cancels the await but the underlying
# HTTP POST may still have reached Polymarket and created an order)
# stayed orphaned in `pending` until the next restart.  Worst case:
# exposure mis-reported until ops noticed.  30 min cadence keeps that
# window bounded without competing with the 60-min rebalance.
RECONCILER_INTERVAL_MINUTES = 30

# FIX-06: every job gets coalesce + a generous misfire_grace_time.  Without
# these, if a job is late by any amount (scheduler paused, event loop backed
# up), APScheduler silently drops it — we've lost rebalance cycles in prod
# because of this.  Coalesce collapses queued late fires into one so we don't
# play catch-up either.
JOB_COALESCE = True
JOB_MISFIRE_GRACE_S = 300


async def _prune_table(store, table: str, column: str, cutoff_expr: str) -> int:
    """Delete rows older than `cutoff_expr` (e.g. "-30 days") from `table`.

    Returns the number of rows deleted.  Kept at module level so the test
    suite can exercise it without spinning up a scheduler.
    """
    cursor = await store.db.execute(
        f"DELETE FROM {table} WHERE {column} < datetime('now', ?)",
        (cutoff_expr,),
    )
    await store.db.commit()
    return cursor.rowcount or 0


def setup_scheduler(
    config: AppConfig,
    rebalancer: Rebalancer,
    alerter: Alerter | None = None,
    *,
    query_clob_order: Callable[[dict], Awaitable] | None = None,
    is_paper: bool = False,
) -> AsyncIOScheduler:
    """Configure and return the APScheduler with all jobs.

    G-1' (2026-04-26): ``query_clob_order`` + ``is_paper`` plumb the
    reconciler into a periodic 30-min job so timed-out CLOB writes get
    detected within half an hour rather than waiting for the next
    bot restart.  Both default to ``None``/``False`` so older callers
    (and tests that don't exercise the reconciler path) still work.
    """
    scheduler = AsyncIOScheduler()

    # Main rebalance job (full cycle)
    scheduler.add_job(
        rebalancer.run,
        "interval",
        minutes=config.scheduling.rebalance_interval_minutes,
        id="rebalance",
        name="Rebalance positions",
        max_instances=1,
        coalesce=JOB_COALESCE,
        misfire_grace_time=JOB_MISFIRE_GRACE_S,
    )

    # Settlement check + lightweight position check (every 15 min)
    async def settlement_and_position_check():
        await rebalancer.run_settlement_only()
        await rebalancer.run_position_check()

    scheduler.add_job(
        settlement_and_position_check,
        "interval",
        minutes=15,
        id="settlement_check",
        name="Settlement + position check",
        max_instances=1,
        coalesce=JOB_COALESCE,
        misfire_grace_time=JOB_MISFIRE_GRACE_S,
    )

    # METAR temperature refresh — synced to station reporting times
    # Routine METAR issued at :51-:53, poll at :57 and :03 to catch updates
    scheduler.add_job(
        rebalancer.refresh_metar,
        "cron",
        minute="57,3",
        id="metar_refresh",
        name="METAR temperature sync",
        max_instances=1,
        coalesce=JOB_COALESCE,
        misfire_grace_time=JOB_MISFIRE_GRACE_S,
    )

    # Run initial rebalance 5 seconds after startup
    from datetime import datetime, timedelta, timezone
    scheduler.add_job(
        rebalancer.run,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=5),
        id="rebalance_startup",
        name="Initial rebalance on startup",
        coalesce=JOB_COALESCE,
        misfire_grace_time=JOB_MISFIRE_GRACE_S,
    )

    # FIX-13: nightly prune + WAL checkpoint.  Without this, edge_history
    # grows ~1k rows/day and decision_log grows ~100/day; the SQLite file
    # balloons and WAL-mode journal stays huge.  Keeping 30 days of
    # edge_history and 90 days of decision_log is more than enough for
    # backtesting while capping disk use.
    async def prune_and_checkpoint():
        store = rebalancer._portfolio.store  # tracker.store property
        try:
            edge_deleted = await _prune_table(
                store, "edge_history", "cycle_at", "-30 days",
            )
            dec_deleted = await _prune_table(
                store, "decision_log", "cycle_at", "-90 days",
            )
            # WAL checkpoint reclaims space from the -wal/-shm files.
            await store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await store.db.commit()
            logger.info(
                "Prune+checkpoint: edge_history=%d rows deleted, decision_log=%d rows deleted",
                edge_deleted, dec_deleted,
            )
        except Exception:
            logger.exception("prune_and_checkpoint failed")

    scheduler.add_job(
        prune_and_checkpoint,
        "cron",
        hour=3, minute=0,
        id="prune_job",
        name="Nightly prune + WAL checkpoint",
        max_instances=1,
        coalesce=JOB_COALESCE,
        misfire_grace_time=JOB_MISFIRE_GRACE_S,
    )

    # G-1' (2026-04-26): periodic reconciler.  Skip wiring if the caller
    # didn't provide a query callable AND we're not in paper (the paper
    # path inside reconcile_pending_orders marks every pending row failed
    # without needing a CLOB probe — useful for clearing dry-run residue).
    if query_clob_order is not None or is_paper:
        async def reconcile_periodic():
            """Wrap the startup reconciler with two extra guarantees:

            1. Acquire the rebalancer's cycle lock so the reconciler's
               DB writes don't interleave with a rebalance / position
               check write — both touch positions + orders.
            2. ``exit_on_mismatch=False`` because the runtime must NOT
               sys.exit on a mismatch (that's only safe at startup
               before the scheduler is live).  A runtime mismatch
               sends a critical alert via the reconciler's own path
               and the bot keeps going; ops decides whether to pause.
            """
            try:
                async with rebalancer._cycle_lock:
                    store = rebalancer._portfolio.store
                    await reconcile_pending_orders(
                        store=store,
                        alerter=alerter or Alerter(),
                        query_clob_order=query_clob_order,
                        is_paper=is_paper,
                        exit_on_mismatch=False,
                    )
            except Exception:
                logger.exception("Periodic reconciler failed; continuing")

        scheduler.add_job(
            reconcile_periodic,
            "interval",
            minutes=RECONCILER_INTERVAL_MINUTES,
            id="reconciler_periodic",
            name="Periodic CLOB ↔ DB reconciler",
            max_instances=1,
            coalesce=JOB_COALESCE,
            misfire_grace_time=JOB_MISFIRE_GRACE_S,
        )

    if alerter is not None:
        def _on_job_error(event):  # APScheduler fires this synchronously
            job_id = getattr(event, "job_id", "<unknown>")
            exc = getattr(event, "exception", None)
            kind = "error" if event.code == EVENT_JOB_ERROR else "missed"
            msg = f"Scheduler job {kind}: {job_id} — {exc}" if exc else (
                f"Scheduler job {kind}: {job_id}"
            )
            logger.error(msg)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop (shouldn't happen in production since the
                # scheduler runs on the main asyncio loop); fall back to
                # logger-only so we don't crash the listener itself.
                return
            loop.create_task(alerter.send("critical", msg))

        scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR | EVENT_JOB_MISSED)

    reconciler_msg = (
        f", reconciler every {RECONCILER_INTERVAL_MINUTES} min"
        if (query_clob_order is not None or is_paper) else ""
    )
    logger.info(
        "Scheduler configured: rebalance every %d min, settlement+forecast check every 15 min, "
        "METAR sync at :57/:03%s (coalesce=%s, grace=%ds)",
        config.scheduling.rebalance_interval_minutes,
        reconciler_msg,
        JOB_COALESCE,
        JOB_MISFIRE_GRACE_S,
    )
    return scheduler
