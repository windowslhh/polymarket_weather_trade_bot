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


def evaluate_yes_signals(
    event: WeatherMarketEvent,
    forecast: Forecast,
    observation: Observation | None,
    daily_max_f: float | None,
    config: StrategyConfig,
) -> list[TradeSignal]:
    """Phase 6: Generate BUY YES signals when real-time data confirms a slot.

    Only triggers when:
    1. We have a real-time observation
    2. The daily max temperature falls within a slot's range
    3. Enough time has passed (temperature unlikely to change dramatically)
    4. The YES price is still undervalued
    """
    if observation is None or daily_max_f is None:
        return []

    if event.end_timestamp is None:
        return []

    now = datetime.now(timezone.utc)
    hours_remaining = (event.end_timestamp - now).total_seconds() / 3600

    if hours_remaining > 3:
        return []

    signals: list[TradeSignal] = []
    for slot in event.slots:
        lower = slot.temp_lower_f if slot.temp_lower_f is not None else -999
        upper = slot.temp_upper_f if slot.temp_upper_f is not None else 999

        if not (lower <= daily_max_f <= upper):
            continue

        if slot.price_yes <= 0 or slot.price_yes >= 1:
            continue

        if hours_remaining <= 1:
            est_prob = 0.92
        elif hours_remaining <= 2:
            est_prob = 0.85
        else:
            est_prob = 0.75

        if est_prob < config.yes_confirmation_threshold:
            continue

        ev = est_prob * (1.0 - slot.price_yes) - (1.0 - est_prob) * slot.price_yes
        if ev <= 0:
            continue

        signals.append(TradeSignal(
            token_type=TokenType.YES,
            side=Side.BUY,
            slot=slot,
            event=event,
            expected_value=ev,
            estimated_win_prob=est_prob,
        ))

    return signals


def evaluate_ladder_signals(
    event: WeatherMarketEvent,
    forecast: Forecast,
    config: StrategyConfig,
    error_dist: ForecastErrorDistribution | None = None,
    held_token_ids: set[str] | None = None,
    days_ahead: int = 0,
) -> list[TradeSignal]:
    """Generate BUY NO signals for slots near the forecast using ladder/围网 strategy.

    Complements evaluate_no_signals which only targets distant slots (distance > threshold).
    The ladder covers the zone within the threshold where NO prices are higher but
    still offer positive EV based on empirical distributions.

    Slots are sorted by distance from forecast; those within ± ladder_width of the
    center slot are considered.
    """
    if config.ladder_width <= 0:
        return []

    # Sort slots by distance to forecast
    sorted_slots = sorted(
        event.slots,
        key=lambda s: _slot_distance(s, forecast.predicted_high_f),
    )

    # Find the center (closest to forecast) and select ladder range
    ladder_slots = sorted_slots[:config.ladder_width * 2 + 1]

    signals: list[TradeSignal] = []
    for slot in ladder_slots:
        # Skip already-held slots
        if held_token_ids and slot.token_id_no in held_token_ids:
            continue

        distance = _slot_distance(slot, forecast.predicted_high_f)

        # Skip center slots — too close to forecast, high risk of loss
        if distance < config.ladder_min_distance_f:
            continue

        # Skip slots already covered by standard no_signals (far enough)
        if distance >= config.no_distance_threshold_f:
            continue

        # Only buy NO in ladder (not YES)
        if slot.price_no <= 0 or slot.price_no >= 1:
            continue

        # Skip overpriced NO (risk/reward too asymmetric)
        if slot.price_no > config.max_no_price:
            continue

        win_prob = _estimate_no_win_prob(slot, forecast, error_dist)
        ev = win_prob * (1.0 - slot.price_no) - (1.0 - win_prob) * slot.price_no

        # Require higher EV for future markets
        ladder_ev_threshold = config.ladder_min_ev
        if days_ahead > 0:
            ladder_ev_threshold /= (config.day_ahead_ev_discount ** days_ahead)

        if ev < ladder_ev_threshold:
            continue

        signals.append(TradeSignal(
            token_type=TokenType.NO,
            side=Side.BUY,
            slot=slot,
            event=event,
            expected_value=ev,
            estimated_win_prob=win_prob,
        ))

    if signals:
        logger.debug(
            "City %s: %d ladder signals from %d near-forecast slots",
            event.city, len(signals), len(ladder_slots),
        )
    return signals


def evaluate_trim_signals(
    event: WeatherMarketEvent,
    forecast: Forecast,
    held_no_slots: list[TempSlot],
    config: StrategyConfig,
    error_dist: ForecastErrorDistribution | None = None,
    entry_prices: dict[str, float] | None = None,
) -> list[TradeSignal]:
    """Generate SELL signals for held NO positions whose EV has decayed.

    Unlike exit signals (which trigger on temperature proximity), trim signals
    fire when the expected value drops below min_trim_ev due to forecast changes.

    Hold-to-settlement bias: only trim if EV is negative. Positions with slightly
    positive EV (between 0 and min_trim_ev) are held since the round-trip spread
    cost of selling and re-entering is often higher than the EV decay.
    """
    signals: list[TradeSignal] = []

    for slot in held_no_slots:
        win_prob = _estimate_no_win_prob(slot, forecast, error_dist)
        ev = win_prob * (1.0 - slot.price_no) - (1.0 - win_prob) * slot.price_no

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
                "TRIM signal: %s slot %s EV=%.4f < -%.4f (win_prob=%.2f)",
                event.city, slot.outcome_label, ev, config.min_trim_ev, win_prob,
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
) -> list[TradeSignal]:
    """Phase 5: Generate SELL signals when held NO positions are threatened.

    IMPORTANT: Only applies to same-day markets (days_ahead=0).
    Today's observed temperature is irrelevant for future markets — those
    depend on future forecasts, not current observations.

    Trend state adjusts exit sensitivity:
    - STABLE: wider exit threshold (hold positions)
    - BREAKOUT: tighter threshold (exit faster when temperature moving)
    """
    if observation is None or daily_max_f is None:
        return []

    # EXIT signals only make sense for today's market
    # Today's daily_max is irrelevant for tomorrow's or later markets
    if days_ahead > 0:
        return []

    # Adjust exit distance based on trend
    # Widened thresholds to avoid premature exits (data showed 119/176 positions
    # were closed before settlement, losing round-trip spread costs)
    exit_distance = config.no_distance_threshold_f * 0.4
    if trend == TrendState.STABLE:
        exit_distance = config.no_distance_threshold_f * 0.5  # hold longer
    elif trend in (TrendState.BREAKOUT_UP, TrendState.BREAKOUT_DOWN):
        exit_distance = config.no_distance_threshold_f * 0.3  # exit faster but not as aggressively

    signals: list[TradeSignal] = []
    for slot in held_no_slots:
        distance = _slot_distance(slot, daily_max_f)

        if distance < exit_distance:
            signals.append(TradeSignal(
                token_type=TokenType.NO,
                side=Side.SELL,
                slot=slot,
                event=event,
                expected_value=0,
                estimated_win_prob=0,
            ))
            logger.info(
                "EXIT signal: daily max %.1f°F approaching NO slot %s (distance %.1f°F)",
                daily_max_f, slot.outcome_label, distance,
            )

    return signals
