"""Hourly rebalance orchestrator — the main strategy loop."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from src.alerts import Alerter
from src.config import AppConfig
from src.execution.executor import Executor
from src.markets.clob_client import ClobClient
from src.markets.discovery import discover_weather_markets
from src.markets.models import TradeSignal
from src.portfolio.tracker import PortfolioTracker
from src.strategy.evaluator import (
    _estimate_no_win_prob,
    _slot_distance,
    evaluate_exit_signals,
    evaluate_ladder_signals,
    evaluate_no_signals,
    evaluate_trim_signals,
    evaluate_yes_signals,
)
from src.strategy.sizing import compute_size
from src.strategy.trend import ForecastTrend
from src.weather.forecast import get_forecasts_batch
from src.weather.historical import ForecastErrorDistribution
from src.weather.metar import DailyMaxTracker
from src.settlement.settler import check_settlements
from src.weather.settlement import fetch_settlement_temp, validate_station_config

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
        error_distributions: dict[str, ForecastErrorDistribution] | None = None,
    ) -> None:
        self._config = config
        self._clob = clob
        self._portfolio = portfolio
        self._executor = executor
        self._max_tracker = max_tracker or DailyMaxTracker()
        self._error_dists = error_distributions or {}
        self._alerter = Alerter(config.alert_webhook_url)
        self._trend = ForecastTrend()

        # Dashboard state (updated each cycle)
        self._last_signals: list[dict] = []
        self._last_run_at: str | None = None
        self._last_error: str | None = None
        self._last_events_count: int = 0
        self._last_forecasts: dict = {}
        self._last_markets: list[dict] = []
        self._last_price_source: str = "gamma"
        self._last_unrealized: float = 0.0

    def set_error_distributions(self, dists: dict[str, ForecastErrorDistribution]) -> None:
        self._error_dists = dists

    def get_dashboard_state(self) -> dict:
        """Return snapshot of current state for web UI."""
        trends = {}
        for city_cfg in self._config.cities:
            history = self._trend.get_history(city_cfg.name)
            if history:
                trends[city_cfg.name] = self._trend.get_trend(city_cfg.name).value

        return {
            "last_run": self._last_run_at,
            "last_error": self._last_error,
            "active_events": self._last_events_count,
            "last_signals": self._last_signals,
            "trends": trends,
            "unrealized": self._last_unrealized,
            "markets": self._last_markets,
            "forecasts": self._last_forecasts,
            "price_source": self._last_price_source,
        }

    async def run_settlement_only(self) -> None:
        """Lightweight settlement check — runs every 15 min, no trading."""
        try:
            settlement_results = await check_settlements(self._portfolio._store)
            for sr in settlement_results:
                await self._alerter.send(
                    "info",
                    f"Settlement: {sr.city} → {sr.winning_slot[:30]} | "
                    f"{sr.positions_settled} positions | P&L=${sr.total_pnl:.2f}",
                )
        except Exception:
            logger.exception("Settlement check failed")

    async def run(self) -> list[TradeSignal]:
        """Execute one full rebalance cycle."""
        logger.info("=== Starting rebalance cycle ===")
        self._last_error = None

        try:
            return await self._run_cycle()
        except Exception as e:
            self._last_error = str(e)
            raise
        finally:
            self._last_run_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    async def _run_cycle(self) -> list[TradeSignal]:
        # Validate station config (first run only)
        if not hasattr(self, '_station_validated'):
            mismatches = validate_station_config(self._config.cities)
            for m in mismatches:
                logger.warning("Station mismatch: %s — %s", m.city, m.issue)
            self._station_validated = True

        # 0. Settlement check (also runs independently every 15 min)
        await self.run_settlement_only()

        # Check circuit breaker
        daily_pnl = await self._portfolio.get_daily_pnl(date.today())
        if daily_pnl is not None and daily_pnl < -self._config.strategy.daily_loss_limit_usd:
            logger.warning("Circuit breaker triggered: daily P&L = $%.2f", daily_pnl)
            await self._alerter.circuit_breaker(daily_pnl)
            return []

        # 1. Discover active markets
        events = await discover_weather_markets(
            self._config.cities,
            min_volume=self._config.strategy.min_market_volume,
            max_spread=self._config.strategy.max_slot_spread,
            max_days_ahead=self._config.strategy.max_days_ahead,
        )
        self._last_events_count = len(events)
        if not events:
            logger.info("No active weather markets found")
            return []
        logger.info("Found %d active weather events", len(events))

        # 1b. Refresh prices from CLOB (live mode) or keep Gamma prices (paper/dry-run)
        all_token_ids = []
        for event in events:
            for slot in event.slots:
                if slot.token_id_yes:
                    all_token_ids.append(slot.token_id_yes)
                if slot.token_id_no:
                    all_token_ids.append(slot.token_id_no)

        clob_prices = await self._clob.get_prices_batch(all_token_ids)
        if clob_prices:
            refreshed = 0
            for event in events:
                for slot in event.slots:
                    if slot.token_id_yes in clob_prices:
                        slot.price_yes = clob_prices[slot.token_id_yes]
                        refreshed += 1
                    if slot.token_id_no in clob_prices:
                        slot.price_no = clob_prices[slot.token_id_no]
                        refreshed += 1
            self._last_price_source = "clob"
            logger.info("Refreshed %d slot prices from CLOB", refreshed)
        else:
            self._last_price_source = "gamma"
            logger.info("Using Gamma API prices (CLOB unavailable in %s mode)",
                        "paper" if self._config.paper else "dry-run" if self._config.dry_run else "live")

        # 2. Fetch forecasts for all cities with active markets
        active_cities = {e.city for e in events}
        city_configs = [c for c in self._config.cities if c.name in active_cities]
        forecasts = await get_forecasts_batch(city_configs)
        logger.info("Fetched forecasts for %d cities", len(forecasts))

        # Save forecast state for dashboard
        self._last_forecasts = {
            city: {"high": f.predicted_high_f, "low": f.predicted_low_f,
                   "confidence": f.confidence_interval_f, "source": f.source}
            for city, f in forecasts.items()
        }

        # 3. Fetch observations from settlement-consistent stations
        import httpx
        from src.weather.models import Observation
        daily_maxes: dict[str, float | None] = {}
        city_observations: dict[str, Observation] = {}
        async with httpx.AsyncClient(timeout=15) as client:
            for city_cfg in city_configs:
                obs = await fetch_settlement_temp(city_cfg.name, client)
                if obs:
                    metar_obs = Observation(
                        icao=obs.icao, temp_f=obs.temp_f,
                        observation_time=obs.observation_time,
                        raw_metar=obs.raw_data,
                    )
                    daily_max = self._max_tracker.update(metar_obs)
                    daily_maxes[city_cfg.name] = daily_max
                    city_observations[city_cfg.name] = metar_obs

        # 4. Evaluate signals for each event
        all_signals: list[TradeSignal] = []
        total_exposure = await self._portfolio.get_total_exposure()
        cycle_at = datetime.now(timezone.utc).isoformat()

        # Track cumulative new exposure per city across all events in this cycle
        cycle_city_additions: dict[str, float] = {}

        # Build market data for dashboard
        self._last_markets = []

        for event in events:
            forecast = forecasts.get(event.city)
            if not forecast:
                continue

            # Use DB exposure + any new exposure added earlier in this cycle
            db_city_exposure = await self._portfolio.get_city_exposure(event.city)
            city_exposure = db_city_exposure + cycle_city_additions.get(event.city, 0.0)
            daily_max = daily_maxes.get(event.city)
            observation = city_observations.get(event.city)
            error_dist = self._error_dists.get(event.city)

            # Update forecast trend tracker
            self._trend.update(event.city, forecast.predicted_high_f)
            hours_to_settle = None
            if event.end_timestamp:
                hours_to_settle = (event.end_timestamp - datetime.now(timezone.utc)).total_seconds() / 3600
            trend_state = self._trend.get_trend(event.city, hours_to_settle)
            if trend_state.value != "STABLE":
                logger.info("Trend for %s: %s (delta=%.1f°F)", event.city, trend_state.value, self._trend.get_delta(event.city))

            # Build market data for dashboard
            market_slots = []
            for slot in event.slots:
                distance = _slot_distance(slot, forecast.predicted_high_f)
                win_prob = _estimate_no_win_prob(slot, forecast, error_dist)
                ev = win_prob * (1.0 - slot.price_no) - (1.0 - win_prob) * slot.price_no if slot.price_no > 0 else None
                market_slots.append({
                    "label": slot.outcome_label,
                    "price_yes": slot.price_yes,
                    "price_no": slot.price_no,
                    "spread": slot.spread,
                    "distance": distance,
                    "win_prob": win_prob,
                    "ev": ev,
                    "is_forecast_slot": distance < 3,
                })
            self._last_markets.append({
                "city": event.city,
                "market_date": event.market_date.isoformat(),
                "forecast_high": forecast.predicted_high_f,
                "forecast_source": forecast.source,
                "confidence": forecast.confidence_interval_f,
                "trend": trend_state.value,
                "resolution_source": event.resolution_source or "unknown",
                "volume": event.volume,
                "hours_to_settle": round(hours_to_settle, 1) if hours_to_settle else None,
                "slots": market_slots,
            })

            # Record edge history for every slot (for backtesting)
            for slot_data in market_slots:
                try:
                    await self._portfolio._store.insert_edge_snapshot(
                        cycle_at=cycle_at, city=event.city,
                        market_date=event.market_date.isoformat(),
                        slot_label=slot_data["label"],
                        forecast_high_f=forecast.predicted_high_f,
                        price_yes=slot_data["price_yes"],
                        price_no=slot_data["price_no"],
                        win_prob=slot_data["win_prob"],
                        ev=slot_data["ev"] or 0,
                        distance_f=slot_data["distance"],
                        trend_state=trend_state.value,
                    )
                except Exception:
                    pass
            try:
                await self._portfolio._store.flush_edge_batch()
            except Exception:
                pass

            # Get held positions to avoid duplicate buys
            held_no_slots = await self._portfolio.get_held_no_slots(event.event_id)
            held_token_ids = {s.token_id_no for s in held_no_slots}

            # Calculate days ahead for EV discount
            days_ahead = (event.market_date - date.today()).days

            # Phase 4: NO signals (skip already-held slots)
            no_signals = evaluate_no_signals(
                event, forecast, self._config.strategy, error_dist, trend_state, held_token_ids, days_ahead,
            )

            # Phase 4b: Ladder signals (skip already-held slots)
            ladder_signals = evaluate_ladder_signals(
                event, forecast, self._config.strategy, error_dist, held_token_ids, days_ahead,
            )

            # Phase 5: Exit signals
            exit_signals = evaluate_exit_signals(
                event, observation, daily_max, held_no_slots, self._config.strategy, trend_state,
            )

            # Phase 5b: Trim signals
            trim_signals = evaluate_trim_signals(
                event, forecast, held_no_slots, self._config.strategy, error_dist,
            )

            # Phase 6: YES signals
            yes_signals = evaluate_yes_signals(
                event, forecast, observation, daily_max, self._config.strategy,
            )

            # Size entry signals
            for signal in no_signals + ladder_signals + yes_signals:
                size = compute_size(signal, city_exposure, total_exposure, self._config.strategy)
                if size > 0:
                    signal.suggested_size_usd = size
                    all_signals.append(signal)
                    city_exposure += size
                    total_exposure += size
                    cycle_city_additions[event.city] = cycle_city_additions.get(event.city, 0.0) + size

            # Exit and trim signals
            all_signals.extend(exit_signals)
            all_signals.extend(trim_signals)

            # Log decisions to DB
            all_evaluated = no_signals + ladder_signals + exit_signals + trim_signals + yes_signals
            for signal in all_evaluated:
                action = "BUY" if signal.side.value == "BUY" and signal.suggested_size_usd > 0 else \
                         "SELL" if signal.side.value == "SELL" else "SKIP"
                try:
                    await self._portfolio._store.insert_decision_log(
                        cycle_at=cycle_at, city=event.city, event_id=event.event_id,
                        signal_type=signal.token_type.value, slot_label=signal.slot.outcome_label,
                        forecast_high_f=forecast.predicted_high_f, daily_max_f=daily_max,
                        trend_state=trend_state.value, win_prob=signal.estimated_win_prob,
                        expected_value=signal.expected_value, price=signal.price,
                        size_usd=signal.suggested_size_usd, action=action,
                    )
                except Exception:
                    pass  # non-critical

        # Save signal summaries for dashboard
        self._last_signals = [
            {
                "city": s.event.city,
                "token_type": s.token_type.value,
                "side": s.side.value,
                "slot_label": s.slot.outcome_label,
                "expected_value": s.expected_value,
                "estimated_win_prob": s.estimated_win_prob,
                "suggested_size_usd": s.suggested_size_usd,
            }
            for s in all_signals
        ]

        # 5. Execute trades
        if all_signals:
            logger.info("Generated %d trade signals, executing...", len(all_signals))
            await self._executor.execute_signals(all_signals)
        else:
            logger.info("No trade signals generated")

        # Collect latest Gamma prices for unrealized P&L (works in paper mode)
        gamma_prices: dict[str, float] = {}
        for event in events:
            for slot in event.slots:
                if slot.token_id_no and slot.price_no > 0:
                    gamma_prices[slot.token_id_no] = slot.price_no
                if slot.token_id_yes and slot.price_yes > 0:
                    gamma_prices[slot.token_id_yes] = slot.price_yes

        # Update P&L snapshot (uses CLOB in live mode, Gamma prices in paper mode)
        self._last_unrealized = await self._portfolio.compute_unrealized_pnl(self._clob, gamma_prices)
        await self._portfolio.snapshot_pnl(self._clob, gamma_prices)

        # Cleanup old tracking data
        self._max_tracker.cleanup_old()

        await self._alerter.rebalance_summary(len(all_signals), len(events))
        logger.info("=== Rebalance cycle complete ===")
        return all_signals
