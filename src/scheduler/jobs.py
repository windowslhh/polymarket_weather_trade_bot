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

    # Main rebalance job
    scheduler.add_job(
        rebalancer.run,
        "interval",
        minutes=config.scheduling.rebalance_interval_minutes,
        id="rebalance",
        name="Rebalance positions",
        max_instances=1,  # prevent overlapping runs
    )

    # Also run immediately on startup
    scheduler.add_job(
        rebalancer.run,
        "date",  # run once
        id="rebalance_startup",
        name="Initial rebalance on startup",
    )

    logger.info(
        "Scheduler configured: rebalance every %d minutes",
        config.scheduling.rebalance_interval_minutes,
    )
    return scheduler
