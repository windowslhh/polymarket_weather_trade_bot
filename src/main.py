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
from src.preflight import run_preflight
from src.recovery.reconciler import reconcile_pending_orders
from src.scheduler.jobs import setup_scheduler
from src.security import load_eth_private_key
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

    # Live mode: resolve the signing key from Keychain, *overriding* any
    # ETH_PRIVATE_KEY that load_config() picked up from .env.  Spec says
    # Keychain is the source of truth; an .env value left over from an
    # old paper deploy must not silently sign on the live path.
    # load_eth_private_key() falls through to ETH_PRIVATE_KEY itself
    # (Keychain miss → env → raise), so non-mac live deploys still work.
    # Paper / dry-run do not touch Keychain at all.
    if not config.dry_run and not config.paper:
        try:
            config.eth_private_key = load_eth_private_key()
        except RuntimeError as exc:
            logger.error("%s", exc)
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

    # FIX-05 + Review Blocker #2: resolve orphaned pending orders before
    # generating any new signals.  In live mode we probe the CLOB via
    # get_trades / get_orders (matched on token_id / side / price /
    # size_shares since py-clob-client doesn't echo our idempotency_key
    # back to Polymarket — see clob_client.probe_order_status).  Paper
    # and dry-run mark all pending rows failed (no live CLOB to probe).
    live_clob_for_probe = ClobClient(config)

    async def _probe(row: dict):
        """Adapter: reconciler hands us the orders row; we unpack into the
        CLOB probe's parameters."""
        from datetime import datetime, timezone
        created_at_epoch = None
        created_at = row.get("created_at")
        if created_at:
            try:
                # SQLite stores ISO8601 naive UTC; pad to UTC and convert.
                dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                # Widen the window by 5 minutes to accommodate clock skew.
                created_at_epoch = int(dt.timestamp()) - 300
            except ValueError:
                pass
        price_usd = float(row.get("price", 0))
        size_usd = float(row.get("size_usd", 0))
        size_shares = size_usd / price_usd if price_usd > 0 else 0.0
        probe = await live_clob_for_probe.probe_order_status(
            token_id=row["token_id"], side=row["side"],
            price=price_usd, size_shares=size_shares,
            created_at_epoch=created_at_epoch,
        )
        # Bridge ProbeResult (clob_client) → ClobOrderStatus (reconciler).
        from src.recovery.reconciler import ClobOrderStatus
        return ClobOrderStatus(
            state=probe.state, order_id=probe.order_id,
            price=probe.price, size=probe.size, message=probe.message,
        )

    is_paper = config.paper or config.dry_run

    # FIX-M7: preflight checks before we touch any state.  DB write /
    # CLOB reachability / webhook ping — any fatal failure exits 2
    # before the scheduler starts, so a broken deploy can't silently
    # run for an hour on a dead dependency.
    await run_preflight(
        store=store,
        clob_client=live_clob_for_probe,
        alerter=alerter,
        webhook_url=config.alert_webhook_url,
        is_paper=config.paper,
        is_dry_run=config.dry_run,
    )

    await reconcile_pending_orders(
        store=store, alerter=alerter,
        query_clob_order=None if is_paper else _probe,
        is_paper=is_paper,
        clob_client=None if is_paper else live_clob_for_probe,
    )

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

    # Redeemer is only meaningful in live mode (paper/dry-run skip the
    # on-chain call entirely).  Build it once here so the settler can
    # pull it from rebalancer; falls through to None if FUNDER_ADDRESS
    # is missing — the settler logs and the dashboard still records
    # settlement P&L, just without the Safe execTransaction.
    redeemer = None
    if not config.dry_run and not config.paper:
        if config.funder_address and config.eth_private_key:
            from src.settlement.redeemer import Redeemer
            redeemer = Redeemer(
                funder_address=config.funder_address,
                private_key=config.eth_private_key,
                clob_client=clob,
            )
            logger.info(
                "Redeemer initialized (funder=%s)",
                config.funder_address[:10] + "..." + config.funder_address[-6:],
            )
        else:
            logger.warning(
                "Redeemer not initialized: live mode but FUNDER_ADDRESS or "
                "ETH_PRIVATE_KEY missing — winners will mark settled in DB "
                "but require manual on-chain redeem.",
            )

    rebalancer = Rebalancer(
        config, clob, portfolio, executor, max_tracker, error_dists,
        redeemer=redeemer,
    )

    # FIX-08: restore persistent exit cooldowns before trading starts.
    # Without this, a crash inside a cooldown window would reset the
    # BUY→EXIT→BUY churn guard.
    await rebalancer.load_persistent_state()

    # Backfill today's METAR history so temperature curves show the full day
    logger.info("Backfilling today's METAR observations...")
    await rebalancer.backfill_today_observations()

    # Setup scheduler — alerter wired in so FIX-06's job_error listener can page.
    scheduler = setup_scheduler(config, rebalancer, alerter=alerter)

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
        # FIX-09: graceful shutdown with 30s budget.  APScheduler.shutdown
        # is sync and returns once its jobstores are closed; we wrap it in
        # wait_for on a thread so a pathologically slow job still gets a
        # deadline.  After the scheduler stops producing new work, drain
        # any in-flight executor trades so we don't cut a CLOB POST
        # mid-flight and leave a pending orders row behind.
        logger.info("Shutdown: stopping scheduler (waiting for jobs)...")
        try:
            await asyncio.wait_for(
                asyncio.to_thread(scheduler.shutdown, wait=True),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Shutdown: scheduler.shutdown(wait=True) exceeded 30s — "
                "forcing; long-running jobs may leave pending state",
            )
            scheduler.shutdown(wait=False)
        drained = await executor.wait_until_idle(timeout=30.0)
        if not drained:
            logger.error(
                "Shutdown: executor did not drain cleanly; pending orders "
                "will be reconciled on next startup (FIX-05)",
            )
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
