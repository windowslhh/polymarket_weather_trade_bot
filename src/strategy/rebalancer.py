"""Hourly rebalance orchestrator — the main strategy loop."""
from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from dataclasses import replace

from src.alerts import Alerter
from src.config import AppConfig, StrategyConfig, get_strategy_variants
from src.execution.executor import Executor
from src.markets.clob_client import ClobClient
from src.markets.discovery import discover_weather_markets, _parse_temp_bounds
from src.markets.models import TempSlot, TradeSignal, WeatherMarketEvent
from src.markets.price_buffer import PriceBuffer
from src.portfolio.tracker import PortfolioTracker
from src.strategy.evaluator import (
    _estimate_no_win_prob,
    _slot_distance,
    evaluate_exit_signals,
    evaluate_locked_win_signals,
    evaluate_no_signals,
    evaluate_trim_signals,
)
from src.strategy.calibrator import calibrate_distance_dynamic, calibrate_distance_threshold
from src.strategy.temperature import is_daily_max_final
from src.strategy.sizing import compute_size
from src.strategy.trend import ForecastTrend
from src.weather.forecast import get_forecasts_batch
from src.weather.historical import ForecastErrorDistribution
from src.weather.metar import DailyMaxTracker, get_today_metar_history
from src.weather.models import Observation
from src.settlement.settler import check_settlements
from src.weather.settlement import fetch_settlement_temp, validate_station_config

logger = logging.getLogger(__name__)


def _effective_city_config(strat_cfg, city: str):
    """Return a copy of strat_cfg with max_exposure_per_city_usd reduced for
    thin-liquidity cities.  Non-thin cities get the original config back
    unchanged (no allocation).  See docs/fixes/2026-04-16-strategy-p0-fixes.md#fix-5.

    FIX-M9: match case-insensitively so a config with "miami" or
    "MIAMI" doesn't silently skip the reduction.  Event cities come
    from Gamma's market titles which are not guaranteed to match the
    capitalization of our config.
    """
    thin_ci = {c.lower() for c in strat_cfg.thin_liquidity_cities}
    if city.lower() in thin_ci:
        reduced = strat_cfg.max_exposure_per_city_usd * strat_cfg.thin_liquidity_exposure_ratio
        return replace(strat_cfg, max_exposure_per_city_usd=reduced)
    return strat_cfg


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
        # Register local timezones so DailyMaxTracker groups by local date
        # Also build city_name → ZoneInfo map for post-peak evaluator logic
        self._city_tz: dict[str, ZoneInfo] = {}
        for city_cfg in config.cities:
            if city_cfg.tz:
                self._max_tracker.register_timezone(city_cfg.icao, city_cfg.tz)
                self._city_tz[city_cfg.name] = ZoneInfo(city_cfg.tz)
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
        self._active_city_configs: list = []  # cities with active Polymarket markets

        # Mutual-exclusion lock: prevents full rebalance and 15-min position check
        # from running concurrently and racing on shared state (_recent_exits,
        # _cached_forecasts, _last_gamma_prices).
        self._cycle_lock = asyncio.Lock()

        # Exit cooldown: {token_id: exit_datetime} to prevent BUY→EXIT→BUY churn.
        # FIX-08: this dict is the in-process cache; the DB row is the source
        # of truth across restarts.  Populated from DB at startup via
        # load_persistent_state() and dual-written on every EXIT/TRIM.
        self._recent_exits: dict[str, datetime] = {}
        self._last_price_source: str = "gamma"
        self._last_unrealized: float = 0.0
        self._last_gamma_prices: dict[str, float] = {}

        # TWAP price buffer: smooths CLOB/Gamma prices across cycles to reduce
        # noise and filter single-point outliers before strategy decisions.
        self._price_buffer = PriceBuffer()

        # Cached Forecast objects — refreshed every 15 min (position check)
        # and every 60 min (full rebalance).  Used by position check for
        # better exit/trim decisions.
        self._cached_forecasts: dict[str, "Forecast"] = {}
        # FIX-01: cache by (market_date, city) so D+1/D+2 events get the
        # correct-day forecast instead of today's by accident.  Populated
        # by run()/backfill, read by run_position_check + dashboard.
        #
        # Known subtlety (H-9): the cache key is UTC today; cities on the
        # west coast hit local midnight 5-8 hours *after* UTC rolls over.
        # During that window (00:00-08:00 UTC), an event whose
        # market_date we've already classified as D+1 can reach TRIM with
        # forecast=None if the previous day's cache entry was evicted
        # before today's got populated.  For the 60-min rebalance this
        # is self-healing (the next cycle refills); for the 15-min
        # position_check it can briefly skip TRIM.  Not fixed here
        # because a city-local keying scheme would require carrying
        # tz/city along every lookup — a larger refactor better done
        # post go-live.  Operators see a "TRIM skip (missing forecast)"
        # log line during the window.
        self._cached_forecasts_by_date: dict[date, dict[str, "Forecast"]] = {}

    def set_error_distributions(self, dists: dict[str, ForecastErrorDistribution]) -> None:
        self._error_dists = dists

    async def load_persistent_state(self) -> None:
        """FIX-08: restore exit cooldowns from DB on startup.

        Without this, a restart reset the cooldown window — a TRIM at
        14:59 followed by a crash at 15:00 could produce a BUY at 15:01,
        exactly the BUY→EXIT→BUY churn the cooldown was designed to
        prevent.  The DB row is the source of truth; the in-process
        `_recent_exits` dict is a read cache populated here.
        """
        try:
            active = await self._portfolio.load_active_exit_cooldowns()
        except Exception:
            logger.exception("load_persistent_state failed — starting with empty cooldowns")
            return
        self._recent_exits.update(active)
        if active:
            logger.info(
                "Exit cooldowns restored: %d token(s) still in cooldown window",
                len(active),
            )

    async def _record_exit_cooldown(self, token_id: str, now: datetime) -> None:
        """Dual-write: RAM cache + DB row.  Keeps the in-cycle fast path
        backed by a persistent source of truth."""
        self._recent_exits[token_id] = now
        try:
            await self._portfolio.record_exit_cooldown(
                token_id=token_id, exit_time=now,
                cooldown_hours=self._config.strategy.exit_cooldown_hours,
            )
        except Exception:
            # The cache write already happened, so the current process
            # still respects the cooldown; we just lose durability on a
            # crash.  Log loudly so the operator knows disk bookkeeping
            # lagged — critical infra issue but not trade-blocking.
            logger.exception("record_exit_cooldown DB write failed for %s", token_id)

    def _forecast_for_event(self, event: WeatherMarketEvent):
        """FIX-01: look up the forecast whose forecast_date matches the event's
        market_date.  Falls back to None (caller must skip the event) rather
        than returning today's forecast for a D+1 event — that was Bug #1.
        """
        return self._cached_forecasts_by_date.get(event.market_date, {}).get(event.city)

    def _cleanup_recent_exits(self) -> None:
        """Remove expired entries from _recent_exits to prevent unbounded growth.

        Uses the maximum configured exit_cooldown_hours across all strategy variants
        as the TTL.  Entries older than this cannot influence any future BUY decision,
        so they are safe to discard.
        """
        variants = get_strategy_variants()
        if variants:
            max_cooldown_h = max(
                self._config.strategy.exit_cooldown_hours,
                *(self._config.strategy.exit_cooldown_hours for _ in variants),
            )
        else:
            max_cooldown_h = self._config.strategy.exit_cooldown_hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_cooldown_h)
        expired = [tid for tid, t in self._recent_exits.items() if t < cutoff]
        for tid in expired:
            del self._recent_exits[tid]
        if expired:
            logger.debug("Cleaned up %d expired exit cooldown entries", len(expired))

    async def backfill_today_observations(self) -> None:
        """Backfill today's METAR history for cities with active markets.

        Fetches up to 24h of historical METAR data and replays it into
        DailyMaxTracker so temperature curves show the full day, not just
        from the moment the bot started.

        Uses _active_city_configs if available (set by first rebalance),
        otherwise discovers markets first to determine which cities to backfill.
        """
        try:
            # Determine which cities have active markets
            if not self._active_city_configs:
                from src.markets.discovery import discover_weather_markets
                events = await discover_weather_markets(
                    self._config.cities,
                    min_volume=self._config.strategy.min_market_volume,
                    max_spread=self._config.strategy.max_slot_spread,
                    max_days_ahead=self._config.strategy.max_days_ahead,
                )
                active_cities = {e.city for e in events}
                self._active_city_configs = [
                    c for c in self._config.cities if c.name in active_cities
                ]
                logger.info("Backfill: discovered %d cities with active markets", len(self._active_city_configs))

            city_configs = self._active_city_configs
            async with httpx.AsyncClient(timeout=15) as client:
                for city_cfg in city_configs:
                    if not city_cfg.tz:
                        continue
                    observations = await get_today_metar_history(
                        city_cfg.icao, city_cfg.tz, client,
                    )
                    local_today = datetime.now(ZoneInfo(city_cfg.tz)).date()
                    for obs in observations:
                        self._max_tracker.update(obs)
                    if observations:
                        self._last_daily_maxes[city_cfg.name] = self._max_tracker.get_max(
                            city_cfg.icao, day=local_today,
                        )
            total = sum(
                len(self._max_tracker.get_observations(c.icao, day=datetime.now(ZoneInfo(c.tz)).date()))
                for c in city_configs if c.tz
            )
            logger.info("Backfilled %d total observations across %d cities", total, len(city_configs))

            # Fetch forecasts so dashboard has data immediately (not after first rebalance)
            import asyncio
            today = datetime.now(timezone.utc).date()
            forecasts = await get_forecasts_batch(city_configs)
            self._cached_forecasts.update(forecasts)

            # Review H-4: return_exceptions=True so one day's failure doesn't
            # drop the other.  Non-dict results are treated as empty.
            fc_results = await asyncio.gather(
                get_forecasts_batch(city_configs, today + timedelta(days=1)),
                get_forecasts_batch(city_configs, today + timedelta(days=2)),
                return_exceptions=True,
            )
            forecasts_d1 = fc_results[0] if isinstance(fc_results[0], dict) else {}
            forecasts_d2 = fc_results[1] if isinstance(fc_results[1], dict) else {}
            for label, r in (("D+1", fc_results[0]), ("D+2", fc_results[1])):
                if isinstance(r, Exception):
                    logger.warning("Backfill: %s forecast fetch failed — %s", label, r)

            # FIX-01: seed by-date cache so run_position_check() can route
            # forecasts by event.market_date even before the first full
            # rebalance cycle has populated it.
            self._cached_forecasts_by_date = {
                today: dict(forecasts),
                today + timedelta(days=1): dict(forecasts_d1),
                today + timedelta(days=2): dict(forecasts_d2),
            }

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
            logger.info("Backfilled forecasts for %d cities (today + D1/D2)", len(forecasts))
        except Exception:
            logger.exception("Failed to backfill METAR history and forecasts")

    def get_dashboard_state(self) -> dict:
        """Return snapshot of current state for web UI."""
        trends = {}
        for city_cfg in self._config.cities:
            history = self._trend.get_history(city_cfg.name)
            if history:
                trends[city_cfg.name] = self._trend.get_trend(city_cfg.name).value

        # Build observation time series per city for temperature dashboard
        # Only include cities with active markets
        observation_series: dict[str, list[tuple[str, float]]] = {}
        active_cfgs = {c.icao: c for c in (self._active_city_configs or self._config.cities)}
        for icao, cfg in active_cfgs.items():
            local_day = (
                datetime.now(ZoneInfo(cfg.tz)).date() if cfg.tz
                else datetime.now(timezone.utc).date()  # FIX-M1
            )
            obs = self._max_tracker.get_observations(icao, day=local_day)
            if obs:
                observation_series[cfg.name] = obs

        # Build per-city forecast error distribution summary for dashboard
        error_dist_summary: dict[str, dict] = {}
        for city_name, dist in self._error_dists.items():
            if dist._count > 0:
                error_dist_summary[city_name] = {
                    "mean_error": round(dist.mean, 2),
                    "std_error": round(dist.std, 2),
                    "samples": dist._count,
                }

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
            "observation_series": observation_series,
            "error_dists": error_dist_summary,
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

        Only fetches for cities with active Polymarket markets.
        """
        try:
            city_configs = self._active_city_configs or self._config.cities
            daily_maxes, _ = await self._fetch_observations(city_configs)
            self._last_daily_maxes.update(daily_maxes)
            updated = sum(1 for v in daily_maxes.values() if v is not None)
            logger.info("METAR refresh: %d/%d cities updated", updated, len(city_configs))
        except Exception:
            logger.exception("METAR refresh failed")

    async def refresh_forecasts(self) -> None:
        """Lightweight forecast refresh for cities with open positions.

        Runs every 15 min alongside position check to reduce forecast latency
        from 60 min (rebalance-only) to 15 min.  Only fetches for cities
        with active positions to minimise API calls.
        """
        try:
            all_positions = await self._portfolio.get_all_open_positions()
            if not all_positions:
                logger.debug("Forecast refresh: no open positions, skipping")
                return

            cities_with_positions = {p["city"] for p in all_positions}
            city_configs = [
                c for c in self._config.cities if c.name in cities_with_positions
            ]
            if not city_configs:
                return

            forecasts = await get_forecasts_batch(city_configs)
            if forecasts:
                self._cached_forecasts.update(forecasts)
                # Review 🟡 #2: keep the by-date cache fresh so the 15-min
                # TRIM in run_position_check reads same-day forecasts up to
                # 15 min old (not up to 60 min from the last full cycle).
                # Also refresh D+1/D+2 so held D+1 positions get fresh exit
                # inputs when TRIM fires for them.
                today = datetime.now(timezone.utc).date()
                # Review H-4: return_exceptions=True so one day failing
                # doesn't bubble up and drop the other.  The per-day
                # isinstance check below filters out exception objects
                # before they enter the by-date cache.
                fc_results = await asyncio.gather(
                    get_forecasts_batch(city_configs, today + timedelta(days=1)),
                    get_forecasts_batch(city_configs, today + timedelta(days=2)),
                    return_exceptions=True,
                )
                fc_d1 = fc_results[0] if isinstance(fc_results[0], dict) else {}
                fc_d2 = fc_results[1] if isinstance(fc_results[1], dict) else {}
                for label, r in (("D+1", fc_results[0]), ("D+2", fc_results[1])):
                    if isinstance(r, Exception):
                        logger.debug("Forecast refresh %s skipped: %s", label, r)
                # Replace today's entry outright (fresher is strictly
                # better); merge-update D+1/D+2 so a transient fetch
                # failure doesn't blow away still-valid cached data.
                self._cached_forecasts_by_date[today] = dict(forecasts)
                for delta, fc in ((1, fc_d1), (2, fc_d2)):
                    if fc:
                        self._cached_forecasts_by_date.setdefault(
                            today + timedelta(days=delta), {},
                        ).update(fc)
                logger.info(
                    "Forecast refresh: %d cities updated (%s)",
                    len(forecasts),
                    ", ".join(f"{c}: {f.predicted_high_f:.0f}°F" for c, f in forecasts.items()),
                )
            else:
                logger.warning("Forecast refresh: no forecasts returned")
        except Exception:
            logger.exception("Forecast refresh failed")

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
        - Generate new entry (NO) signals

        It DOES:
        - Refresh forecasts for cities with open positions (15-min latency)
        - Fetch latest METAR observations
        - Update DailyMaxTracker
        - Evaluate locked-win BUY signals for existing events
        - Evaluate EXIT signals for threatened held positions (with forecast)
        - Execute urgent trades (locked wins + exits)

        Uses _cycle_lock to avoid racing with the full rebalance cycle: both
        coroutines write _recent_exits, _cached_forecasts, and _last_gamma_prices.
        If the lock is already held (full rebalance in progress), skip this cycle
        rather than queuing — position checks run every 15 min so the next one
        will catch any urgent signals.
        """
        if self._cycle_lock.locked():
            logger.info("Position check skipped: full rebalance cycle in progress")
            return []

        logger.info("--- Position check start ---")
        signals: list[TradeSignal] = []

        # FIX-10: once the daily loss limit has fired, the full rebalance
        # stops generating BUYs — but the 15-min position_check used to
        # keep issuing locked-win BUYs because it never consulted daily_pnl.
        # That could deepen a bad day's loss by 15 min at a time.  Now we
        # check the limit up front and flip a flag; TRIM / EXIT / settlement
        # still run, because closing out is ALWAYS allowed.
        _cb_block_buys = False
        try:
            _daily_pnl = await self._portfolio.get_daily_pnl(
                datetime.now(timezone.utc).date(),  # FIX-M1: UTC day
            )
            if (
                _daily_pnl is not None
                and _daily_pnl < -self._config.strategy.daily_loss_limit_usd
            ):
                _cb_block_buys = True
                logger.warning(
                    "Position check circuit breaker: daily P&L = $%.2f — blocking new BUYs "
                    "(TRIM/EXIT/settlement continue)",
                    _daily_pnl,
                )
        except Exception:
            logger.exception("Position check: circuit-breaker check failed; continuing")

        # FIX-11: kill switch also applies to the 15-min cycle — same rule
        # (no new BUYs, but TRIM/EXIT continue) so paused state is honoured
        # whether the operator hits pause before or after the full cycle.
        try:
            if await self._portfolio.store.get_bot_paused():
                _cb_block_buys = True
                logger.warning(
                    "Position check: kill switch engaged — BUYs suppressed; "
                    "TRIM/EXIT continue",
                )
        except Exception:
            logger.exception("Position check: kill-switch read failed; continuing")

        async with self._cycle_lock:
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

                # ── Refresh prices for held tokens ───────────────────────────────
                # Fetch fresh Gamma prices for all open position token IDs so that
                # exit/trim decisions use prices no older than 15 minutes rather
                # than the stale values from the last full rebalance (up to 60 min).
                held_token_ids = list({p["token_id"] for p in all_positions if p.get("token_id")})
                if held_token_ids:
                    try:
                        import json as _json
                        import httpx as _httpx

                        async def _fetch_gamma_for_tokens(tids: list[str]) -> dict[str, float]:
                            prices: dict[str, float] = {}
                            async with _httpx.AsyncClient(timeout=10) as _client:
                                for i in range(0, len(tids), 20):
                                    batch = tids[i:i + 20]
                                    try:
                                        resp = await _client.get(
                                            "https://gamma-api.polymarket.com/markets",
                                            params=[("clob_token_ids", tid) for tid in batch],
                                        )
                                        resp.raise_for_status()
                                        for mkt in resp.json():
                                            toks = mkt.get("clobTokenIds", [])
                                            pxs = mkt.get("outcomePrices", [])
                                            if isinstance(toks, str):
                                                toks = _json.loads(toks)
                                            if isinstance(pxs, str):
                                                pxs = _json.loads(pxs)
                                            for tid, px in zip(toks, pxs):
                                                try:
                                                    prices[tid] = float(px)
                                                except (ValueError, TypeError):
                                                    pass
                                    except Exception:
                                        logger.warning("Gamma price batch fetch failed (batch %d)", i // 20)
                            return prices

                        fresh_gamma = await _fetch_gamma_for_tokens(held_token_ids)
                        if fresh_gamma:
                            # Apply TWAP smoothing (same buffer as main cycle)
                            smoothed_fresh = self._price_buffer.apply_batch(fresh_gamma)
                            self._last_gamma_prices.update(smoothed_fresh)
                            logger.info(
                                "Position check: refreshed prices for %d/%d tokens (TWAP-smoothed)",
                                len(fresh_gamma), len(held_token_ids),
                            )
                        else:
                            logger.warning("Position check: Gamma price refresh returned no prices")
                    except Exception:
                        logger.warning("Position check: price refresh failed, using cached prices")
                # ─────────────────────────────────────────────────────────────────

                # Refresh forecasts + METAR in parallel for minimum latency
                forecast_task = self.refresh_forecasts()
                metar_task = self._fetch_observations(city_configs)
                _, (daily_maxes, city_observations) = await asyncio.gather(
                    forecast_task, metar_task,
                )

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
                skipped_no_obs = 0

                for (event_id, strat_name), positions in event_strat_positions.items():
                    meta = event_meta[event_id]
                    city = meta["city"]
                    # Compute local hour and local date using city timezone.
                    # Must use city-local date (not UTC) for days_ahead to avoid
                    # misclassifying next-day markets as same-day during UTC midnight crossover.
                    city_tz = self._city_tz.get(city)
                    if city_tz:
                        city_now = datetime.now(city_tz)
                        local_hour = city_now.hour
                        local_today = city_now.date()
                    else:
                        local_hour = None
                        # FIX-M1: UTC day when we have no tz for this city.
                        local_today = datetime.now(timezone.utc).date()

                    observation = city_observations.get(city)
                    error_dist = self._error_dists.get(city)

                    # Infer market_date from slot_label (e.g. "...on April 11?")
                    # to correctly compute days_ahead for exit logic.
                    # Must be computed BEFORE daily_max lookup so we query the
                    # tracker for the correct event date.
                    market_date = local_today
                    sample_label = positions[0].get("slot_label", "")
                    m = re.search(r'on (\w+ \d+)\??$', sample_label)
                    if m:
                        try:
                            market_date = datetime.strptime(
                                f"{m.group(1)} {local_today.year}", "%B %d %Y"
                            ).date()
                        except ValueError:
                            logger.warning(
                                "Position check: could not parse date from slot_label %r "
                                "for event %s — defaulting days_ahead=0 (same-day exit logic applies)",
                                sample_label, event_id,
                            )
                    else:
                        logger.debug(
                            "Position check: no date found in slot_label %r "
                            "for event %s — defaulting days_ahead=0",
                            sample_label, event_id,
                        )
                    days_ahead = (market_date - local_today).days

                    # Get daily max for the EVENT's market date (not the latest
                    # observation's date).  Near midnight local time, the live
                    # METAR may map to the next day, returning a stale nighttime
                    # max instead of the actual peak for this event's date.
                    _city_icao_pc = next(
                        (c.icao for c in self._config.cities if c.name == city), None,
                    )
                    daily_max = (
                        self._max_tracker.get_max(_city_icao_pc, day=market_date)
                        if _city_icao_pc else daily_maxes.get(city)
                    )

                    if not observation or daily_max is None:
                        skipped_no_obs += 1
                        continue

                    # Build held NO slots from positions, using cached Gamma prices
                    # when available so exit signals carry the current market price
                    # (not entry price) for accurate realized P&L computation.
                    gamma = self._last_gamma_prices
                    held_no_slots: list[TempSlot] = []
                    held_token_ids_set: set[str] = set()
                    for pos in positions:
                        if pos["token_type"] == "NO" and pos["side"] == "BUY":
                            try:
                                lower, upper = _parse_temp_bounds(pos["slot_label"])
                            except Exception:
                                lower, upper = None, None
                            tid = pos["token_id"]
                            price = gamma.get(tid, pos["entry_price"])
                            held_no_slots.append(TempSlot(
                                token_id_yes="",
                                token_id_no=tid,
                                outcome_label=pos["slot_label"],
                                temp_lower_f=lower,
                                temp_upper_f=upper,
                                price_no=price,
                            ))
                            held_token_ids_set.add(tid)

                    if not held_no_slots:
                        continue

                    # Build strategy config for this variant
                    overrides = variants.get(strat_name, {})
                    strat_cfg = replace(self._config.strategy, **overrides)

                    # FIX-17: respect city_whitelist in position_check too,
                    # so a held position from an earlier, broader variant
                    # doesn't keep generating TRIM/EXIT for a variant that
                    # shouldn't be touching that city anymore.
                    if strat_cfg.city_whitelist and city not in strat_cfg.city_whitelist:
                        continue

                    # Auto-calibrate distance if enabled (k×std dynamic formula).
                    # Review 🟡 #3: pull the forecast matching this event's
                    # market_date so D+1/D+2 positions use the correct-day
                    # ensemble spread rather than today's.
                    if strat_cfg.auto_calibrate_distance and error_dist is not None:
                        _fc = self._cached_forecasts_by_date.get(
                            market_date, {},
                        ).get(city) or self._cached_forecasts.get(city)
                        cal_dist = calibrate_distance_dynamic(
                            error_dist,
                            ensemble_spread_f=_fc.ensemble_spread_f if _fc else None,
                            enable_spread_adjustment=strat_cfg.enable_spread_adjustment,
                        )
                        # FIX-P2-11: math.ceil (conservative rounding) instead
                    # of banker's round — a calibrated threshold of 5.5°F
                    # shouldn't silently become 6 half the time and 5 the
                    # other half; 6 is the right floor for "safe entry".
                    strat_cfg = replace(strat_cfg, no_distance_threshold_f=math.ceil(cal_dist))

                    # Build a lightweight event object for signal evaluation
                    event_obj = WeatherMarketEvent(
                        event_id=event_id,
                        condition_id="",
                        city=city,
                        market_date=market_date,
                        slots=held_no_slots,
                    )

                    # Determine if daily max is final (past peak + stable).
                    # Use market_date for observation series to match daily_max.
                    _dm_final = False
                    if city_tz and daily_max is not None and _city_icao_pc:
                        _obs_series = self._max_tracker.get_observations(
                            _city_icao_pc, day=market_date,
                        )
                        _dm_final = is_daily_max_final(
                            datetime.now(city_tz), _obs_series,
                            post_peak_hour=strat_cfg.post_peak_hour,
                            stability_window_minutes=strat_cfg.stability_window_minutes,
                        )

                    # Evaluate locked-win signals (new BUY opportunities)
                    locked_signals = evaluate_locked_win_signals(
                        event_obj, daily_max, strat_cfg, held_token_ids_set,
                        days_ahead=days_ahead, daily_max_final=_dm_final,
                    )

                    # FIX-01: use the forecast for *this event's* market_date.
                    # Position-check exits on D+1/D+2 positions were using
                    # today's forecast — see Bug #1 Houston 2026-04-17.  Only
                    # skip if we can't find a matching-day forecast and the
                    # event isn't same-day.
                    forecast = self._cached_forecasts_by_date.get(
                        market_date, {},
                    ).get(city)
                    if forecast is None and market_date == local_today:
                        forecast = self._cached_forecasts.get(city)

                    # Pull trend state for this city so exit thresholds are tighter
                    # during breakout periods (trend is updated by the main 60-min cycle
                    # and remains valid for 15-min position checks between rebalances).
                    city_trend = self._trend.get_trend(city)

                    # Evaluate exit signals (urgent sells) — only same-day markets
                    exit_signals = evaluate_exit_signals(
                        event_obj, observation, daily_max, held_no_slots, strat_cfg,
                        trend=city_trend,
                        days_ahead=days_ahead, forecast=forecast, error_dist=error_dist,
                        local_hour=local_hour,
                    )

                    # FIX-02: run TRIM evaluation at 15-min cadence so price-stop
                    # and EV-decay exits fire at finer granularity than the
                    # 60-min full cycle.  Chicago 80-81 (2026-04-15) bled
                    # 72% because TRIM was 60-min-only; a 15-min check would
                    # have caught the price collapse roughly 45 min sooner.
                    # Requires forecast (skipped when absent, e.g. D+1 events
                    # with no tomorrow forecast cached).
                    trim_signals: list[TradeSignal] = []
                    if forecast is not None:
                        entry_prices: dict[str, float] = {}
                        entry_ev_map: dict[str, float] = {}
                        locked_win_token_ids: set[str] = set()
                        for pos in positions:
                            tid = pos.get("token_id")
                            if not tid:
                                continue
                            if "LOCKED WIN" in (pos.get("buy_reason") or ""):
                                locked_win_token_ids.add(tid)
                            if pos.get("entry_price"):
                                entry_prices[tid] = pos["entry_price"]
                            if pos.get("entry_ev") is not None:
                                entry_ev_map[tid] = pos["entry_ev"]
                        trim_signals = evaluate_trim_signals(
                            event_obj, forecast, held_no_slots, strat_cfg, error_dist,
                            entry_prices=entry_prices,
                            locked_win_token_ids=locked_win_token_ids,
                            daily_max_f=daily_max,
                            entry_ev_map=entry_ev_map,
                        )

                    # P0-3 FIX: Query exposure once before loop, accumulate in-memory
                    strat_city_exp = await self._portfolio.get_city_exposure(city, strategy=strat_name)
                    strat_total_exp = await self._portfolio.get_total_exposure(strategy=strat_name)

                    # Fix 5: reduce per-city cap for thin-liquidity cities
                    effective_cfg = _effective_city_config(strat_cfg, city)

                    # Size and tag locked-win signals.  FIX-10: once the
                    # daily loss limit has fired, skip new BUYs here but
                    # keep processing EXIT/TRIM below — closing is safe,
                    # only opening is blocked.
                    if not _cb_block_buys:
                        for sig in locked_signals:
                            size = compute_size(sig, strat_city_exp, strat_total_exp, effective_cfg)
                            if size > 0:
                                sig.suggested_size_usd = size
                                sig.strategy = strat_name
                                sig.reason = f"[{strat_name}] {sig.reason}, EV={sig.expected_value:.3f}"
                                signals.append(sig)
                                strat_city_exp += size
                                strat_total_exp += size

                    # Tag exit signals
                    for sig in exit_signals:
                        sig.strategy = strat_name
                        sig.reason = f"[{strat_name}] EXIT: daily max {daily_max:.0f}°F approaching slot" if daily_max else f"[{strat_name}] EXIT: temp approaching"
                        await self._record_exit_cooldown(sig.token_id, now)
                        signals.append(sig)

                    # Tag TRIM signals — reason was already built by the
                    # evaluator (e.g. "TRIM [price_stop]: 0.645→0.180");
                    # we only prefix the strategy tag.
                    # Review H-8: dedup against EXIT signals.  When both
                    # fire on the same token this cycle, keep EXIT only.
                    exit_tids_pc = {s.token_id for s in exit_signals}
                    for sig in trim_signals:
                        if sig.token_id in exit_tids_pc:
                            continue
                        sig.strategy = strat_name
                        sig.reason = f"[{strat_name}] {sig.reason or 'TRIM'}"
                        await self._record_exit_cooldown(sig.token_id, now)
                        signals.append(sig)

                if skipped_no_obs:
                    logger.warning(
                        "Position check: skipped %d event(s) due to missing METAR observations or daily_max — "
                        "exit/trim signals suppressed for affected positions",
                        skipped_no_obs,
                    )

                # Execute any urgent trades
                if signals:
                    # FIX-02: SELL signals now include both EXIT and TRIM since
                    # TRIM runs in position_check; break them out in the log.
                    n_buy = sum(1 for s in signals if s.side.value == "BUY")
                    n_sell = sum(1 for s in signals if s.side.value == "SELL")
                    n_trim = sum(1 for s in signals if "TRIM" in (s.reason or ""))
                    logger.info(
                        "Position check: %d urgent signals (locked=%d, exit=%d, trim=%d)",
                        len(signals), n_buy, n_sell - n_trim, n_trim,
                    )
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

        # Acquire cycle lock so position check cannot interleave with full rebalance.
        # Both coroutines mutate _recent_exits, _cached_forecasts, _last_gamma_prices;
        # the lock ensures they run sequentially on the shared async event loop.
        async with self._cycle_lock:
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
        daily_pnl = await self._portfolio.get_daily_pnl(
            datetime.now(timezone.utc).date(),  # FIX-M1: UTC day
        )
        if daily_pnl is not None and daily_pnl < -self._config.strategy.daily_loss_limit_usd:
            logger.warning("Circuit breaker triggered: daily P&L = $%.2f", daily_pnl)
            await self._alerter.circuit_breaker(daily_pnl)
            return []

        # FIX-11: check the persistent kill switch.  Paused → skip BUY
        # generation but let TRIM / EXIT / settlement continue (closing
        # is always safe; only opening new exposure is blocked).  The
        # flag is maintained by /api/admin/pause + /api/admin/unpause.
        self._paused_this_cycle = False
        try:
            if await self._portfolio.store.get_bot_paused():
                self._paused_this_cycle = True
                logger.warning(
                    "Kill switch engaged (bot_state.paused=1) — BUYs suppressed "
                    "for this cycle; TRIM/EXIT/settlement continue"
                )
        except Exception:
            logger.exception(
                "Kill-switch check failed; continuing (fail-open is safer "
                "than halting the whole bot on a DB read glitch)",
            )

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

        # 1b. Refresh prices: CLOB (live) cross-validated with Gamma, then TWAP-smoothed.
        all_token_ids = []
        for event in events:
            for slot in event.slots:
                if slot.token_id_yes:
                    all_token_ids.append(slot.token_id_yes)
                if slot.token_id_no:
                    all_token_ids.append(slot.token_id_no)

        # Collect raw Gamma prices from discovery payload
        gamma_raw: dict[str, float] = {}
        for event in events:
            for slot in event.slots:
                if slot.token_id_no and slot.price_no > 0:
                    gamma_raw[slot.token_id_no] = slot.price_no
                if slot.token_id_yes and slot.price_yes > 0:
                    gamma_raw[slot.token_id_yes] = slot.price_yes

        # Try CLOB for live mode; paper/dry-run always returns {}
        clob_prices = await self._clob.get_prices_batch(all_token_ids)

        if clob_prices:
            # Cross-validate CLOB vs Gamma; fall back to Gamma on >5% divergence
            merged = self._price_buffer.cross_validate(clob_prices, gamma_raw)
            self._last_price_source = "clob"
        else:
            merged = gamma_raw
            self._last_price_source = "gamma"
            logger.info("Using Gamma API prices (CLOB unavailable in %s mode)",
                        "paper" if self._config.paper else "dry-run" if self._config.dry_run else "live")

        # Apply TWAP smoothing; returns TWAP or raw price if window is thin
        smoothed = self._price_buffer.apply_batch(merged)

        # Write smoothed prices back into slot objects for signal evaluation
        refreshed = 0
        for event in events:
            for slot in event.slots:
                if slot.token_id_yes in smoothed:
                    slot.price_yes = smoothed[slot.token_id_yes]
                    refreshed += 1
                if slot.token_id_no in smoothed:
                    slot.price_no = smoothed[slot.token_id_no]
                    refreshed += 1
        if clob_prices:
            logger.info("Refreshed %d slot prices from CLOB (TWAP-smoothed)", refreshed)

        # 2. Fetch forecasts for all cities with active markets
        active_cities = {e.city for e in events}
        city_configs = [c for c in self._config.cities if c.name in active_cities]
        self._active_city_configs = city_configs  # save for refresh_metar
        forecasts = await get_forecasts_batch(city_configs)
        self._cached_forecasts.update(forecasts)  # keep cache fresh
        logger.info("Fetched forecasts for %d cities", len(forecasts))

        # FIX-01: fetch +1 / +2 day forecasts and route by event.market_date
        # below.  Previously these were fetched for dashboard display only;
        # the main evaluation loop fell back to today's forecast for
        # tomorrow's events, producing Bug #1 (Houston 2026-04-17).
        today = datetime.now(timezone.utc).date()
        # Review H-4: return_exceptions=True so one day's failure doesn't
        # drop the other.  Non-dict results are treated as empty.
        fc_results = await asyncio.gather(
            get_forecasts_batch(city_configs, today + timedelta(days=1)),
            get_forecasts_batch(city_configs, today + timedelta(days=2)),
            return_exceptions=True,
        )
        forecasts_d1 = fc_results[0] if isinstance(fc_results[0], dict) else {}
        forecasts_d2 = fc_results[1] if isinstance(fc_results[1], dict) else {}
        for label, r in (("D+1", fc_results[0]), ("D+2", fc_results[1])):
            if isinstance(r, Exception):
                logger.warning("Full cycle: %s forecast fetch failed — %s", label, r)
        logger.info("Fetched +1/+2 day forecasts for trading + dashboard")

        # Rebuild the by-date cache on every full cycle so stale entries
        # from yesterday can't leak into today's D+1 routing.
        self._cached_forecasts_by_date = {
            today: dict(forecasts),
            today + timedelta(days=1): dict(forecasts_d1),
            today + timedelta(days=2): dict(forecasts_d2),
        }

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

        # 3. Fetch observations from settlement-consistent stations
        daily_maxes, city_observations = await self._fetch_observations(city_configs)

        # Save daily max temps for dashboard (update, don't overwrite — keep non-market cities)
        self._last_daily_maxes.update(daily_maxes)

        # 4. Evaluate signals for each event
        all_signals: list[TradeSignal] = []
        cycle_at = datetime.now(timezone.utc).isoformat()

        # Track cumulative new exposure per city across all events in this cycle.
        # Key: "{strategy}:{city}" — prevents a strategy's city limit from being
        # double-counted when two events share the same city (e.g. same city, different dates).
        cycle_city_additions: dict[str, float] = {}
        # Track cumulative total new exposure per strategy across ALL events.
        # Without this, strat_total_exp is re-queried from DB each event and
        # misses in-cycle additions, allowing the portfolio total to exceed the limit.
        cycle_total_additions: dict[str, float] = {}

        # Build market data for dashboard
        self._last_markets = []

        for event in events:
            # FIX-01: pick the forecast matching event.market_date, not today.
            # D+1/D+2 events now use tomorrow/day-after forecasts; a missing
            # entry means we couldn't fetch the right-day forecast and must
            # skip the event rather than trade on today's data.
            forecast = self._forecast_for_event(event)
            if not forecast:
                # Fall back to today's if and only if the event is same-day
                # (keeps the pre-FIX-01 behaviour for the common case).
                if event.market_date == today:
                    forecast = forecasts.get(event.city)
                if not forecast:
                    logger.info(
                        "Skipping event %s (%s, date=%s): no forecast for that date",
                        event.event_id[:8], event.city, event.market_date,
                    )
                    continue

            # Get daily max for the EVENT's market date (not the latest observation's date).
            # Near midnight local time, the live METAR may map to the next day, returning
            # a stale/low nighttime max instead of today's actual peak.
            _city_icao_ev = next((c.icao for c in self._config.cities if c.name == event.city), None)
            daily_max = (
                self._max_tracker.get_max(_city_icao_ev, day=event.market_date)
                if _city_icao_ev else daily_maxes.get(event.city)
            )
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
            # Compute calibrated distance thresholds for dashboard display
            cal_threshold = None
            base_threshold = None
            if self._config.strategy.auto_calibrate_distance and error_dist is not None:
                base_threshold = round(calibrate_distance_dynamic(
                    error_dist, enable_spread_adjustment=False,
                ), 1)
                cal_threshold = round(calibrate_distance_dynamic(
                    error_dist,
                    ensemble_spread_f=forecast.ensemble_spread_f if forecast else None,
                    enable_spread_adjustment=self._config.strategy.enable_spread_adjustment,
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
                "base_threshold_f": base_threshold,
                "forecast_bias": round(error_dist.mean, 2) if error_dist is not None else None,
                "ensemble_spread": forecast.ensemble_spread_f,
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
                        ensemble_spread_f=forecast.ensemble_spread_f,
                        # FIX-01: audit trail for "which forecast_date did we evaluate with?"
                        forecast_date=forecast.forecast_date.isoformat(),
                    )
                except Exception:
                    logger.debug("Failed to insert edge snapshot for %s %s", event.city, slot_data["label"])
            try:
                await self._portfolio.flush_edge_batch()
            except Exception:
                logger.debug("Failed to flush edge batch")

            # Compute local hour and local date for post-peak evaluator logic.
            # Use city-local date (not UTC) for days_ahead: during UTC midnight
            # crossover (00:00–06:00 UTC), date.today() is already the next day
            # in UTC while US cities are still on the previous day locally.
            city_tz = self._city_tz.get(event.city)
            if city_tz:
                city_now = datetime.now(city_tz)
                local_hour = city_now.hour
                local_today = city_now.date()
            else:
                local_hour = None
                local_today = date.today()
            days_ahead = (event.market_date - local_today).days

            # Collect all evaluated signals across all strategy variants for decision logging
            # P0-1 FIX: store (signal, strat_name, source, strat_cfg, forecast) to avoid stale refs
            all_evaluated_for_event: list[tuple[TradeSignal, str, str, StrategyConfig, float]] = []
            # Sampled NO rejects per strategy (capped to avoid flooding decision_log).
            # See docs/fixes/2026-04-16-strategy-p0-fixes.md#fix-3.
            _REJECT_SAMPLE_PER_STRATEGY = 3
            all_rejects_for_event: list[tuple[dict, str, float]] = []  # (reject_item, strat_name, forecast_high)

            # Run all strategy variants
            variants = get_strategy_variants()
            for strat_name, overrides in variants.items():
                # Build strategy config for this variant
                strat_cfg = replace(self._config.strategy, **overrides)

                # FIX-17: city_whitelist lets a variant opt into a narrow
                # geography (D' targets LA/Seattle/Denver where historical
                # forecast error is small enough for the 0.08 EV bar to
                # fire without noise).  Empty = all cities allowed.
                if strat_cfg.city_whitelist and event.city not in strat_cfg.city_whitelist:
                    # Review H-6: surface the skip in decision_log so an
                    # operator investigating "why didn't D' fire on NYC?"
                    # can distinguish a whitelist reject from a legitimate
                    # no-signal.  Logged at the event level (no slot) so
                    # we don't flood the table with one row per slot.
                    try:
                        await self._portfolio.insert_decision_log(
                            cycle_at=cycle_at,
                            city=event.city,
                            event_id=event.event_id,
                            signal_type="REJECT",
                            slot_label="",
                            forecast_high_f=forecast.predicted_high_f if forecast else None,
                            daily_max_f=daily_max,
                            trend_state=trend_state.value,
                            win_prob=0.0, expected_value=0.0,
                            price=0.0, size_usd=0.0,
                            action="SKIP",
                            reason=f"[{strat_name}] REJECT: city_not_in_whitelist",
                            strategy=strat_name,
                        )
                    except Exception:
                        logger.debug("Failed to log whitelist skip for %s", event.city)
                    continue

                # Auto-calibrate distance threshold from historical error data (k×std formula)
                if strat_cfg.auto_calibrate_distance and error_dist is not None:
                    cal_dist = calibrate_distance_dynamic(
                        error_dist,
                        ensemble_spread_f=forecast.ensemble_spread_f if forecast else None,
                        enable_spread_adjustment=strat_cfg.enable_spread_adjustment,
                    )
                    # FIX-P2-11: math.ceil (conservative rounding) instead
                    # of banker's round — a calibrated threshold of 5.5°F
                    # shouldn't silently become 6 half the time and 5 the
                    # other half; 6 is the right floor for "safe entry".
                    strat_cfg = replace(strat_cfg, no_distance_threshold_f=math.ceil(cal_dist))

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

                # Track exposure per strategy.
                # strat_total_exp adds cycle_total_additions so that in-cycle BUYs
                # across earlier events are counted against the portfolio total limit,
                # not just against the per-city limit.
                db_city_exp = await self._portfolio.get_city_exposure(event.city, strategy=strat_name)
                strat_city_exp = db_city_exp + cycle_city_additions.get(f"{strat_name}:{event.city}", 0.0)
                db_total_exp = await self._portfolio.get_total_exposure(strategy=strat_name)
                strat_total_exp = db_total_exp + cycle_total_additions.get(strat_name, 0.0)

                # Count existing positions for this event+strategy
                existing_positions = await self._portfolio.get_open_positions_for_event(
                    event_id=event.event_id, strategy=strat_name,
                )
                event_pos_count = len(existing_positions)

                # Phase 4: NO signals (forecast-based entry, post-peak boost)
                # Collect rejected slots for observability (sampled into decision_log below).
                no_rejects: list[dict] = []
                no_signals = evaluate_no_signals(
                    event, forecast, strat_cfg, error_dist, trend_state, held_token_ids, days_ahead,
                    daily_max_f=daily_max, local_hour=local_hour,
                    hours_to_settlement=hours_to_settle,
                    rejects=no_rejects,
                )

                # Sample up to N rejects per strategy for decision_log observability.
                # Deterministic: first-N keeps the earliest rejections in slot order
                # (sufficient for debugging "why no signals generated today?").
                for item in no_rejects[:_REJECT_SAMPLE_PER_STRATEGY]:
                    all_rejects_for_event.append((item, strat_name, forecast.predicted_high_f))

                # Determine if daily max is final (past peak + stable).
                # Use event.market_date to retrieve observation series for the
                # correct day (matches the daily_max lookup above).
                _dm_final_main = False
                if city_tz and daily_max is not None:
                    _city_icao_main = next(
                        (c.icao for c in self._config.cities if c.name == event.city), None,
                    )
                    if _city_icao_main:
                        _obs_series_main = self._max_tracker.get_observations(
                            _city_icao_main, day=event.market_date,
                        )
                        _dm_final_main = is_daily_max_final(
                            datetime.now(city_tz), _obs_series_main,
                            post_peak_hour=strat_cfg.post_peak_hour,
                            stability_window_minutes=strat_cfg.stability_window_minutes,
                        )

                # Locked-win signals: NO guaranteed on slots where daily max > upper bound
                locked_signals = evaluate_locked_win_signals(
                    event, daily_max, strat_cfg, held_token_ids, days_ahead,
                    daily_max_final=_dm_final_main,
                )

                # Identify locked-win positions and entry prices from DB
                locked_win_token_ids: set[str] = set()
                entry_prices: dict[str, float] = {}
                # Fix 4: build entry_ev map so TRIM can use a relative decay gate
                # (current EV < entry_ev * (1 - decay_ratio)) in addition to the
                # absolute floor.  None entries (pre-migration positions) are
                # omitted, which falls back to absolute-only semantics.
                entry_ev_map: dict[str, float] = {}
                for pos in existing_positions:
                    if "LOCKED WIN" in (pos.get("buy_reason") or ""):
                        locked_win_token_ids.add(pos["token_id"])
                    if pos.get("token_id") and pos.get("entry_price"):
                        entry_prices[pos["token_id"]] = pos["entry_price"]
                    if pos.get("token_id") and pos.get("entry_ev") is not None:
                        entry_ev_map[pos["token_id"]] = pos["entry_ev"]

                # Phase 5: Exit + Trim signals (post-peak aware)
                exit_signals = evaluate_exit_signals(
                    event, observation, daily_max, held_no_slots, strat_cfg, trend_state,
                    days_ahead=days_ahead,
                    forecast=forecast, error_dist=error_dist,
                    hours_to_settlement=hours_to_settle,
                    local_hour=local_hour,
                )
                trim_signals = evaluate_trim_signals(
                    event, forecast, held_no_slots, strat_cfg, error_dist,
                    entry_prices=entry_prices,
                    locked_win_token_ids=locked_win_token_ids,
                    daily_max_f=daily_max,
                    entry_ev_map=entry_ev_map,
                )

                # Size and tag entry signals with strategy label
                # Cap new positions per event to avoid over-concentration
                max_new = max(0, strat_cfg.max_positions_per_event - event_pos_count)
                new_count = 0
                now = datetime.now(timezone.utc)
                cooldown_seconds = strat_cfg.exit_cooldown_hours * 3600
                # Fix 5: reduce per-city cap for thin-liquidity cities
                effective_cfg = _effective_city_config(strat_cfg, event.city)
                # FIX-11: when the kill switch is engaged, skip the BUY
                # sizing loop entirely.  EXIT/TRIM further down still run.
                if getattr(self, "_paused_this_cycle", False):
                    locked_signals = []
                    no_signals = []
                # Locked wins first (higher priority), then forecast-based NO
                for signal in locked_signals + no_signals:
                    # Check exit cooldown: skip BUY if recently exited this slot
                    tid = signal.token_id
                    exit_time = self._recent_exits.get(tid)
                    if exit_time and (now - exit_time).total_seconds() < cooldown_seconds:
                        signal._cooled_down = True  # type: ignore[attr-defined]
                        continue

                    size = compute_size(signal, strat_city_exp, strat_total_exp, effective_cfg)
                    if size > 0 and new_count < max_new:
                        signal.suggested_size_usd = size
                        signal.strategy = strat_name
                        # Attach buy reason to signal for persistence
                        dist = _slot_distance(signal.slot, forecast.predicted_high_f)
                        if signal.is_locked_win:
                            signal.reason = f"[{strat_name}] {signal.reason}, EV={signal.expected_value:.3f}"
                        else:
                            signal.reason = f"[{strat_name}] NO: dist={dist:.0f}°F, EV={signal.expected_value:.3f}, win={signal.estimated_win_prob:.0%}"
                        all_signals.append(signal)
                        strat_city_exp += size
                        strat_total_exp += size
                        key = f"{strat_name}:{event.city}"
                        cycle_city_additions[key] = cycle_city_additions.get(key, 0.0) + size
                        # Also track the per-strategy cycle total so that subsequent
                        # events in this cycle see the updated portfolio total, not
                        # the stale DB value from before this cycle started.
                        cycle_total_additions[strat_name] = cycle_total_additions.get(strat_name, 0.0) + size
                        new_count += 1
                    elif size > 0 and new_count >= max_new:
                        # Mark as capped for decision log
                        signal._capped_by_event_limit = True  # type: ignore[attr-defined]

                # Tag exit/trim signals and record exit times for cooldown.
                # TRIM reasons are built by evaluate_trim_signals with the
                # actual firing gate embedded (e.g. "TRIM [price_stop]: …");
                # we only prefix the strategy tag here.  EXIT reasons stay
                # inline because evaluate_exit_signals does not set them.
                for signal in exit_signals + trim_signals:
                    signal.strategy = strat_name
                    await self._record_exit_cooldown(signal.token_id, now)
                    if signal in exit_signals:
                        signal.reason = (
                            f"[{strat_name}] EXIT: daily max {daily_max:.0f}°F approaching slot"
                            if daily_max else f"[{strat_name}] EXIT: temp approaching"
                        )
                    else:
                        signal.reason = f"[{strat_name}] {signal.reason or 'TRIM'}"
                # Review H-8: when the same (event, token, strategy) has
                # both an EXIT and a TRIM in the same cycle, keep EXIT
                # only.  Two reasons: (a) EXIT is driven by live daily_max
                # proximity — a stronger signal than EV decay; (b) sending
                # both produces two SELL orders racing at CLOB for the
                # same shares, half of which will fail with "position
                # closed" and generate noise in the alert channel.
                exit_tids = {s.token_id for s in exit_signals}
                trim_signals = [s for s in trim_signals if s.token_id not in exit_tids]
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
                        strategy=strat_name,
                    )
                except Exception:
                    logger.debug("Failed to insert decision log for %s %s", event.city, signal.slot.outcome_label)

            # Write sampled NO REJECTs for this event (observability).
            # See docs/fixes/2026-04-16-strategy-p0-fixes.md#fix-3.
            for reject_item, r_strat_name, r_forecast_high in all_rejects_for_event:
                try:
                    await self._portfolio.insert_decision_log(
                        cycle_at=cycle_at, city=event.city, event_id=event.event_id,
                        signal_type="NO", slot_label=reject_item.get("slot_label", ""),
                        forecast_high_f=r_forecast_high,
                        daily_max_f=reject_item.get("daily_max_f", daily_max),
                        trend_state=trend_state.value,
                        win_prob=reject_item.get("win_prob", 0.0),
                        expected_value=reject_item.get("expected_value", 0.0),
                        price=reject_item.get("price_no", 0.0),
                        size_usd=0.0, action="REJECT",
                        reason=f"[{r_strat_name}] REJECT: {reject_item.get('reason', 'UNKNOWN')}",
                        strategy=r_strat_name,
                    )
                except Exception:
                    logger.debug("Failed to insert REJECT log for %s", event.city)

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

        # Collect smoothed prices (already in slot objects) for dashboard + P&L
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
        self._cleanup_recent_exits()

        await self._alerter.rebalance_summary(len(all_signals), len(events))
        logger.info("=== Rebalance cycle complete ===")
        return all_signals
