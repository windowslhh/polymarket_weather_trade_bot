"""Tests for strategy evaluator and sizing."""
from datetime import date, datetime, timezone

from src.config import StrategyConfig
from src.markets.models import Side, TempSlot, TokenType, WeatherMarketEvent
from src.strategy.evaluator import (
    _estimate_no_win_probability_normal as _estimate_no_win_probability,
    _slot_distance,
    evaluate_exit_signals,
    evaluate_no_signals,
    evaluate_yes_signals,
)
from src.strategy.sizing import compute_size
from src.weather.models import Forecast, Observation


def _make_slot(lower: float | None, upper: float | None, price_yes: float = 0.1, price_no: float = 0.9) -> TempSlot:
    label = ""
    if lower is not None and upper is not None:
        label = f"{lower}°F to {upper}°F"
    elif lower is not None:
        label = f"{lower}°F or above"
    elif upper is not None:
        label = f"Below {upper}°F"
    return TempSlot(
        token_id_yes="yes_token",
        token_id_no="no_token",
        outcome_label=label,
        temp_lower_f=lower,
        temp_upper_f=upper,
        price_yes=price_yes,
        price_no=price_no,
    )


def _make_event(city: str = "New York", slots: list[TempSlot] | None = None) -> WeatherMarketEvent:
    return WeatherMarketEvent(
        event_id="evt_1",
        condition_id="cond_1",
        city=city,
        market_date=date.today(),
        slots=slots or [],
        end_timestamp=datetime(2026, 4, 4, 23, 0, tzinfo=timezone.utc),
        title=f"Highest temperature in {city} on April 4",
    )


def _make_forecast(high: float = 75.0) -> Forecast:
    return Forecast(
        city="New York",
        forecast_date=date.today(),
        predicted_high_f=high,
        predicted_low_f=high - 15,
        confidence_interval_f=4.0,
        source="test",
        fetched_at=datetime.now(timezone.utc),
    )


class TestNoWinProbability:
    def test_far_distance_high_probability(self):
        prob = _estimate_no_win_probability(12.0, 4.0)
        assert prob > 0.95

    def test_close_distance_lower_probability(self):
        prob = _estimate_no_win_probability(2.0, 4.0)
        assert prob < 0.8

    def test_zero_distance(self):
        prob = _estimate_no_win_probability(0.0, 4.0)
        assert prob == 0.5

    def test_capped_at_99(self):
        prob = _estimate_no_win_probability(100.0, 4.0)
        assert prob == 0.99


class TestSlotDistance:
    def test_range_slot_contains_forecast(self):
        slot = _make_slot(73, 77)
        assert _slot_distance(slot, 75.0) == 0.0

    def test_range_slot_above_forecast(self):
        slot = _make_slot(80, 84)
        assert _slot_distance(slot, 75.0) == 5.0

    def test_range_slot_below_forecast(self):
        slot = _make_slot(60, 64)
        assert _slot_distance(slot, 75.0) == 11.0

    def test_open_upper_slot(self):
        slot = _make_slot(90, None)
        assert _slot_distance(slot, 75.0) == 16.0


class TestEvaluateNoSignals:
    def test_generates_signals_for_distant_slots(self):
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01)
        slots = [
            _make_slot(73, 77, price_no=0.85),  # close to forecast (75) -> skip
            _make_slot(85, 89, price_no=0.92),  # 10°F away -> signal
            _make_slot(90, 94, price_no=0.95),  # 15°F away -> signal
        ]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)

        signals = evaluate_no_signals(event, forecast, config)
        assert len(signals) == 2
        assert all(s.token_type == TokenType.NO for s in signals)
        assert all(s.side == Side.BUY for s in signals)

    def test_no_signals_when_all_close(self):
        config = StrategyConfig(no_distance_threshold_f=8)
        slots = [_make_slot(73, 77, price_no=0.85)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)

        signals = evaluate_no_signals(event, forecast, config)
        assert len(signals) == 0


class TestEvaluateExitSignals:
    def test_exit_when_temp_approaches_slot(self):
        config = StrategyConfig(no_distance_threshold_f=8)
        held_slot = _make_slot(80, 84, price_no=0.92)
        event = _make_event(slots=[held_slot])
        obs = Observation(icao="KLGA", temp_f=79.0, observation_time=datetime.now(timezone.utc))

        signals = evaluate_exit_signals(event, obs, 79.0, [held_slot], config)
        # distance from 79 to 80-84 is 1, which is < threshold/2 = 4
        assert len(signals) == 1
        assert signals[0].side == Side.SELL

    def test_no_exit_when_temp_far(self):
        config = StrategyConfig(no_distance_threshold_f=8)
        held_slot = _make_slot(90, 94, price_no=0.95)
        event = _make_event(slots=[held_slot])
        obs = Observation(icao="KLGA", temp_f=72.0, observation_time=datetime.now(timezone.utc))

        signals = evaluate_exit_signals(event, obs, 72.0, [held_slot], config)
        assert len(signals) == 0


class TestSizing:
    def test_basic_sizing(self):
        signal = _make_signal(win_prob=0.95, price=0.92)
        size = compute_size(signal, city_exposure_usd=0, total_exposure_usd=0,
                           config=StrategyConfig())
        assert size > 0
        assert size <= 5.0  # max per slot

    def test_zero_when_city_maxed(self):
        signal = _make_signal(win_prob=0.95, price=0.92)
        size = compute_size(signal, city_exposure_usd=50.0, total_exposure_usd=50.0,
                           config=StrategyConfig())
        assert size == 0.0

    def test_zero_when_negative_ev(self):
        signal = _make_signal(win_prob=0.5, price=0.92)
        size = compute_size(signal, city_exposure_usd=0, total_exposure_usd=0,
                           config=StrategyConfig())
        assert size == 0.0


def _make_signal(win_prob: float = 0.95, price: float = 0.92) -> object:
    """Create a mock TradeSignal-like object for sizing tests."""
    from src.markets.models import TradeSignal
    slot = _make_slot(90, 94, price_no=price)
    event = _make_event(slots=[slot])
    return TradeSignal(
        token_type=TokenType.NO,
        side=Side.BUY,
        slot=slot,
        event=event,
        expected_value=0.05,
        estimated_win_prob=win_prob,
    )
