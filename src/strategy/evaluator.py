"""Core strategy logic: evaluate temperature slots and generate trade signals."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from src.config import StrategyConfig
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.weather.models import Forecast, Observation

logger = logging.getLogger(__name__)


def _estimate_no_win_probability(
    distance_f: float,
    confidence_interval_f: float,
) -> float:
    """Estimate probability that temperature will NOT land in this slot.

    Uses a simple normal distribution approximation.
    The further the slot is from the forecast, the higher the NO probability.
    """
    # Treat confidence_interval as ~1 std dev
    sigma = max(confidence_interval_f, 1.0)
    # z-score: how many standard deviations away
    z = distance_f / sigma
    # Approximate CDF using error function
    # P(NOT in slot) ≈ 1 - P(in slot)
    # P(in slot) for a 2°F wide slot at distance z ≈ density at z * slot_width
    # Simplified: use cumulative probability
    cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
    return min(cdf, 0.99)  # cap at 99%


def _slot_distance(slot: TempSlot, forecast_high_f: float) -> float:
    """Calculate the minimum distance from the slot to the forecast high."""
    mid = slot.temp_midpoint_f
    if slot.temp_lower_f is not None and slot.temp_upper_f is not None:
        # Slot is a range: distance = min distance from forecast to range
        if slot.temp_lower_f <= forecast_high_f <= slot.temp_upper_f:
            return 0.0
        return min(abs(forecast_high_f - slot.temp_lower_f), abs(forecast_high_f - slot.temp_upper_f))
    return abs(mid - forecast_high_f)


def evaluate_no_signals(
    event: WeatherMarketEvent,
    forecast: Forecast,
    config: StrategyConfig,
) -> list[TradeSignal]:
    """Phase 4: Generate BUY NO signals for slots far from forecast.

    Only considers slots whose temperature range is far enough from the
    forecasted high to have a high NO win probability.
    """
    signals: list[TradeSignal] = []

    for slot in event.slots:
        distance = _slot_distance(slot, forecast.predicted_high_f)

        if distance < config.no_distance_threshold_f:
            continue

        if slot.price_no <= 0 or slot.price_no >= 1:
            continue

        win_prob = _estimate_no_win_probability(distance, forecast.confidence_interval_f)
        # EV = win_prob * profit_if_win - (1 - win_prob) * cost_if_lose
        # Buying NO at price p: win -> gain (1-p), lose -> lose p
        ev = win_prob * (1.0 - slot.price_no) - (1.0 - win_prob) * slot.price_no

        if ev < config.min_no_ev:
            continue

        signals.append(TradeSignal(
            token_type=TokenType.NO,
            side=Side.BUY,
            slot=slot,
            event=event,
            expected_value=ev,
            estimated_win_prob=win_prob,
        ))

    logger.debug(
        "City %s date %s: %d NO signals from %d slots",
        event.city, event.market_date, len(signals), len(event.slots),
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

    # Only consider YES when we're within the last 3 hours of the market
    if hours_remaining > 3:
        return []

    signals: list[TradeSignal] = []
    for slot in event.slots:
        lower = slot.temp_lower_f if slot.temp_lower_f is not None else -999
        upper = slot.temp_upper_f if slot.temp_upper_f is not None else 999

        # Check if daily max falls in this slot's range
        if not (lower <= daily_max_f <= upper):
            continue

        if slot.price_yes <= 0 or slot.price_yes >= 1:
            continue

        # Estimate probability based on time remaining and current temp
        # If < 1 hour left and temp is in this range, very high confidence
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


def evaluate_exit_signals(
    event: WeatherMarketEvent,
    observation: Observation | None,
    daily_max_f: float | None,
    held_no_slots: list[TempSlot],
    config: StrategyConfig,
) -> list[TradeSignal]:
    """Phase 5: Generate SELL signals when held NO positions are threatened.

    Sell NO when the real-time temperature is approaching the slot range.
    """
    if observation is None or daily_max_f is None:
        return []

    signals: list[TradeSignal] = []
    for slot in held_no_slots:
        distance = _slot_distance(slot, daily_max_f)

        # If daily max is within the danger zone (within threshold/2 of slot),
        # the NO position is at risk — sell to cut losses
        if distance < config.no_distance_threshold_f / 2:
            signals.append(TradeSignal(
                token_type=TokenType.NO,
                side=Side.SELL,
                slot=slot,
                event=event,
                expected_value=0,  # exit signal, not an EV play
                estimated_win_prob=0,
            ))
            logger.info(
                "EXIT signal: daily max %.1f°F approaching NO slot %s (distance %.1f°F)",
                daily_max_f, slot.outcome_label, distance,
            )

    return signals
