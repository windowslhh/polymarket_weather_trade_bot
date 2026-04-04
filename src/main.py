"""Main entry point for the Polymarket Weather Trading Bot."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from src.config import load_config
from src.execution.executor import Executor
from src.markets.clob_client import ClobClient
from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker
from src.scheduler.jobs import setup_scheduler
from src.strategy.rebalancer import Rebalancer
from src.weather.historical import build_all_distributions
from src.weather.metar import DailyMaxTracker
from src.weather.settlement import validate_station_config


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)


async def run(args: argparse.Namespace) -> None:
    config = load_config()
    config.dry_run = args.dry_run

    logger = logging.getLogger(__name__)

    if config.dry_run:
        logger.info("*** DRY RUN MODE — no real orders will be placed ***")

    if not config.dry_run and not config.eth_private_key:
        logger.error("ETH_PRIVATE_KEY not set. Use --dry-run for paper trading.")
        sys.exit(1)

    logger.info("Loaded %d cities from config", len(config.cities))

    # Validate settlement station configuration
    mismatches = validate_station_config(config.cities)
    for m in mismatches:
        logger.warning("STATION MISMATCH: %s — %s", m.city, m.issue)
    if mismatches:
        logger.warning("Fix config.yaml ICAO codes to match Polymarket settlement stations!")

    # Initialize components
    store = Store(config.db_path)
    await store.initialize()

    # Build empirical forecast error distributions (cached, ~7 day refresh)
    logger.info("Loading forecast error distributions...")
    error_dists = await build_all_distributions(config.cities)
    for city_name, dist in error_dists.items():
        if dist._count > 0:
            logger.info("  %s: %d samples, mean=%.2f°F, std=%.2f°F",
                       city_name, dist._count, dist.mean, dist.std)
        else:
            logger.warning("  %s: no historical data, using normal fallback", city_name)

    clob = ClobClient(config)
    portfolio = PortfolioTracker(store)
    executor = Executor(clob, portfolio)
    max_tracker = DailyMaxTracker()
    rebalancer = Rebalancer(config, clob, portfolio, executor, max_tracker, error_dists)

    # Setup scheduler
    scheduler = setup_scheduler(config, rebalancer)

    # Handle graceful shutdown
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def shutdown_handler():
        logger.info("Shutdown signal received, stopping...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler)

    # Start
    scheduler.start()
    logger.info("Bot started. Press Ctrl+C to stop.")

    try:
        await stop_event.wait()
    finally:
        scheduler.shutdown(wait=False)
        await store.close()
        logger.info("Bot stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Weather Trading Bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in paper trading mode (no real orders)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
