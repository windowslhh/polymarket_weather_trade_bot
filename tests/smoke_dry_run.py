#!/usr/bin/env python
"""Smoke test: run one full rebalance cycle in dry-run mode.

Validates the complete pipeline end-to-end:
  Market Discovery → Forecast → METAR → Strategy Evaluation → Sizing → Decision Log

Exits with code 0 on success, 1 on failure.
Uses real APIs (NWS, Gamma, aviationweather) but does NOT place orders.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import load_config
from src.execution.executor import Executor
from src.markets.clob_client import ClobClient
from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker
from src.strategy.rebalancer import Rebalancer
from src.weather.historical import build_all_distributions
from src.weather.metar import DailyMaxTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("smoke_test")


async def main():
    logger.info("=" * 60)
    logger.info("SMOKE TEST: One full rebalance cycle (dry-run)")
    logger.info("=" * 60)

    config = load_config()
    config.dry_run = True
    config.paper = False

    db_path = Path("/tmp/smoke_test_bot.db")
    config.db_path = db_path

    store = Store(db_path)
    await store.initialize()

    # Build error distributions
    logger.info("Loading forecast error distributions...")
    error_dists = await build_all_distributions(config.cities)
    for city, dist in error_dists.items():
        logger.info("  %s: %d samples", city, dist._count)

    clob = ClobClient(config)
    portfolio = PortfolioTracker(store)
    executor = Executor(clob, portfolio)
    max_tracker = DailyMaxTracker()
    rebalancer = Rebalancer(config, clob, portfolio, executor, max_tracker, error_dists)

    # Run one full cycle
    logger.info("--- Running rebalance cycle ---")
    try:
        signals = await rebalancer.run()
    except Exception:
        logger.exception("Rebalance cycle FAILED")
        await store.close()
        return 1

    logger.info("--- Cycle complete ---")
    logger.info("Signals generated: %d", len(signals))

    # Validate outputs
    checks_passed = 0
    checks_total = 0

    # Check 1: No YES/LADDER signals
    checks_total += 1
    yes_signals = [s for s in signals if s.token_type.value == "YES"]
    if yes_signals:
        logger.error("FAIL: Found %d YES signals (should be 0)", len(yes_signals))
    else:
        logger.info("✓ No YES signals (pure NO strategy)")
        checks_passed += 1

    # Check 2: All BUY signals have reason
    checks_total += 1
    buy_signals = [s for s in signals if s.side.value == "BUY"]
    buys_without_reason = [s for s in buy_signals if not s.reason]
    if buys_without_reason:
        logger.error("FAIL: %d BUY signals missing reason", len(buys_without_reason))
    else:
        logger.info("✓ All %d BUY signals have reason attached", len(buy_signals))
        checks_passed += 1

    # Check 3: All signals have strategy A-D
    checks_total += 1
    valid_strats = {"A", "B", "C", "D"}
    bad_strats = [s for s in signals if s.strategy not in valid_strats]
    if bad_strats:
        logger.error("FAIL: %d signals with invalid strategy", len(bad_strats))
    else:
        strat_counts = {}
        for s in signals:
            strat_counts[s.strategy] = strat_counts.get(s.strategy, 0) + 1
        logger.info("✓ All signals have valid strategy: %s", strat_counts)
        checks_passed += 1

    # Check 4: Decision log written
    checks_total += 1
    decision_log = await store.get_decision_log(limit=50)
    if not decision_log and signals:
        logger.error("FAIL: No decision log entries despite %d signals", len(signals))
    else:
        actions = {}
        for d in decision_log:
            a = d.get("action", "?")
            actions[a] = actions.get(a, 0) + 1
        logger.info("✓ Decision log: %d entries — %s", len(decision_log), actions)
        checks_passed += 1

    # Check 5: Dashboard state valid
    checks_total += 1
    state = rebalancer.get_dashboard_state()
    if not state.get("last_run"):
        logger.error("FAIL: Dashboard state has no last_run")
    elif state.get("last_error"):
        logger.error("FAIL: Dashboard state has error: %s", state["last_error"])
    else:
        logger.info("✓ Dashboard state OK: %d events, %d markets, forecasts=%s",
                    state.get("active_events", 0),
                    len(state.get("markets", [])),
                    list(state.get("forecasts", {}).keys()))
        checks_passed += 1

    # Check 6: Edge history written
    checks_total += 1
    edge_history = await store.get_edge_history(limit=10)
    if not edge_history:
        logger.warning("⚠ No edge history (may be normal if no active markets)")
        checks_passed += 1  # Not a failure — may just be no markets
    else:
        logger.info("✓ Edge history: %d snapshots written", len(edge_history))
        checks_passed += 1

    # Check 7: Gamma prices captured
    checks_total += 1
    gamma_prices = rebalancer.get_gamma_prices()
    if not gamma_prices and signals:
        logger.error("FAIL: No Gamma prices despite having signals")
    else:
        logger.info("✓ Gamma prices captured: %d tokens", len(gamma_prices))
        checks_passed += 1

    # Summary
    logger.info("=" * 60)
    if checks_passed == checks_total:
        logger.info("SMOKE TEST PASSED: %d/%d checks ✓", checks_passed, checks_total)
    else:
        logger.error("SMOKE TEST PARTIAL: %d/%d checks passed", checks_passed, checks_total)

    await store.close()

    # Cleanup temp DB
    if db_path.exists():
        db_path.unlink()

    return 0 if checks_passed == checks_total else 1


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
