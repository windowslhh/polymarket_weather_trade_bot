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

    # Settlement check only (not full rebalance)
    scheduler.add_job(
        rebalancer.run_settlement_only,
        "interval",
        minutes=15,
        id="settlement_check",
        name="Settlement check",
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
        "Scheduler configured: rebalance every %d min, settlement check every 15 min",
        config.scheduling.rebalance_interval_minutes,
    )
    return scheduler
