"""Hourly rebalance orchestrator — the main strategy loop."""
from __future__ import annotations

import logging
from datetime import date

from src.config import AppConfig
from src.execution.executor import Executor
from src.markets.clob_client import ClobClient
from src.markets.discovery import discover_weather_markets
from src.markets.models import TradeSignal
from src.portfolio.tracker import PortfolioTracker
from src.strategy.evaluator import evaluate_exit_signals, evaluate_no_signals, evaluate_yes_signals
from src.strategy.sizing import compute_size
from src.weather.forecast import get_forecasts_batch
from src.weather.metar import DailyMaxTracker, get_latest_metar

logger = logging.getLogger(__name__)


class Rebalancer:
    """Orchestrates the full rebalance cycle across all cities."""

    def __init__(
        self,
        config: AppConfig,
        clob: ClobClient,
        portfolio: PortfolioTracker,
        executor: Executor,
        max_tracker: DailyMaxTracker | None = None,
    ) -> None:
        self._config = config
        self._clob = clob
        self._portfolio = portfolio
        self._executor = executor
        self._max_tracker = max_tracker or DailyMaxTracker()

    async def run(self) -> list[TradeSignal]:
        """Execute one full rebalance cycle.

        Returns all trade signals generated (for logging/monitoring).
        """
        logger.info("=== Starting rebalance cycle ===")

        # Check circuit breaker
        daily_pnl = await self._portfolio.get_daily_pnl(date.today())
        if daily_pnl is not None and daily_pnl < -self._config.strategy.daily_loss_limit_usd:
            logger.warning("Circuit breaker triggered: daily P&L = $%.2f", daily_pnl)
            return []

        # 1. Discover active markets
        events = await discover_weather_markets(self._config.cities)
        if not events:
            logger.info("No active weather markets found")
            return []
        logger.info("Found %d active weather events", len(events))

        # 2. Fetch forecasts for all cities with active markets
        active_cities = {e.city for e in events}
        city_configs = [c for c in self._config.cities if c.name in active_cities]
        forecasts = await get_forecasts_batch(city_configs)
        logger.info("Fetched forecasts for %d cities", len(forecasts))

        # 3. Fetch METAR observations
        import httpx
        observations: dict[str, float | None] = {}  # city -> daily_max
        async with httpx.AsyncClient(timeout=15) as client:
            for city_cfg in city_configs:
                obs = await get_latest_metar(city_cfg.icao, client)
                if obs:
                    daily_max = self._max_tracker.update(obs)
                    observations[city_cfg.name] = daily_max

        # 4. Evaluate signals for each event
        all_signals: list[TradeSignal] = []
        total_exposure = await self._portfolio.get_total_exposure()

        for event in events:
            forecast = forecasts.get(event.city)
            if not forecast:
                continue

            city_exposure = await self._portfolio.get_city_exposure(event.city)
            daily_max = observations.get(event.city)

            # Phase 4: NO signals
            no_signals = evaluate_no_signals(event, forecast, self._config.strategy)

            # Phase 5: Exit signals for held positions
            held_no_slots = await self._portfolio.get_held_no_slots(event.event_id)
            exit_signals = evaluate_exit_signals(
                event, None, daily_max, held_no_slots, self._config.strategy,
            )

            # Phase 6: YES signals
            yes_signals = evaluate_yes_signals(
                event, forecast, None, daily_max, self._config.strategy,
            )

            # Size all entry signals
            for signal in no_signals + yes_signals:
                size = compute_size(signal, city_exposure, total_exposure, self._config.strategy)
                if size > 0:
                    signal.suggested_size_usd = size
                    all_signals.append(signal)
                    city_exposure += size
                    total_exposure += size

            # Exit signals don't need sizing
            all_signals.extend(exit_signals)

        # 5. Execute trades
        if all_signals:
            logger.info("Generated %d trade signals, executing...", len(all_signals))
            await self._executor.execute_signals(all_signals)
        else:
            logger.info("No trade signals generated")

        # Cleanup old tracking data
        self._max_tracker.cleanup_old()

        logger.info("=== Rebalance cycle complete ===")
        return all_signals
