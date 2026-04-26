"""FIX-01: rebalancer must route forecast-by-event.market_date, not by today's date.

Bug #1 (Houston 2026-04-17) was caused by the main cycle's
``forecasts = await get_forecasts_batch(city_configs)`` call defaulting to
today's date and then using that forecast for D+1/D+2 events.  This test
locks in the invariant: given cached forecasts for {today, today+1,
today+2}, the rebalancer's ``_forecast_for_event`` returns the right-day
forecast for each event's ``market_date``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.markets.models import TempSlot, WeatherMarketEvent
from src.strategy.rebalancer import Rebalancer
from src.weather.models import Forecast


def _make_forecast(city: str, forecast_date: date, high: float) -> Forecast:
    return Forecast(
        city=city, forecast_date=forecast_date,
        predicted_high_f=high, predicted_low_f=high - 15,
        confidence_interval_f=3.0, source="test",
        fetched_at=datetime.now(timezone.utc),
    )


def _make_event(city: str, market_date: date) -> WeatherMarketEvent:
    return WeatherMarketEvent(
        event_id=f"ev_{city}_{market_date}", condition_id="c", city=city,
        market_date=market_date,
        slots=[TempSlot(
            token_id_yes="y", token_id_no="n", outcome_label="80°F",
            temp_lower_f=80.0, temp_upper_f=80.0, price_no=0.5,
        )],
    )


def _make_rebalancer_for_lookup() -> Rebalancer:
    """A bare Rebalancer with the minimum scaffolding for _forecast_for_event.

    We do not exercise run() — that orchestrator test lives in test_integration.
    Here we only want to prove the lookup picks the correct day.
    """
    cfg = SimpleNamespace(cities=[], strategy=SimpleNamespace(
        exit_cooldown_hours=1,
    ))
    # __init__ touches config.cities for tz registration, an alerter for webhook,
    # etc.  We build a real Rebalancer with empty cities to avoid the full mock
    # tax while still exercising the production code path.
    clob = MagicMock()
    portfolio = MagicMock()
    executor = MagicMock()
    max_tracker = MagicMock()
    max_tracker.register_timezone = MagicMock()
    rb = Rebalancer.__new__(Rebalancer)
    rb._config = cfg
    rb._cached_forecasts_by_date = {}
    return rb


def test_routes_today_d1_d2_correctly():
    rb = _make_rebalancer_for_lookup()
    today = date(2026, 4, 25)
    rb._cached_forecasts_by_date = {
        today: {"Chicago": _make_forecast("Chicago", today, 70.0)},
        today + timedelta(days=1): {"Chicago": _make_forecast("Chicago", today + timedelta(days=1), 80.0)},
        today + timedelta(days=2): {"Chicago": _make_forecast("Chicago", today + timedelta(days=2), 90.0)},
    }

    same_day = rb._forecast_for_event(_make_event("Chicago", today))
    d1 = rb._forecast_for_event(_make_event("Chicago", today + timedelta(days=1)))
    d2 = rb._forecast_for_event(_make_event("Chicago", today + timedelta(days=2)))

    assert same_day.predicted_high_f == 70.0
    assert d1.predicted_high_f == 80.0, "D+1 must not use today's forecast"
    assert d2.predicted_high_f == 90.0, "D+2 must not use today's forecast"


def test_missing_date_returns_none():
    """If we don't have a forecast for the event's date, lookup returns None
    — the caller is responsible for skipping the event (or falling back to
    today's forecast only when same-day)."""
    rb = _make_rebalancer_for_lookup()
    today = date(2026, 4, 25)
    rb._cached_forecasts_by_date = {
        today: {"Chicago": _make_forecast("Chicago", today, 70.0)},
    }

    result = rb._forecast_for_event(_make_event("Chicago", today + timedelta(days=5)))
    assert result is None


def test_wrong_city_returns_none():
    rb = _make_rebalancer_for_lookup()
    today = date(2026, 4, 25)
    rb._cached_forecasts_by_date = {
        today: {"Chicago": _make_forecast("Chicago", today, 70.0)},
    }
    result = rb._forecast_for_event(_make_event("Dallas", today))
    assert result is None


def test_edge_history_includes_forecast_date(tmp_path):
    """New edge_history.forecast_date column is wired through the store."""
    import asyncio
    from src.portfolio.store import Store

    async def _exercise():
        store = Store(tmp_path / "bot.db")
        await store.initialize()

        await store.insert_edge_snapshot(
            cycle_at="2026-04-25T12:00:00",
            city="Chicago",
            market_date="2026-04-26",  # D+1 event
            slot_label="80°F to 84°F",
            forecast_high_f=82.0,
            price_yes=0.3, price_no=0.7,
            win_prob=0.8, ev=0.04, distance_f=2.0,
            trend_state="STABLE",
            forecast_date="2026-04-26",  # must match, not today
        )
        await store.flush_edge_batch()

        rows = await store.get_edge_history()
        await store.close()
        return rows

    rows = asyncio.run(_exercise())
    assert len(rows) == 1
    assert rows[0]["forecast_date"] == "2026-04-26"
    assert rows[0]["market_date"] == "2026-04-26"
