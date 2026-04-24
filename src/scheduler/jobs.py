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
