"""Tests for forecast trend detection."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from src.strategy.trend import ForecastTrend, TrendState


def test_stable_with_small_changes():
    t = ForecastTrend()
    t.update("NYC", 75.0)
    t.update("NYC", 75.3)
    t.update("NYC", 75.5)
    assert t.get_trend("NYC") == TrendState.STABLE


def test_breakout_up():
    t = ForecastTrend()
    t.update("NYC", 72.0)
    t.update("NYC", 74.0)
    t.update("NYC", 76.0)
    assert t.get_trend("NYC") == TrendState.BREAKOUT_UP


def test_breakout_down():
    t = ForecastTrend()
    t.update("NYC", 80.0)
    t.update("NYC", 78.0)
    t.update("NYC", 76.0)
    assert t.get_trend("NYC") == TrendState.BREAKOUT_DOWN


def test_settling_near_settlement():
    t = ForecastTrend()
    t.update("NYC", 75.0)
    t.update("NYC", 75.2)
    # Near settlement (3 hours) with stable forecast
    assert t.get_trend("NYC", hours_to_settlement=3.0) == TrendState.SETTLING


def test_not_settling_if_volatile():
    t = ForecastTrend()
    t.update("NYC", 72.0)
    t.update("NYC", 78.0)
    # Even near settlement, if forecast is volatile → not settling
    assert t.get_trend("NYC", hours_to_settlement=3.0) != TrendState.SETTLING


def test_get_delta():
    t = ForecastTrend()
    t.update("NYC", 70.0)
    t.update("NYC", 73.5)
    assert t.get_delta("NYC") == 3.5


def test_delta_no_history():
    t = ForecastTrend()
    assert t.get_delta("NYC") == 0.0


def test_single_reading_is_stable():
    t = ForecastTrend()
    t.update("NYC", 75.0)
    assert t.get_trend("NYC") == TrendState.STABLE


def test_independent_cities():
    t = ForecastTrend()
    t.update("NYC", 70.0)
    t.update("NYC", 74.0)
    t.update("NYC", 78.0)
    t.update("LA", 80.0)
    t.update("LA", 80.1)
    assert t.get_trend("NYC") == TrendState.BREAKOUT_UP
    assert t.get_trend("LA") == TrendState.STABLE


def test_history_capped():
    t = ForecastTrend()
    for i in range(50):
        t.update("NYC", 70.0 + i * 0.1)
    assert len(t.get_history("NYC")) <= 24
