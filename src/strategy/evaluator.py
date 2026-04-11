"""Core strategy logic: evaluate temperature slots and generate trade signals.

Uses empirical forecast error distributions (from historical data) when available,
falling back to normal distribution approximation otherwise.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from src.config import StrategyConfig
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.weather.historical import ForecastErrorDistribution
from src.strategy.trend import TrendState
from src.weather.models import Forecast, Observation

logger = logging.getLogger(__name__)


def _estimate_no_win_probability_normal(
    distance_f: float,
    confidence_interval_f: float,
) -> float:
    """Fallback: estimate NO win probability using normal distribution.

    Only used when no empirical distribution is available.
    """
    sigma = max(confidence_interval_f, 1.0)
    z = distance_f / sigma
    cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
    return min(cdf, 0.99)


def _slot_distance(slot: TempSlot, forecast_high_f: float) -> float:
    """Calculate the minimum distance from the slot to the forecast high."""
    if slot.temp_lower_f is not None and slot.temp_upper_f is not None:
        if slot.temp_lower_f <= forecast_high_f <= slot.temp_upper_f:
            return 0.0
        return min(abs(forecast_high_f - slot.temp_lower_f), abs(forecast_high_f - slot.temp_upper_f))
    mid = slot.temp_midpoint_f
    return abs(mid - forecast_high_f)


def _estimate_no_win_prob(
    slot: TempSlot,
    forecast: Forecast,
    error_dist: ForecastErrorDistribution | None,
) -> float:
    """Estimate NO win probability using empirical distribution if available."""
    if error_dist is not None and error_dist._count >= 30:
        return error_dist.prob_no_wins(
            slot.temp_lower_f, slot.temp_upper_f, forecast.predicted_high_f,
        )
    # Fallback to normal approximation
    distance = _slot_distance(slot, forecast.predicted_high_f)
    return _estimate_no_win_probability_normal(distance, forecast.confidence_interval_f)


def evaluate_no_signals(
    event: WeatherMarketEvent,
    forecast: Forecast,
    config: StrategyConfig,
    error_dist: ForecastErrorDistribution | None = None,
    trend: TrendState | None = None,
    held_token_ids: set[str] | None = None,
    days_ahead: int = 0,
) -> list[TradeSignal]:
    """Phase 4: Generate BUY NO signals for slots far from forecast.

    When an empirical error distribution is provided, uses it for accurate
    probability estimation. Otherwise falls back to normal approximation.

    Trend state adjusts behavior:
    - SETTLING: tighter EV threshold (only high-confidence trades)
    - BREAKOUT_UP/DOWN: boost signals on the opposite side of the breakout
    """
    signals: list[TradeSignal] = []

    # Adjust EV threshold based on trend and days ahead
    ev_threshold = config.min_no_ev
    if trend == TrendState.SETTLING:
        ev_threshold = config.min_no_ev * 1.5
    # Require higher EV for future markets (forecast less reliable)
    if days_ahead > 0:
        ev_threshold /= (config.day_ahead_ev_discount ** days_ahead)

    for slot in event.slots:
        # Skip already-held slots
        if held_token_ids and slot.token_id_no in held_token_ids:
            continue

        distance = _slot_distance(slot, forecast.predicted_high_f)

        if distance < config.no_distance_threshold_f:
            continue

        if slot.price_no <= 0 or slot.price_no >= 1:
            continue

        # Skip overpriced NO — risk/reward too asymmetric at high prices
        if slot.price_no > config.max_no_price:
            continue

        win_prob = _estimate_no_win_prob(slot, forecast, error_dist)

        # Trend-based probability boost for breakout direction
        if trend == TrendState.BREAKOUT_UP and slot.temp_lower_f is not None:
            # Forecast rising → lower slots become safer NO bets
            if slot.temp_upper_f is not None and slot.temp_upper_f < forecast.predicted_high_f:
                win_prob = min(win_prob * 1.05, 0.99)
        elif trend == TrendState.BREAKOUT_DOWN and slot.temp_upper_f is not None:
            # Forecast falling → upper slots become safer NO bets
            if slot.temp_lower_f is not None and slot.temp_lower_f > forecast.predicted_high_f:
                win_prob = min(win_prob * 1.05, 0.99)

        # EV = win_prob * profit_if_win - (1 - win_prob) * cost_if_lose
        ev = win_prob * (1.0 - slot.price_no) - (1.0 - win_prob) * slot.price_no

        if ev < ev_threshold:
            continue

        signals.append(TradeSignal(
            token_type=TokenType.NO,
            side=Side.BUY,
            slot=slot,
            event=event,
            expected_value=ev,
            estimated_win_prob=win_prob,
        ))

    using = "empirical" if (error_dist and error_dist._count >= 30) else "normal"
    trend_label = trend.value if trend else "none"
    logger.debug(
        "City %s date %s: %d NO signals from %d slots (prob: %s, trend: %s)",
        event.city, event.market_date, len(signals), len(event.slots), using, trend_label,
    )
    return signals


def evaluate_trim_signals(
    event: WeatherMarketEvent,
    forecast: Forecast,
    held_no_slots: list[TempSlot],
    config: StrategyConfig,
    error_dist: ForecastErrorDistribution | None = None,
    entry_prices: dict[str, float] | None = None,
    locked_win_token_ids: set[str] | None = None,
    daily_max_f: float | None = None,
) -> list[TradeSignal]:
    """Generate SELL signals for held NO positions whose EV has decayed.

    Unlike exit signals (which trigger on temperature proximity), trim signals
    fire when the expected value drops below min_trim_ev due to forecast changes.

    NEVER trims locked-win positions — these are guaranteed winners where the
    forecast-based EV is misleading (daily_max already exceeded slot upper).

    Hold-to-settlement bias: only trim if EV is negative. Positions with slightly
    positive EV (between 0 and min_trim_ev) are held since the round-trip spread
    cost of selling and re-entering is often higher than the EV decay.
    """
    signals: list[TradeSignal] = []
    locked_ids = locked_win_token_ids or set()
    ep = entry_prices or {}

    for slot in held_no_slots:
        # NEVER trim locked wins — daily_max already exceeded slot upper,
        # NO is guaranteed to win.  Forecast-based EV is misleading here
        # because it doesn't account for the observed daily maximum.
        if slot.token_id_no in locked_ids:
            logger.debug(
                "TRIM skip (locked win): %s slot %s",
                event.city, slot.outcome_label,
            )
            continue

        # Also protect slots where daily_max currently exceeds upper bound
        # (locked-win condition is true NOW, even if not bought as locked win)
        if daily_max_f is not None and slot.temp_upper_f is not None:
            if daily_max_f > slot.temp_upper_f:
                logger.debug(
                    "TRIM skip (daily_max %.1f > upper %.1f): %s slot %s",
                    daily_max_f, slot.temp_upper_f, event.city, slot.outcome_label,
                )
                continue

        win_prob = _estimate_no_win_prob(slot, forecast, error_dist)

        # Use ENTRY price for EV calculation, not current market price.
        # When the market moves in our favor (NO price rises because market
        # agrees NO will win), current-price EV goes negative even though
        # holding to settlement is still highly profitable at our entry cost.
        # Entry-price EV answers: "is this position still a good hold?"
        price_for_ev = ep.get(slot.token_id_no, slot.price_no)
        ev = win_prob * (1.0 - price_for_ev) - (1.0 - win_prob) * price_for_ev

        # Only trim if EV has gone clearly negative — hold positions with marginal positive EV
        # to avoid losing round-trip spread costs
        if ev < -config.min_trim_ev:
            signals.append(TradeSignal(
                token_type=TokenType.NO,
                side=Side.SELL,
                slot=slot,
                event=event,
                expected_value=ev,
                estimated_win_prob=win_prob,
            ))
            logger.info(
                "TRIM signal: %s slot %s EV=%.4f < -%.4f (win_prob=%.2f, entry_price=%.3f)",
                event.city, slot.outcome_label, ev, config.min_trim_ev, win_prob, price_for_ev,
            )

    return signals


def evaluate_locked_win_signals(
    event: WeatherMarketEvent,
    daily_max_f: float | None,
    config: StrategyConfig,
    held_token_ids: set[str] | None = None,
    days_ahead: int = 0,
) -> list[TradeSignal]:
    """Generate BUY NO signals for slots where NO is guaranteed to win.

    When today's observed daily max exceeds a slot's upper bound, the actual
    high temperature is already above that range, so the slot's YES cannot win.
    NO is a locked win — the daily max can only go up, never down.

    Rules:
    - Only same-day markets (days_ahead == 0)
    - daily_max_f must exist
    - Range slot [L, U]: locked if daily_max_f > U
    - "Below X°F" slot (lower=None, upper=X): locked if daily_max_f > X
    - "≥X°F" slot (upper=None): daily_max >= L means YES wins, so NO loses — SKIP
    - Skip already-held tokens
    - Skip if price_no <= 0 or >= 1
    """
    if daily_max_f is None or days_ahead > 0:
        return []

    if not config.enable_locked_wins:
        return []

    signals: list[TradeSignal] = []
    for slot in event.slots:
        # Skip already held
        if held_token_ids and slot.token_id_no in held_token_ids:
            continue

        if slot.price_no <= 0 or slot.price_no >= 1:
            continue

        # Determine if NO is locked win
        is_locked = False

        if slot.temp_upper_f is not None:
            # Range slot [L, U] or "Below U" slot (L=None)
            # daily_max > upper_bound → actual high already exceeded this range → NO wins
            if daily_max_f > slot.temp_upper_f:
                is_locked = True
        # "≥X°F" slot (upper=None, lower set): daily_max >= lower means YES wins → skip
        # If daily_max < lower, it's not locked yet (temp could still rise)
        # → no locked win either way for open-upper slots

        if not is_locked:
            continue

        # Reject locked wins where NO price is too high — thin margin gets
        # eaten by fees.  E.g. $0.97 → only $0.03 profit per share, ~1% ROI
        # after fees.  Cap at 0.90 to ensure at least ~10% gross return.
        if slot.price_no > 0.90:
            logger.debug(
                "LOCKED WIN skip (price %.3f > 0.90): %s slot %s — margin too thin",
                slot.price_no, event.city, slot.outcome_label,
            )
            continue

        # Locked win: near-certain probability (0.99), compute EV
        win_prob = 0.99
        ev = win_prob * (1.0 - slot.price_no) - (1.0 - win_prob) * slot.price_no

        if ev <= 0:
            continue

        signal = TradeSignal(
            token_type=TokenType.NO,
            side=Side.BUY,
            slot=slot,
            event=event,
            expected_value=ev,
            estimated_win_prob=win_prob,
            is_locked_win=True,
        )
        signals.append(signal)

        logger.info(
            "LOCKED WIN: %s slot %s (daily max %.1f > upper %.1f), EV=%.4f",
            event.city, slot.outcome_label, daily_max_f,
            slot.temp_upper_f, ev,
        )

    return signals


def evaluate_exit_signals(
    event: WeatherMarketEvent,
    observation: Observation | None,
    daily_max_f: float | None,
    held_no_slots: list[TempSlot],
    config: StrategyConfig,
    trend: TrendState | None = None,
    days_ahead: int = 0,
    forecast: Forecast | None = None,
    error_dist: ForecastErrorDistribution | None = None,
    hours_to_settlement: float | None = None,
) -> list[TradeSignal]:
    """Three-layer hybrid exit logic for held NO positions.

    Layer 1 — Locked-win protection:
        If daily_max > slot.upper_bound, NO is a guaranteed winner → never exit.

    Layer 2 — EV-based exit:
        When temperature approaches the slot (distance < exit_threshold),
        re-compute current EV using the closer of daily_max and forecast.
        If EV is still positive → HOLD; if negative → SELL.

    Layer 3 — Pre-settlement force exit:
        If within force_exit_hours of settlement AND distance < exit_threshold
        → force SELL regardless of EV (avoid resolution risk).

    Only applies to same-day markets (days_ahead == 0).
    """
    if observation is None or daily_max_f is None:
        return []

    if days_ahead > 0:
        return []

    # Compute exit distance threshold based on trend
    # Use tighter multipliers to avoid premature exits on NO positions.
    # NO wins when temp does NOT land in the slot, so even 3-4°F distance
    # is still a safe position — only exit when truly threatened.
    exit_distance = config.no_distance_threshold_f * 0.25
    if trend == TrendState.STABLE:
        exit_distance = config.no_distance_threshold_f * 0.3
    elif trend in (TrendState.BREAKOUT_UP, TrendState.BREAKOUT_DOWN):
        exit_distance = config.no_distance_threshold_f * 0.2

    signals: list[TradeSignal] = []
    for slot in held_no_slots:
        # ── Layer 1: Locked-win protection ──
        # If daily_max already exceeded the slot's upper bound, NO wins for certain.
        # Never exit a guaranteed winner.
        if slot.temp_upper_f is not None and daily_max_f > slot.temp_upper_f:
            logger.debug(
                "EXIT skip (locked win): %s slot %s — daily max %.1f > upper %.1f",
                event.city, slot.outcome_label, daily_max_f, slot.temp_upper_f,
            )
            continue

        distance = _slot_distance(slot, daily_max_f)

        if distance >= exit_distance:
            continue  # still far enough, no exit needed

        # ── Layer 2: EV-based exit ──
        # Re-compute current EV using the more conservative reference
        # (the closer of daily_max and forecast) for distance/probability.
        win_prob = 0.0
        ev = 0.0
        if forecast is not None:
            # Use daily_max as the reference since it represents the worst case
            # (daily max can only go up, so it's the tighter bound)
            win_prob = _estimate_no_win_prob(slot, forecast, error_dist)

            # Also compute probability using daily_max as reference point
            dist_to_max = _slot_distance(slot, daily_max_f)
            wp_from_max = _estimate_no_win_probability_normal(
                dist_to_max, forecast.confidence_interval_f,
            )
            # Take the lower (more conservative) win probability
            win_prob = min(win_prob, wp_from_max)
            ev = win_prob * (1.0 - slot.price_no) - (1.0 - win_prob) * slot.price_no

            if ev >= 0:
                # Positive EV → hold (unless Layer 3 overrides)
                # ── Layer 3: Pre-settlement force exit ──
                if (hours_to_settlement is not None
                        and hours_to_settlement <= config.force_exit_hours
                        and distance < exit_distance):
                    signals.append(TradeSignal(
                        token_type=TokenType.NO,
                        side=Side.SELL,
                        slot=slot,
                        event=event,
                        expected_value=ev,
                        estimated_win_prob=win_prob,
                    ))
                    logger.info(
                        "FORCE EXIT: %s slot %s — %.1fh to settlement, distance %.1f°F, EV=%.4f",
                        event.city, slot.outcome_label, hours_to_settlement, distance, ev,
                    )
                else:
                    logger.debug(
                        "EXIT hold (positive EV): %s slot %s — distance %.1f, EV=%.4f",
                        event.city, slot.outcome_label, distance, ev,
                    )
                continue

        # EV is negative (or no forecast available) → sell
        signals.append(TradeSignal(
            token_type=TokenType.NO,
            side=Side.SELL,
            slot=slot,
            event=event,
            expected_value=ev,
            estimated_win_prob=win_prob,
        ))
        logger.info(
            "EXIT signal: %s slot %s — daily max %.1f°F, distance %.1f°F, EV=%.4f",
            event.city, slot.outcome_label, daily_max_f, distance, ev,
        )

    return signals
