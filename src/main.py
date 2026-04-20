"""Main entry point for the Polymarket Weather Trading Bot."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import threading

from src.alerts import Alerter
from src.config import load_config
from src.execution.executor import Executor
from src.markets.clob_client import ClobClient
from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker
from src.scheduler.jobs import setup_scheduler
from src.strategy.rebalancer import Rebalancer
from src.weather.historical import build_all_distributions
from src.weather.metar import DailyMaxTracker
from src.weather.settlement import (
    BULK_UNRESOLVED_THRESHOLD,
    check_station_alignment,
    is_bulk_unresolved,
    validate_station_config,
)


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


async def run(args: argparse.Namespace) -> None:
    config = load_config()
    config.dry_run = args.dry_run
    config.paper = args.paper

    logger = logging.getLogger(__name__)

    if config.dry_run:
        logger.info("*** DRY RUN MODE — signals only, no positions recorded ***")
    elif config.paper:
        logger.info("*** PAPER TRADING MODE — simulated fills, positions tracked ***")

    if not config.dry_run and not config.paper and not config.eth_private_key:
        logger.error("ETH_PRIVATE_KEY not set. Use --paper for simulated trading or --dry-run for signal preview.")
        sys.exit(1)

    logger.info("Loaded %d cities from config", len(config.cities))

    # Hoisted Alerter — lives for the duration of ``run()`` so any
    # fire-and-forget webhook task kicked off during startup (e.g. the
    # O1 bulk-UNRESOLVED alarm) keeps its strong reference through the
    # Alerter instance's ``_pending_tasks`` set.  A throwaway
    # ``Alerter(url).send(...)`` pattern would let the instance (and
    # thus the task) get garbage-collected the instant the await
    # returns, which CPython's refcount model will do deterministically
    # — the webhook POST never completes.
    alerter = Alerter(config.alert_webhook_url)

    # Validate settlement station configuration (static — config vs registry)
    mismatches = validate_station_config(config.cities)
    for m in mismatches:
        logger.warning("STATION MISMATCH (static): %s — %s", m.city, m.issue)

    # Live alignment check — pull Gamma events and compare the ICAO in each
    # event's resolutionSource against our config.  If Polymarket silently
    # switched settlement stations for any city, abort startup rather than
    # trade against the wrong data.  Bypass with --skip-station-check only
    # for emergency deploys; UNRESOLVED and NO_EVENT are always warn-only.
    if not args.skip_station_check:
        logger.info("Running live station alignment check...")
        try:
            alignment_issues = await check_station_alignment(config.cities)
        except Exception:
            logger.exception("Live station alignment check failed (skipping)")
            alignment_issues = []
        # GAMMA_ERROR sentinel means the fetch itself failed — log loudly
        # so operators don't mistake a silent skip for an all-clear.
        gamma_errors = [i for i in alignment_issues if i.kind == "GAMMA_ERROR"]
        if gamma_errors:
            logger.warning(
                "STATION ALIGNMENT SKIPPED: Gamma fetch failed; live ICAO check did "
                "NOT run. Proceeding with config as-is — if persistent, investigate "
                "Gamma connectivity before trusting startup.",
            )
        hard_fail = [i for i in alignment_issues if i.kind == "MISMATCH"]
        soft = [i for i in alignment_issues
                if i.kind not in ("MISMATCH", "GAMMA_ERROR")]
        for i in soft:
            logger.warning(
                "STATION %s: %s — config=%s, gamma=%s, event=%s",
                i.kind, i.city, i.config_icao, i.gamma_icao or "<none>", i.event_id or "<none>",
            )
        # O1: escalate to ERROR + webhook when UNRESOLVED covers most of the
        # fleet — the fingerprint of a Polymarket resolutionSource regex
        # break.  logger.error alone only reaches stdout/docker logs; route
        # through Alerter so the operator's webhook (Telegram/Discord/Slack)
        # actually pages them.  No sys.exit: transient Gamma weirdness
        # shouldn't block a deploy, but the critical channel surface matters.
        bulk_unresolved, unresolved_count = is_bulk_unresolved(
            alignment_issues, len(config.cities),
        )
        if bulk_unresolved:
            msg = (
                f"CRITICAL ALIGNMENT ANOMALY: {unresolved_count}/{len(config.cities)} "
                f"cities have UNRESOLVED events (>{BULK_UNRESOLVED_THRESHOLD * 100:.0f}%). "
                "Polymarket's resolutionSource URL format may have changed — "
                "investigate extract_settlement_icao before trusting today's signals."
            )
            logger.error(msg)
            if config.alert_webhook_url:
                await alerter.send("critical", msg)
        if hard_fail:
            for i in hard_fail:
                logger.error(
                    "STATION MISMATCH (live): %s — config=%s but Gamma event uses %s (event=%s)",
                    i.city, i.config_icao, i.gamma_icao, i.event_id,
                )
            logger.error(
                "Refusing to start: %d live ICAO mismatch(es). Fix config.yaml and "
                "src/weather/settlement.py, or override with --skip-station-check.",
                len(hard_fail),
            )
            sys.exit(2)
        if not alignment_issues:
            logger.info("Live station alignment: all clear")
        elif not gamma_errors:
            logger.info(
                "Live station alignment: %d soft issue(s) (no MISMATCHes — OK to proceed)",
                len(alignment_issues),
            )
    else:
        logger.warning("--skip-station-check set: live ICAO alignment not verified")

    # Initialize components
    store = Store(config.db_path)
    await store.initialize()

    # Build empirical forecast error distributions (cached, ~7 day refresh)
    logger.info("Loading forecast error distributions...")
    error_dists = await build_all_distributions(config.cities)
    for city_name, dist in error_dists.items():
        if dist._count > 0:
            logger.info("  %s: %d samples, mean=%.2f\u00b0F, std=%.2f\u00b0F",
                       city_name, dist._count, dist.mean, dist.std)
        else:
            logger.warning("  %s: no historical data, using normal fallback", city_name)

    clob = ClobClient(config)
    portfolio = PortfolioTracker(store)
    executor = Executor(clob, portfolio)
    max_tracker = DailyMaxTracker()
    rebalancer = Rebalancer(config, clob, portfolio, executor, max_tracker, error_dists)

    # Backfill today's METAR history so temperature curves show the full day
    logger.info("Backfilling today's METAR observations...")
    await rebalancer.backfill_today_observations()

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

    # Start scheduler
    scheduler.start()

    # Start web dashboard in background thread (unless --no-web)
    web_thread = None
    if not args.no_web:
        from src.web.app import run_web_server
        port = args.port
        web_thread = threading.Thread(
            target=run_web_server,
            args=(store, rebalancer, config, port),
            daemon=True,
        )
        web_thread.start()
        logger.info("Web dashboard: http://localhost:%d", port)

    logger.info("Bot started. Press Ctrl+C to stop.")

    try:
        await stop_event.wait()
    finally:
        scheduler.shutdown(wait=False)
        await store.close()
        logger.info("Bot stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Weather Trading Bot")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Signal preview only — no positions recorded, no orders placed",
    )
    mode_group.add_argument(
        "--paper",
        action="store_true",
        help="Paper trading — simulated fills, positions tracked, P&L computed (no real money)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="Disable web dashboard",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5001,
        help="Web dashboard port (default: 5001)",
    )
    parser.add_argument(
        "--skip-station-check",
        action="store_true",
        help="Skip live Gamma ICAO alignment check on startup (emergency deploys only)",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
