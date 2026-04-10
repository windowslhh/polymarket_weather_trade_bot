"""Scheduled job definitions for the trading bot."""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.config import AppConfig
from src.strategy.rebalancer import Rebalancer

logger = logging.getLogger(__name__)


def setup_scheduler(config: AppConfig, rebalancer: Rebalancer) -> AsyncIOScheduler:
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
    )

    # Run initial rebalance 5 seconds after startup
    from datetime import datetime, timedelta, timezone
    scheduler.add_job(
        rebalancer.run,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=5),
        id="rebalance_startup",
        name="Initial rebalance on startup",
    )

    logger.info(
        "Scheduler configured: rebalance every %d min, settlement check every 15 min, "
        "METAR sync at :57/:03",
        config.scheduling.rebalance_interval_minutes,
    )
    return scheduler
