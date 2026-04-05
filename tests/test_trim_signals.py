"""Tests for auto-trim low-EV position signals."""
from __future__ import annotations

from datetime import date

import pytest

from src.config import StrategyConfig
from src.markets.models import Side, TempSlot, TokenType, WeatherMarketEvent
from src.strategy.evaluator import evaluate_trim_signals
from src.weather.historical import ForecastErrorDistribution
from src.weather.models import Forecast


def _make_forecast(high: float = 75.0) -> Forecast:
    from datetime import datetime, timezone
    return Forecast(
        city="TestCity",
        forecast_date=date(2026, 4, 5),
        predicted_high_f=high,
        predicted_low_f=60.0,
        confidence_interval_f=3.0,
        source="test",
        fetched_at=datetime.now(timezone.utc),
    )


def _make_event_with_slot(lower: float, upper: float, price_no: float = 0.1) -> tuple[WeatherMarketEvent, TempSlot]:
    slot = TempSlot(
        token_id_yes="ty", token_id_no="tn",
        outcome_label=f"{lower}-{upper}°F",
        temp_lower_f=lower, temp_upper_f=upper,
        price_yes=1 - price_no, price_no=price_no,
    )
    event = WeatherMarketEvent(
        event_id="e1", condition_id="c1",
        city="TestCity", market_date=date(2026, 4, 5),
        slots=[slot],
    )
    return event, slot


def test_trim_fires_when_ev_below_threshold():
    """Slot near forecast should have low NO EV and trigger trim."""
    # Forecast=75, slot=73-77 → forecast is IN the slot → NO win prob is LOW
    event, slot = _make_event_with_slot(73.0, 77.0, price_no=0.5)
    forecast = _make_forecast(75.0)
    config = StrategyConfig(min_trim_ev=0.005)

    signals = evaluate_trim_signals(event, forecast, [slot], config)
    assert len(signals) == 1
    assert signals[0].side == Side.SELL
    assert signals[0].token_type == TokenType.NO


def test_trim_does_not_fire_when_ev_above_threshold():
    """Slot far from forecast should have high NO EV and not trigger trim."""
    # Forecast=75, slot=90-95 → forecast far away → NO win prob is HIGH
    event, slot = _make_event_with_slot(90.0, 95.0, price_no=0.1)
    forecast = _make_forecast(75.0)
    config = StrategyConfig(min_trim_ev=0.005)

    signals = evaluate_trim_signals(event, forecast, [slot], config)
    assert len(signals) == 0


def test_trim_with_empirical_distribution():
    """Trim uses empirical error distribution when available."""
    event, slot = _make_event_with_slot(73.0, 77.0, price_no=0.5)
    forecast = _make_forecast(75.0)
    config = StrategyConfig(min_trim_ev=0.005)

    # Build distribution with tight errors → high certainty forecast is in slot
    errors = [e * 0.1 for e in range(-20, 21)]  # errors from -2 to +2
    dist = ForecastErrorDistribution("TestCity", errors)

    signals = evaluate_trim_signals(event, forecast, [slot], config, error_dist=dist)
    # Forecast right in slot with tight distribution → NO should lose → trim
    assert len(signals) == 1


def test_trim_empty_held_slots():
    """No trim signals when no positions are held."""
    event, _ = _make_event_with_slot(73.0, 77.0)
    forecast = _make_forecast(75.0)
    config = StrategyConfig(min_trim_ev=0.005)

    signals = evaluate_trim_signals(event, forecast, [], config)
    assert len(signals) == 0
