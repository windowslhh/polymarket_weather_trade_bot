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


def test_trim_fires_when_ev_clearly_negative():
    """Slot near forecast with high NO price → negative EV → triggers trim.

    Hold-to-settlement bias: trim only fires when EV < -min_trim_ev (clearly negative),
    not just below the threshold. This avoids premature exits that lose spread costs.
    """
    # Forecast=75, slot=73-77 → forecast IN slot → NO win prob ~0.5
    # price_no=0.8 → EV = 0.5*(0.2) - 0.5*(0.8) = -0.3 → clearly negative → trim
    event, slot = _make_event_with_slot(73.0, 77.0, price_no=0.8)
    forecast = _make_forecast(75.0)
    config = StrategyConfig(min_trim_ev=0.02)

    signals = evaluate_trim_signals(event, forecast, [slot], config)
    assert len(signals) == 1
    assert signals[0].side == Side.SELL
    assert signals[0].token_type == TokenType.NO


def test_trim_holds_marginal_ev():
    """Position with EV=0 (at breakeven) should NOT be trimmed — hold to settlement."""
    # Forecast=75, slot=73-77 → NO win prob ~0.5, price_no=0.5 → EV=0
    event, slot = _make_event_with_slot(73.0, 77.0, price_no=0.5)
    forecast = _make_forecast(75.0)
    config = StrategyConfig(min_trim_ev=0.005)

    signals = evaluate_trim_signals(event, forecast, [slot], config)
    assert len(signals) == 0  # EV=0 is not < -0.005, so hold


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
    # Higher NO price to make EV clearly negative
    event, slot = _make_event_with_slot(73.0, 77.0, price_no=0.8)
    forecast = _make_forecast(75.0)
    config = StrategyConfig(min_trim_ev=0.02)

    # Build distribution with tight errors → high certainty forecast is in slot
    errors = [e * 0.1 for e in range(-20, 21)]  # errors from -2 to +2
    dist = ForecastErrorDistribution("TestCity", errors)

    signals = evaluate_trim_signals(event, forecast, [slot], config, error_dist=dist)
    # Forecast right in slot with tight distribution → NO should lose → EV clearly negative → trim
    assert len(signals) == 1


def test_trim_empty_held_slots():
    """No trim signals when no positions are held."""
    event, _ = _make_event_with_slot(73.0, 77.0)
    forecast = _make_forecast(75.0)
    config = StrategyConfig(min_trim_ev=0.005)

    signals = evaluate_trim_signals(event, forecast, [], config)
    assert len(signals) == 0
