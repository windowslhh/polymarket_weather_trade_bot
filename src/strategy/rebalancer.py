"""Hourly rebalance orchestrator — the main strategy loop."""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import httpx
from dataclasses import replace

from src.alerts import Alerter
from src.config import AppConfig, StrategyConfig, get_strategy_variants
from src.execution.executor import Executor
from src.markets.clob_client import ClobClient
from src.markets.discovery import discover_weather_markets, _parse_temp_bounds
from src.markets.models import TempSlot, TradeSignal, WeatherMarketEvent
from src.portfolio.tracker import PortfolioTracker
from src.strategy.evaluator import (
    _estimate_no_win_prob,
    _slot_distance,
    evaluate_exit_signals,
    evaluate_locked_win_signals,
    evaluate_no_signals,
    evaluate_trim_signals,
)
from src.strategy.calibrator import calibrate_distance_threshold
from src.strategy.sizing import compute_size
from src.strategy.trend import ForecastTrend
from src.weather.forecast import get_forecasts_batch
from src.weather.historical import ForecastErrorDistribution
from src.weather.metar import DailyMaxTracker
from src.weather.models import Observation
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
        self._last_daily_maxes: dict[str, float | None] = {}
        self._last_markets: list[dict] = []

        # Exit cooldown: {token_id: exit_datetime} to prevent BUY→EXIT→BUY churn
        self._recent_exits: dict[str, datetime] = {}
        self._last_price_source: str = "gamma"
        self._last_unrealized: float = 0.0
        self._last_gamma_prices: dict[str, float] = {}

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
            "daily_maxes": self._last_daily_maxes,
            "price_source": self._last_price_source,
        }

    def get_gamma_prices(self) -> dict[str, float]:
        """Public accessor for latest Gamma prices (for web dashboard)."""
        return self._last_gamma_prices

    # ── Shared helpers ───────────────────────────────────────────────

    async def _fetch_observations(
        self, city_configs: list,
    ) -> tuple[dict[str, float | None], dict[str, Observation]]:
        """Fetch METAR observations and update DailyMaxTracker.

        Returns (daily_maxes, city_observations) dicts keyed by city name.
        """
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
                    daily_max, _is_new_high = self._max_tracker.update(metar_obs)
                    daily_maxes[city_cfg.name] = daily_max
                    city_observations[city_cfg.name] = metar_obs
        return daily_maxes, city_observations

    async def refresh_metar(self) -> None:
        """Lightweight METAR-only refresh — synced to station reporting times.

        Runs at :57 and :03 past each hour to align with routine METAR reports
        (issued ~:51-:53). Only updates DailyMaxTracker and dashboard state;
        does NOT evaluate signals or execute trades.
        """
        try:
            city_configs = self._config.cities
            daily_maxes, _ = await self._fetch_observations(city_configs)
            self._last_daily_maxes = dict(daily_maxes)
            updated = sum(1 for v in daily_maxes.values() if v is not None)
            logger.info("METAR refresh: %d/%d cities updated", updated, len(city_configs))
        except Exception:
            logger.exception("METAR refresh failed")

    # ── Settlement + position check ──────────────────────────────────

    async def run_settlement_only(self) -> None:
        """Lightweight settlement check — runs every 15 min, no trading."""
        try:
            settlement_results = await check_settlements(self._portfolio.store)
            for sr in settlement_results:
                await self._alerter.send(
                    "info",
                    f"Settlement: {sr.city} → {sr.winning_slot[:30]} | "
                    f"{sr.positions_settled} positions | P&L=${sr.total_pnl:.2f}",
                )
        except Exception:
            logger.exception("Settlement check failed")

    async def run_position_check(self) -> list[TradeSignal]:
        """Lightweight position check — runs alongside settlement every 15 min.

        Unlike a full rebalance cycle, this does NOT:
        - Discover new markets
        - Fetch forecasts
        - Generate new entry (NO) signals

        It ONLY:
        - Fetches latest METAR observations
        - Updates DailyMaxTracker
        - Evaluates locked-win BUY signals for existing events
        - Evaluates EXIT signals for threatened held positions
        - Executes urgent trades (locked wins + exits)
        """
        logger.info("--- Position check start ---")
        signals: list[TradeSignal] = []

        try:
            # Get all open positions grouped by event
            all_positions = await self._portfolio.get_all_open_positions()
            if not all_positions:
                logger.debug("Position check: no open positions")
                return []

            # Identify cities with open positions
            cities_with_positions = {p["city"] for p in all_positions}
            city_configs = [c for c in self._config.cities if c.name in cities_with_positions]

            if not city_configs:
                return []

            # Fetch fresh METAR observations (shared helper)
            daily_maxes, city_observations = await self._fetch_observations(city_configs)

            # Group positions by (event_id, strategy)
            event_strat_positions: dict[tuple[str, str], list[dict]] = defaultdict(list)
            event_meta: dict[str, dict] = {}  # event_id → {city, ...}
            for pos in all_positions:
                key = (pos["event_id"], pos.get("strategy", "B"))
                event_strat_positions[key].append(pos)
                if pos["event_id"] not in event_meta:
                    event_meta[pos["event_id"]] = {"city": pos["city"]}

            now = datetime.now(timezone.utc)
            variants = get_strategy_variants()

            for (event_id, strat_name), positions in event_strat_positions.items():
                meta = event_meta[event_id]
                city = meta["city"]

                daily_max = daily_maxes.get(city)
                observation = city_observations.get(city)
                error_dist = self._error_dists.get(city)

                if not observation or daily_max is None:
                    continue

                # Infer market_date from slot_label (e.g. "...on April 11?")
                # to correctly compute days_ahead for exit logic.
                market_date = date.today()
                sample_label = positions[0].get("slot_label", "")
                m = re.search(r'on (\w+ \d+)\??$', sample_label)
                if m:
                    try:
                        market_date = datetime.strptime(
                            f"{m.group(1)} {date.today().year}", "%B %d %Y"
                        ).date()
                    except ValueError:
                        pass
                days_ahead = (market_date - date.today()).days

                # Build held NO slots from positions
                held_no_slots: list[TempSlot] = []
                held_token_ids: set[str] = set()
                for pos in positions:
                    if pos["token_type"] == "NO" and pos["side"] == "BUY":
                        try:
                            lower, upper = _parse_temp_bounds(pos["slot_label"])
                        except Exception:
                            lower, upper = None, None
                        held_no_slots.append(TempSlot(
                            token_id_yes="",
                            token_id_no=pos["token_id"],
                            outcome_label=pos["slot_label"],
                            temp_lower_f=lower,
                            temp_upper_f=upper,
                            price_no=pos["entry_price"],
                        ))
                        held_token_ids.add(pos["token_id"])

                if not held_no_slots:
                    continue

                # Build strategy config for this variant
                overrides = variants.get(strat_name, {})
                strat_cfg = replace(self._config.strategy, **overrides)

                # Auto-calibrate distance if enabled
                if strat_cfg.auto_calibrate_distance and error_dist is not None:
                    cal_dist = calibrate_distance_threshold(
                        error_dist, strat_cfg.calibration_confidence,
                    )
                    strat_cfg = replace(strat_cfg, no_distance_threshold_f=round(cal_dist))

                # Build a lightweight event object for signal evaluation
                event_obj = WeatherMarketEvent(
                    event_id=event_id,
                    condition_id="",
                    city=city,
                    market_date=market_date,
                    slots=held_no_slots,
                )

                # Evaluate locked-win signals (new BUY opportunities)
                locked_signals = evaluate_locked_win_signals(
                    event_obj, daily_max, strat_cfg, held_token_ids, days_ahead=days_ahead,
                )

                # Evaluate exit signals (urgent sells) — only same-day markets
                exit_signals = evaluate_exit_signals(
                    event_obj, observation, daily_max, held_no_slots, strat_cfg,
                    days_ahead=days_ahead, error_dist=error_dist,
                )

                # P0-3 FIX: Query exposure once before loop, accumulate in-memory
                strat_city_exp = await self._portfolio.get_city_exposure(city, strategy=strat_name)
                strat_total_exp = await self._portfolio.get_total_exposure(strategy=strat_name)

                # Size and tag locked-win signals
                for sig in locked_signals:
                    size = compute_size(sig, strat_city_exp, strat_total_exp, strat_cfg)
                    if size > 0:
                        sig.suggested_size_usd = size
                        sig.strategy = strat_name
                        sig.reason = f"[{strat_name}] LOCKED WIN: daily_max={daily_max:.0f}°F > slot upper, EV={sig.expected_value:.3f}"
                        signals.append(sig)
                        strat_city_exp += size
                        strat_total_exp += size

                # Tag exit signals
                for sig in exit_signals:
                    sig.strategy = strat_name
                    sig.reason = f"[{strat_name}] EXIT: daily max {daily_max:.0f}°F approaching slot" if daily_max else f"[{strat_name}] EXIT: temp approaching"
                    self._recent_exits[sig.token_id] = now
                    signals.append(sig)

            # Execute any urgent trades
            if signals:
                logger.info("Position check: %d urgent signals (locked=%d, exit=%d)",
                           len(signals),
                           sum(1 for s in signals if s.side.value == "BUY"),
                           sum(1 for s in signals if s.side.value == "SELL"))
                await self._executor.execute_signals(signals)
            else:
                logger.debug("Position check: no urgent signals")

        except Exception:
            logger.exception("Position check failed")

        logger.info("--- Position check done ---")
        return signals

    # ── Full rebalance cycle ─────────────────────────────────────────

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

        # Fetch +1 / +2 day forecasts for dashboard (best-effort, don't block trading)
        today = date.today()
        forecasts_d1: dict = {}
        forecasts_d2: dict = {}
        try:
            import asyncio
            fc_d1, fc_d2 = await asyncio.gather(
                get_forecasts_batch(city_configs, today + timedelta(days=1)),
                get_forecasts_batch(city_configs, today + timedelta(days=2)),
            )
            forecasts_d1 = fc_d1
            forecasts_d2 = fc_d2
            logger.info("Fetched +1/+2 day forecasts for dashboard")
        except Exception as exc:
            logger.warning("Failed to fetch multi-day forecasts: %s", exc)

        # Save forecast state for dashboard (today + 2 days)
        self._last_forecasts = {}
        for city, f in forecasts.items():
            entry: dict = {
                "high": f.predicted_high_f, "low": f.predicted_low_f,
                "confidence": f.confidence_interval_f, "source": f.source,
            }
            if city in forecasts_d1:
                entry["high_d1"] = forecasts_d1[city].predicted_high_f
            if city in forecasts_d2:
                entry["high_d2"] = forecasts_d2[city].predicted_high_f
            self._last_forecasts[city] = entry

        # 3. Fetch observations from settlement-consistent stations (shared helper)
        daily_maxes, city_observations = await self._fetch_observations(city_configs)

        # Save daily max temps for dashboard
        self._last_daily_maxes = dict(daily_maxes)

        # 4. Evaluate signals for each event
        all_signals: list[TradeSignal] = []
        cycle_at = datetime.now(timezone.utc).isoformat()

        # Track cumulative new exposure per city across all events in this cycle
        cycle_city_additions: dict[str, float] = {}

        # Build market data for dashboard
        self._last_markets = []

        for event in events:
            forecast = forecasts.get(event.city)
            if not forecast:
                continue

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
            # Compute calibrated distance threshold for dashboard display
            cal_threshold = None
            if self._config.strategy.auto_calibrate_distance and error_dist is not None:
                cal_threshold = round(calibrate_distance_threshold(
                    error_dist, self._config.strategy.calibration_confidence,
                ), 1)
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
                "calibrated_threshold_f": cal_threshold,
                "slots": market_slots,
            })

            # Record edge history for every slot (for backtesting)
            for slot_data in market_slots:
                try:
                    await self._portfolio.insert_edge_snapshot(
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
                    logger.debug("Failed to insert edge snapshot for %s %s", event.city, slot_data["label"])
            try:
                await self._portfolio.flush_edge_batch()
            except Exception:
                logger.debug("Failed to flush edge batch")

            days_ahead = (event.market_date - date.today()).days

            # Collect all evaluated signals across all strategy variants for decision logging
            # P0-1 FIX: store (signal, strat_name, source, strat_cfg, forecast) to avoid stale refs
            all_evaluated_for_event: list[tuple[TradeSignal, str, str, StrategyConfig, float]] = []

            # Run all strategy variants
            variants = get_strategy_variants()
            for strat_name, overrides in variants.items():
                # Build strategy config for this variant
                strat_cfg = replace(self._config.strategy, **overrides)

                # Auto-calibrate distance threshold from historical error data
                if strat_cfg.auto_calibrate_distance and error_dist is not None:
                    cal_dist = calibrate_distance_threshold(
                        error_dist, strat_cfg.calibration_confidence,
                    )
                    strat_cfg = replace(strat_cfg, no_distance_threshold_f=round(cal_dist))

                # Build current prices map from refreshed event slot data
                current_slot_prices: dict[str, float] = {}
                for slot in event.slots:
                    if slot.token_id_no and slot.price_no > 0:
                        current_slot_prices[slot.token_id_no] = slot.price_no

                # Get held positions for this strategy only (with current prices for EV calc)
                held_no_slots = await self._portfolio.get_held_no_slots(
                    event.event_id, strategy=strat_name,
                    current_prices=current_slot_prices,
                )
                held_token_ids = {s.token_id_no for s in held_no_slots}

                # Track exposure per strategy
                db_city_exp = await self._portfolio.get_city_exposure(event.city, strategy=strat_name)
                strat_city_exp = db_city_exp + cycle_city_additions.get(f"{strat_name}:{event.city}", 0.0)
                strat_total_exp = await self._portfolio.get_total_exposure(strategy=strat_name)

                # Count existing positions for this event+strategy
                existing_positions = await self._portfolio.get_open_positions_for_event(
                    event_id=event.event_id, strategy=strat_name,
                )
                event_pos_count = len(existing_positions)

                # Phase 4: NO signals (forecast-based entry)
                no_signals = evaluate_no_signals(
                    event, forecast, strat_cfg, error_dist, trend_state, held_token_ids, days_ahead,
                )

                # Locked-win signals: NO guaranteed on slots where daily max > upper bound
                locked_signals = evaluate_locked_win_signals(
                    event, daily_max, strat_cfg, held_token_ids, days_ahead,
                )

                # Identify locked-win positions and entry prices from DB
                locked_win_token_ids: set[str] = set()
                entry_prices: dict[str, float] = {}
                for pos in existing_positions:
                    if "LOCKED WIN" in (pos.get("buy_reason") or ""):
                        locked_win_token_ids.add(pos["token_id"])
                    if pos.get("token_id") and pos.get("entry_price"):
                        entry_prices[pos["token_id"]] = pos["entry_price"]

                # Phase 5: Exit + Trim signals
                exit_signals = evaluate_exit_signals(
                    event, observation, daily_max, held_no_slots, strat_cfg, trend_state,
                    days_ahead=days_ahead,
                    forecast=forecast, error_dist=error_dist,
                    hours_to_settlement=hours_to_settle,
                )
                trim_signals = evaluate_trim_signals(
                    event, forecast, held_no_slots, strat_cfg, error_dist,
                    entry_prices=entry_prices,
                    locked_win_token_ids=locked_win_token_ids,
                    daily_max_f=daily_max,
                )

                # Size and tag entry signals with strategy label
                # Cap new positions per event to avoid over-concentration
                max_new = max(0, strat_cfg.max_positions_per_event - event_pos_count)
                new_count = 0
                now = datetime.now(timezone.utc)
                cooldown_seconds = strat_cfg.exit_cooldown_hours * 3600
                # Locked wins first (higher priority), then forecast-based NO
                for signal in locked_signals + no_signals:
                    # Check exit cooldown: skip BUY if recently exited this slot
                    tid = signal.token_id
                    exit_time = self._recent_exits.get(tid)
                    if exit_time and (now - exit_time).total_seconds() < cooldown_seconds:
                        signal._cooled_down = True  # type: ignore[attr-defined]
                        continue

                    size = compute_size(signal, strat_city_exp, strat_total_exp, strat_cfg)
                    if size > 0 and new_count < max_new:
                        signal.suggested_size_usd = size
                        signal.strategy = strat_name
                        # Attach buy reason to signal for persistence
                        dist = abs(forecast.predicted_high_f - (signal.slot.temp_midpoint_f or 0))
                        if signal.is_locked_win:
                            signal.reason = f"[{strat_name}] LOCKED WIN: daily_max={daily_max:.0f}°F > slot upper, EV={signal.expected_value:.3f}"
                        else:
                            signal.reason = f"[{strat_name}] NO: dist={dist:.0f}°F, EV={signal.expected_value:.3f}, win={signal.estimated_win_prob:.0%}"
                        all_signals.append(signal)
                        strat_city_exp += size
                        strat_total_exp += size
                        key = f"{strat_name}:{event.city}"
                        cycle_city_additions[key] = cycle_city_additions.get(key, 0.0) + size
                        new_count += 1
                    elif size > 0 and new_count >= max_new:
                        # Mark as capped for decision log
                        signal._capped_by_event_limit = True  # type: ignore[attr-defined]

                # Tag exit/trim signals and record exit times for cooldown
                for signal in exit_signals + trim_signals:
                    signal.strategy = strat_name
                    self._recent_exits[signal.token_id] = now
                    # Attach exit reason to signal for persistence
                    if signal in exit_signals:
                        signal.reason = f"[{strat_name}] EXIT: daily max {daily_max:.0f}°F approaching slot" if daily_max else f"[{strat_name}] EXIT: temp approaching"
                    else:
                        signal.reason = f"[{strat_name}] TRIM: EV decayed to {signal.expected_value:.3f}"
                all_signals.extend(exit_signals)
                all_signals.extend(trim_signals)

                # Accumulate all evaluated signals for this strategy (for decision logging)
                # P0-1 FIX: capture forecast_high and strat_cfg per-signal to avoid stale refs
                forecast_high = forecast.predicted_high_f
                for s in locked_signals:
                    all_evaluated_for_event.append((s, strat_name, "LOCKED", strat_cfg, forecast_high))
                for s in no_signals:
                    all_evaluated_for_event.append((s, strat_name, "NO", strat_cfg, forecast_high))
                for s in exit_signals:
                    all_evaluated_for_event.append((s, strat_name, "EXIT", strat_cfg, forecast_high))
                for s in trim_signals:
                    all_evaluated_for_event.append((s, strat_name, "TRIM", strat_cfg, forecast_high))

            # P0-1 FIX + P2-20 FIX: Use signal.reason when available, reconstruct only for SKIP
            for signal, strat_name, source, sig_strat_cfg, sig_forecast_high in all_evaluated_for_event:
                if signal.reason:
                    # Reason already attached during signal processing
                    reason = signal.reason
                    action = "SELL" if signal.side.value == "SELL" else ("BUY" if signal.suggested_size_usd > 0 else "SKIP")
                elif signal.side.value == "SELL":
                    action = "SELL"
                    reason = f"[{strat_name}] SELL"
                elif signal.side.value == "BUY" and signal.suggested_size_usd > 0:
                    action = "BUY"
                    reason = f"[{strat_name}] BUY: EV={signal.expected_value:.3f}"
                else:
                    action = "SKIP"
                    if getattr(signal, '_capped_by_event_limit', False):
                        reason = f"[{strat_name}] Max positions per event reached ({sig_strat_cfg.max_positions_per_event})"
                    elif getattr(signal, '_cooled_down', False):
                        reason = f"[{strat_name}] Exit cooldown active"
                    elif signal.suggested_size_usd <= 0 and signal.expected_value > 0:
                        reason = f"[{strat_name}] Kelly size < $0.10 (positive EV but too small)"
                    elif signal.expected_value <= 0:
                        reason = f"[{strat_name}] Negative EV ({signal.expected_value:.3f})"
                    else:
                        reason = f"[{strat_name}] Size=0 (exposure limit or Kelly too small)"

                try:
                    await self._portfolio.insert_decision_log(
                        cycle_at=cycle_at, city=event.city, event_id=event.event_id,
                        signal_type=signal.token_type.value, slot_label=signal.slot.outcome_label,
                        forecast_high_f=sig_forecast_high, daily_max_f=daily_max,
                        trend_state=trend_state.value, win_prob=signal.estimated_win_prob,
                        expected_value=signal.expected_value, price=signal.price,
                        size_usd=signal.suggested_size_usd, action=action, reason=reason,
                    )
                except Exception:
                    logger.debug("Failed to insert decision log for %s %s", event.city, signal.slot.outcome_label)

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

        # Collect latest Gamma prices for unrealized P&L and position display
        gamma_prices: dict[str, float] = {}
        for event in events:
            for slot in event.slots:
                if slot.token_id_no and slot.price_no > 0:
                    gamma_prices[slot.token_id_no] = slot.price_no
                if slot.token_id_yes and slot.price_yes > 0:
                    gamma_prices[slot.token_id_yes] = slot.price_yes
        self._last_gamma_prices = gamma_prices

        # Update P&L snapshot (uses CLOB in live mode, Gamma prices in paper mode)
        self._last_unrealized = await self._portfolio.compute_unrealized_pnl(self._clob, gamma_prices)
        await self._portfolio.snapshot_pnl(self._clob, gamma_prices)

        # Cleanup old tracking data
        self._max_tracker.cleanup_old()

        await self._alerter.rebalance_summary(len(all_signals), len(events))
        logger.info("=== Rebalance cycle complete ===")
        return all_signals
