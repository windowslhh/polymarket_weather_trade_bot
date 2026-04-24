"""Scheduled job definitions for the trading bot."""
from __future__ import annotations

import asyncio
import logging

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.alerts import Alerter
from src.config import AppConfig
from src.strategy.rebalancer import Rebalancer

logger = logging.getLogger(__name__)

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
) -> AsyncIOScheduler:
    """Configure and return the APScheduler with all jobs."""
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

    logger.info(
        "Scheduler configured: rebalance every %d min, settlement+forecast check every 15 min, "
        "METAR sync at :57/:03 (coalesce=%s, grace=%ds)",
        config.scheduling.rebalance_interval_minutes,
        JOB_COALESCE,
        JOB_MISFIRE_GRACE_S,
    )
    return scheduler
