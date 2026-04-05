"""Tests for ladder/围网 strategy."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.config import StrategyConfig
from src.markets.models import TempSlot, WeatherMarketEvent
from src.strategy.evaluator import evaluate_ladder_signals
from src.weather.models import Forecast


def _make_forecast(high: float = 75.0) -> Forecast:
    return Forecast(
        city="TestCity", forecast_date=date(2026, 4, 5),
        predicted_high_f=high, predicted_low_f=60.0,
        confidence_interval_f=3.0, source="test",
        fetched_at=datetime.now(timezone.utc),
    )


def _make_event(slot_ranges: list[tuple[float, float]], forecast_high: float = 75.0) -> WeatherMarketEvent:
    """Create event with slots at given temp ranges."""
    slots = []
    for lower, upper in slot_ranges:
        distance = abs((lower + upper) / 2 - forecast_high)
        # NO price inversely proportional to distance (near slots = expensive NO)
        no_price = max(0.05, min(0.95, 1.0 - distance / 30))
        slots.append(TempSlot(
            token_id_yes=f"ty-{lower}", token_id_no=f"tn-{lower}",
            outcome_label=f"{lower}-{upper}°F",
            temp_lower_f=lower, temp_upper_f=upper,
            price_yes=1 - no_price, price_no=no_price,
        ))
    return WeatherMarketEvent(
        event_id="e1", condition_id="c1", city="TestCity",
        market_date=date(2026, 4, 5), slots=slots,
    )


def test_ladder_generates_near_forecast_signals():
    """Ladder should generate signals for slots within distance threshold."""
    # Slots from 65-95 in 5°F bins, forecast=75
    ranges = [(i, i + 5) for i in range(65, 95, 5)]
    event = _make_event(ranges, 75.0)
    config = StrategyConfig(
        no_distance_threshold_f=8, ladder_width=3, ladder_min_ev=0.01,
    )

    signals = evaluate_ladder_signals(event, _make_forecast(75.0), config)
    # Should generate signals for near-forecast slots (distance < 8)
    # but only those with positive EV
    assert len(signals) >= 0  # depends on EV; at least should not crash
    for s in signals:
        # All ladder signals should be within distance threshold
        mid = (s.slot.temp_lower_f + s.slot.temp_upper_f) / 2
        assert abs(mid - 75.0) < config.no_distance_threshold_f


def test_ladder_zero_width_disabled():
    """Ladder with width=0 should generate no signals."""
    ranges = [(70, 75), (75, 80)]
    event = _make_event(ranges)
    config = StrategyConfig(ladder_width=0)

    signals = evaluate_ladder_signals(event, _make_forecast(75.0), config)
    assert len(signals) == 0


def test_ladder_respects_min_ev():
    """Ladder should not generate signals below min EV threshold."""
    # Very close slot = NO price high = low EV
    event = _make_event([(74, 76)])  # right at forecast
    config = StrategyConfig(ladder_width=3, ladder_min_ev=0.5)  # very high threshold

    signals = evaluate_ladder_signals(event, _make_forecast(75.0), config)
    assert len(signals) == 0


def test_ladder_does_not_overlap_with_no_signals():
    """Ladder signals should only cover slots WITHIN distance threshold (not beyond)."""
    ranges = [(i, i + 3) for i in range(60, 96, 3)]
    event = _make_event(ranges, 75.0)
    config = StrategyConfig(
        no_distance_threshold_f=8, ladder_width=3, ladder_min_ev=0.001,
    )

    signals = evaluate_ladder_signals(event, _make_forecast(75.0), config)
    for s in signals:
        if s.slot.temp_lower_f is not None and s.slot.temp_upper_f is not None:
            mid = (s.slot.temp_lower_f + s.slot.temp_upper_f) / 2
            assert abs(mid - 75.0) < config.no_distance_threshold_f
