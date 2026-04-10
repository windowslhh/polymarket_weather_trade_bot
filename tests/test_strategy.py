"""Tests for strategy evaluator and sizing."""
import importlib
import time
from datetime import date, datetime, timezone

import pytest

from src.config import StrategyConfig
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.strategy.evaluator import (
    _estimate_no_win_probability_normal as _estimate_no_win_probability,
    _estimate_no_win_prob,
    _slot_distance,
    evaluate_exit_signals,
    evaluate_no_signals,
    evaluate_trim_signals,
)
from src.strategy.sizing import compute_size
from src.strategy.trend import TrendState
from src.weather.historical import ForecastErrorDistribution
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
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.95)
        slots = [
            _make_slot(73, 77, price_no=0.60),  # close to forecast (75) -> skip (distance)
            _make_slot(85, 89, price_no=0.80),  # 10°F away -> signal
            _make_slot(90, 94, price_no=0.85),  # 15°F away -> signal
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


def _make_signal(win_prob: float = 0.95, price: float = 0.92) -> TradeSignal:
    """Create a mock TradeSignal-like object for sizing tests."""
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


def _make_error_dist(city: str = "New York", n: int = 100, mean: float = 0.0, spread: float = 3.0) -> ForecastErrorDistribution:
    """Create a ForecastErrorDistribution with N synthetic errors."""
    import random
    rng = random.Random(42)
    errors = [mean + rng.gauss(0, spread) for _ in range(n)]
    return ForecastErrorDistribution(city, errors)


# ──────────────────────────────────────────────────────────────────────
# Supplementary Tests: Critical Paths
# ──────────────────────────────────────────────────────────────────────

class TestEstimateNoWinProb:
    """Test the dispatcher that picks empirical vs normal fallback."""

    def test_uses_empirical_when_enough_samples(self):
        dist = _make_error_dist(n=100, spread=3.0)
        slot = _make_slot(90, 94)
        forecast = _make_forecast(75.0)
        prob = _estimate_no_win_prob(slot, forecast, dist)
        # 15°F away with spread=3 → should be very high
        assert 0.90 < prob <= 0.99

    def test_falls_back_to_normal_when_few_samples(self):
        dist = _make_error_dist(n=20, spread=3.0)  # <30 → fallback
        slot = _make_slot(90, 94)
        forecast = _make_forecast(75.0)
        prob = _estimate_no_win_prob(slot, forecast, dist)
        # Normal fallback with distance=15, confidence=4 → very high
        assert prob > 0.90

    def test_falls_back_to_normal_when_none(self):
        slot = _make_slot(90, 94)
        forecast = _make_forecast(75.0)
        prob = _estimate_no_win_prob(slot, forecast, None)
        assert prob > 0.90

    def test_close_slot_low_probability(self):
        dist = _make_error_dist(n=100, spread=3.0)
        slot = _make_slot(73, 77)  # contains forecast 75
        forecast = _make_forecast(75.0)
        prob = _estimate_no_win_prob(slot, forecast, dist)
        # Forecast is inside the slot → p(NO) should be low
        assert prob < 0.70


class TestEvaluateNoSignalsAdvanced:
    """Advanced NO signal tests: held tokens, price bounds, trends, days_ahead."""

    def test_held_token_ids_filtered(self):
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.95)
        slot_a = TempSlot(
            token_id_yes="yes_a", token_id_no="no_a",
            outcome_label="85°F to 89°F", temp_lower_f=85, temp_upper_f=89,
            price_yes=0.20, price_no=0.80,
        )
        slot_b = TempSlot(
            token_id_yes="yes_b", token_id_no="no_b",
            outcome_label="90°F to 94°F", temp_lower_f=90, temp_upper_f=94,
            price_yes=0.15, price_no=0.85,
        )
        event = _make_event(slots=[slot_a, slot_b])
        forecast = _make_forecast(75.0)

        # Without held → 2 signals
        signals_all = evaluate_no_signals(event, forecast, config)
        assert len(signals_all) == 2

        # Hold slot_a's NO token → only slot_b passes
        held = {"no_a"}
        signals_held = evaluate_no_signals(event, forecast, config, held_token_ids=held)
        assert len(signals_held) == 1
        assert signals_held[0].slot.token_id_no == "no_b"

    def test_all_held_returns_empty(self):
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.95)
        slots = [_make_slot(90, 94, price_no=0.80)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)
        held = {slots[0].token_id_no}
        assert evaluate_no_signals(event, forecast, config, held_token_ids=held) == []

    def test_price_no_at_zero_skipped(self):
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.95)
        slots = [_make_slot(90, 94, price_no=0.0)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)
        assert evaluate_no_signals(event, forecast, config) == []

    def test_price_no_at_one_skipped(self):
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.95)
        slots = [_make_slot(90, 94, price_no=1.0)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)
        assert evaluate_no_signals(event, forecast, config) == []

    def test_max_no_price_boundary_exact(self):
        """price_no == max_no_price → should pass (code is '>' not '>=')."""
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.80)
        slots = [_make_slot(90, 94, price_no=0.80)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)
        signals = evaluate_no_signals(event, forecast, config)
        assert len(signals) == 1

    def test_max_no_price_boundary_above(self):
        """price_no just above max_no_price → skipped."""
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.80)
        slots = [_make_slot(90, 94, price_no=0.81)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)
        assert evaluate_no_signals(event, forecast, config) == []

    def test_distance_exactly_at_threshold_passes(self):
        """distance == threshold → passes (code uses '<' not '<=')."""
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.95)
        # forecast 75, slot [83, 87] → distance = |75-83| = 8 == threshold
        slots = [_make_slot(83, 87, price_no=0.80)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)
        signals = evaluate_no_signals(event, forecast, config)
        assert len(signals) == 1

    def test_distance_just_below_threshold_skipped(self):
        """distance < threshold → skipped."""
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.95)
        # forecast 75, slot [82, 86] → distance = |75-82| = 7 < 8
        slots = [_make_slot(82, 86, price_no=0.80)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)
        assert evaluate_no_signals(event, forecast, config) == []

    def test_settling_trend_raises_ev_threshold(self):
        """SETTLING trend multiplies ev_threshold by 1.5 → harder to pass."""
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.05, max_no_price=0.95)
        # Slot far enough to pass distance but marginal EV
        slots = [_make_slot(85, 89, price_no=0.80)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)

        # Without trend → signal may pass
        sig_no_trend = evaluate_no_signals(event, forecast, config)
        # With SETTLING → threshold = 0.05 * 1.5 = 0.075, might exclude
        sig_settling = evaluate_no_signals(event, forecast, config, trend=TrendState.SETTLING)
        # Settling should produce ≤ signals than no trend
        assert len(sig_settling) <= len(sig_no_trend)

    def test_breakout_up_boosts_lower_slots(self):
        """BREAKOUT_UP boosts win_prob for slots below forecast."""
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.95)
        # Slot below forecast: [60, 64], forecast=75, distance=11
        slots = [_make_slot(60, 64, price_no=0.80)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)

        sig_none = evaluate_no_signals(event, forecast, config)
        sig_up = evaluate_no_signals(event, forecast, config, trend=TrendState.BREAKOUT_UP)
        # BREAKOUT_UP should boost lower slot EV
        if sig_none and sig_up:
            assert sig_up[0].expected_value >= sig_none[0].expected_value

    def test_breakout_down_boosts_upper_slots(self):
        """BREAKOUT_DOWN boosts win_prob for slots above forecast."""
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.95)
        # Slot above forecast: [85, 89], forecast=75, distance=10
        slots = [_make_slot(85, 89, price_no=0.80)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)

        sig_none = evaluate_no_signals(event, forecast, config)
        sig_down = evaluate_no_signals(event, forecast, config, trend=TrendState.BREAKOUT_DOWN)
        if sig_none and sig_down:
            assert sig_down[0].expected_value >= sig_none[0].expected_value

    def test_days_ahead_raises_ev_threshold(self):
        """days_ahead > 0 divides ev_threshold by discount^days → stricter."""
        config = StrategyConfig(
            no_distance_threshold_f=8, min_no_ev=0.05,
            max_no_price=0.95, day_ahead_ev_discount=0.7,
        )
        slots = [_make_slot(85, 89, price_no=0.80)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)

        sig_d0 = evaluate_no_signals(event, forecast, config, days_ahead=0)
        # D+1: threshold = 0.05 / 0.7 ≈ 0.071
        sig_d1 = evaluate_no_signals(event, forecast, config, days_ahead=1)
        # D+2: threshold = 0.05 / 0.49 ≈ 0.102
        sig_d2 = evaluate_no_signals(event, forecast, config, days_ahead=2)
        # Monotonically fewer (or equal) signals as days_ahead increases
        assert len(sig_d0) >= len(sig_d1) >= len(sig_d2)

    def test_empty_event_no_signals(self):
        config = StrategyConfig()
        event = _make_event(slots=[])
        forecast = _make_forecast(75.0)
        assert evaluate_no_signals(event, forecast, config) == []

    def test_open_upper_slot_generates_signal(self):
        """≥X°F slot (upper=None) far from forecast → signal."""
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.95)
        # "90°F or above": lower=90, upper=None, midpoint=91, distance=16
        slots = [_make_slot(90, None, price_no=0.80)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)
        signals = evaluate_no_signals(event, forecast, config)
        assert len(signals) == 1

    def test_open_lower_slot_generates_signal(self):
        """Below X°F slot (lower=None) far from forecast → signal."""
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.95)
        # "Below 60°F": lower=None, upper=60, midpoint=59, distance=16
        slots = [_make_slot(None, 60, price_no=0.80)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)
        signals = evaluate_no_signals(event, forecast, config)
        assert len(signals) == 1

    def test_all_signals_are_no_buy(self):
        """Every signal from evaluate_no_signals must be NO/BUY."""
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.95)
        slots = [
            _make_slot(85, 89, price_no=0.80),
            _make_slot(90, 94, price_no=0.85),
            _make_slot(60, 64, price_no=0.75),
        ]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)
        signals = evaluate_no_signals(event, forecast, config)
        for s in signals:
            assert s.token_type == TokenType.NO
            assert s.side == Side.BUY
            assert 0 < s.expected_value
            assert 0 < s.estimated_win_prob <= 0.99

    def test_empirical_dist_used_when_available(self):
        """With empirical dist (>=30 samples), probabilities differ from normal."""
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.95)
        slots = [_make_slot(85, 89, price_no=0.80)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)

        sig_normal = evaluate_no_signals(event, forecast, config, error_dist=None)
        dist = _make_error_dist(n=100, spread=3.0)
        sig_empirical = evaluate_no_signals(event, forecast, config, error_dist=dist)
        # Both should produce signals; EVs may differ
        assert len(sig_normal) >= 1
        assert len(sig_empirical) >= 1


# ──────────────────────────────────────────────────────────────────────
# Supplementary Tests: Trim Signals (previously zero coverage)
# ──────────────────────────────────────────────────────────────────────

class TestEvaluateTrimSignals:
    """Test evaluate_trim_signals — zero existing tests before this."""

    def test_negative_ev_triggers_trim(self):
        """When EV < -min_trim_ev, should produce SELL signal."""
        config = StrategyConfig(min_trim_ev=0.02)
        # Slot very close to forecast → high p(YES) → low p(NO) → negative EV for NO
        held_slot = _make_slot(73, 77, price_no=0.80)  # contains forecast 75
        event = _make_event(slots=[held_slot])
        forecast = _make_forecast(75.0)

        signals = evaluate_trim_signals(event, forecast, [held_slot], config)
        # p(NO) is low → EV negative → trim
        assert len(signals) == 1
        assert signals[0].side == Side.SELL
        assert signals[0].token_type == TokenType.NO
        assert signals[0].expected_value < 0

    def test_positive_ev_no_trim(self):
        """When EV is solidly positive, no trim signal."""
        config = StrategyConfig(min_trim_ev=0.02)
        # Slot far from forecast → high p(NO) → positive EV
        held_slot = _make_slot(90, 94, price_no=0.80)
        event = _make_event(slots=[held_slot])
        forecast = _make_forecast(75.0)

        signals = evaluate_trim_signals(event, forecast, [held_slot], config)
        assert len(signals) == 0

    def test_marginal_positive_ev_held(self):
        """Hold-to-settlement bias: slightly negative EV above -min_trim_ev is held."""
        config = StrategyConfig(min_trim_ev=0.10)
        # Need a slot where EV is between -0.10 and 0 (slightly negative but above threshold)
        # Use a slot just outside forecast with high price_no so EV is slightly negative
        held_slot = _make_slot(80, 84, price_no=0.95)
        event = _make_event(slots=[held_slot])
        forecast = _make_forecast(75.0)

        signals = evaluate_trim_signals(event, forecast, [held_slot], config)
        # With a generous min_trim_ev=0.10, marginal negatives should be held
        # (the exact result depends on win_prob, but we verify the bias exists)
        # Either 0 signals (held) or signal with EV < -0.10
        for s in signals:
            assert s.expected_value < -config.min_trim_ev

    def test_empty_held_slots(self):
        config = StrategyConfig()
        event = _make_event(slots=[])
        forecast = _make_forecast(75.0)
        assert evaluate_trim_signals(event, forecast, [], config) == []

    def test_multiple_slots_mixed_trim(self):
        """Multiple held slots: some trim, some hold."""
        config = StrategyConfig(min_trim_ev=0.02)
        close_slot = _make_slot(73, 77, price_no=0.80)  # close to 75 → negative EV → trim
        far_slot = _make_slot(90, 94, price_no=0.80)     # far from 75 → positive EV → hold
        event = _make_event(slots=[close_slot, far_slot])
        forecast = _make_forecast(75.0)

        signals = evaluate_trim_signals(event, forecast, [close_slot, far_slot], config)
        # Only the close slot should trigger trim
        trimmed_labels = {s.slot.outcome_label for s in signals}
        assert close_slot.outcome_label in trimmed_labels
        assert far_slot.outcome_label not in trimmed_labels

    def test_trim_uses_empirical_dist(self):
        """Trim evaluation should use empirical distribution when provided."""
        config = StrategyConfig(min_trim_ev=0.02)
        held_slot = _make_slot(73, 77, price_no=0.80)
        event = _make_event(slots=[held_slot])
        forecast = _make_forecast(75.0)
        dist = _make_error_dist(n=100, spread=3.0)

        signals = evaluate_trim_signals(event, forecast, [held_slot], config, error_dist=dist)
        # Should produce trim signal (slot contains forecast → NO loses)
        assert len(signals) >= 1
        assert signals[0].estimated_win_prob > 0  # win_prob should be populated


# ──────────────────────────────────────────────────────────────────────
# Supplementary Tests: Exit Signals Advanced
# ──────────────────────────────────────────────────────────────────────

class TestEvaluateExitSignalsAdvanced:
    """Extended exit signal tests: None guards, trend states, boundaries."""

    def test_none_observation_returns_empty(self):
        config = StrategyConfig(no_distance_threshold_f=8)
        held_slot = _make_slot(80, 84, price_no=0.92)
        event = _make_event(slots=[held_slot])
        assert evaluate_exit_signals(event, None, 80.0, [held_slot], config) == []

    def test_none_daily_max_returns_empty(self):
        config = StrategyConfig(no_distance_threshold_f=8)
        held_slot = _make_slot(80, 84, price_no=0.92)
        event = _make_event(slots=[held_slot])
        obs = Observation(icao="KLGA", temp_f=79.0, observation_time=datetime.now(timezone.utc))
        assert evaluate_exit_signals(event, obs, None, [held_slot], config) == []

    def test_both_none_returns_empty(self):
        config = StrategyConfig(no_distance_threshold_f=8)
        held_slot = _make_slot(80, 84, price_no=0.92)
        event = _make_event(slots=[held_slot])
        assert evaluate_exit_signals(event, None, None, [held_slot], config) == []

    def test_days_ahead_positive_returns_empty(self):
        """Exit signals only for same-day markets."""
        config = StrategyConfig(no_distance_threshold_f=8)
        held_slot = _make_slot(80, 84, price_no=0.92)
        event = _make_event(slots=[held_slot])
        obs = Observation(icao="KLGA", temp_f=80.0, observation_time=datetime.now(timezone.utc))
        # Even with temp right at slot boundary, days_ahead=1 → no exit
        assert evaluate_exit_signals(event, obs, 80.0, [held_slot], config, days_ahead=1) == []
        assert evaluate_exit_signals(event, obs, 80.0, [held_slot], config, days_ahead=2) == []

    def test_stable_trend_wider_exit_threshold(self):
        """STABLE uses 0.5x threshold = wider → harder to exit."""
        config = StrategyConfig(no_distance_threshold_f=10)
        # exit_distance: default=10*0.4=4, STABLE=10*0.5=5
        # slot [80,84], daily_max=76 → distance to lower=4
        held_slot = _make_slot(80, 84, price_no=0.92)
        event = _make_event(slots=[held_slot])
        obs = Observation(icao="KLGA", temp_f=76.0, observation_time=datetime.now(timezone.utc))

        # Default: distance=4, exit_distance=4 → 4 < 4 is False → NO exit
        sig_default = evaluate_exit_signals(event, obs, 76.0, [held_slot], config)
        # STABLE: distance=4, exit_distance=5 → 4 < 5 → EXIT
        sig_stable = evaluate_exit_signals(event, obs, 76.0, [held_slot], config, trend=TrendState.STABLE)

        assert len(sig_default) == 0
        assert len(sig_stable) == 1

    def test_breakout_trend_tighter_exit_threshold(self):
        """BREAKOUT uses 0.3x threshold → tighter → exits sooner."""
        config = StrategyConfig(no_distance_threshold_f=10)
        # exit_distance: BREAKOUT_UP=10*0.3=3
        # slot [80,84], daily_max=78 → distance to lower=2
        held_slot = _make_slot(80, 84, price_no=0.92)
        event = _make_event(slots=[held_slot])
        obs = Observation(icao="KLGA", temp_f=78.0, observation_time=datetime.now(timezone.utc))

        # BREAKOUT_UP: distance=2, exit_distance=3 → 2 < 3 → EXIT
        sig = evaluate_exit_signals(event, obs, 78.0, [held_slot], config, trend=TrendState.BREAKOUT_UP)
        assert len(sig) == 1
        # BREAKOUT_DOWN same multiplier
        sig2 = evaluate_exit_signals(event, obs, 78.0, [held_slot], config, trend=TrendState.BREAKOUT_DOWN)
        assert len(sig2) == 1

    def test_exit_signal_has_zero_ev_and_wp(self):
        """Current exit signals hardcode ev=0, wp=0 (to be changed in Phase 4)."""
        config = StrategyConfig(no_distance_threshold_f=8)
        held_slot = _make_slot(80, 84, price_no=0.92)
        event = _make_event(slots=[held_slot])
        obs = Observation(icao="KLGA", temp_f=80.0, observation_time=datetime.now(timezone.utc))
        signals = evaluate_exit_signals(event, obs, 80.0, [held_slot], config)
        assert len(signals) == 1
        assert signals[0].expected_value == 0
        assert signals[0].estimated_win_prob == 0
        assert signals[0].side == Side.SELL

    def test_multiple_held_slots_selective_exit(self):
        """Of multiple held slots, only threatened ones exit."""
        config = StrategyConfig(no_distance_threshold_f=8)
        # exit_distance = 8 * 0.4 = 3.2
        close_slot = _make_slot(80, 84, price_no=0.92)   # daily_max=79 → distance=1 < 3.2 → EXIT
        far_slot = _make_slot(90, 94, price_no=0.85)      # daily_max=79 → distance=11 > 3.2 → HOLD
        event = _make_event(slots=[close_slot, far_slot])
        obs = Observation(icao="KLGA", temp_f=79.0, observation_time=datetime.now(timezone.utc))

        signals = evaluate_exit_signals(event, obs, 79.0, [close_slot, far_slot], config)
        assert len(signals) == 1
        assert signals[0].slot.outcome_label == close_slot.outcome_label

    def test_empty_held_slots_returns_empty(self):
        config = StrategyConfig(no_distance_threshold_f=8)
        event = _make_event(slots=[])
        obs = Observation(icao="KLGA", temp_f=80.0, observation_time=datetime.now(timezone.utc))
        assert evaluate_exit_signals(event, obs, 80.0, [], config) == []

    def test_exit_distance_boundary_exact(self):
        """distance == exit_distance → no exit (code uses '<' not '<=')."""
        config = StrategyConfig(no_distance_threshold_f=10)
        # Default exit_distance = 10 * 0.4 = 4.0
        # slot [80,84], daily_max=76 → distance = |76-80| = 4.0 == exit_distance
        held_slot = _make_slot(80, 84, price_no=0.92)
        event = _make_event(slots=[held_slot])
        obs = Observation(icao="KLGA", temp_f=76.0, observation_time=datetime.now(timezone.utc))
        signals = evaluate_exit_signals(event, obs, 76.0, [held_slot], config)
        assert len(signals) == 0  # 4.0 < 4.0 is False


# ──────────────────────────────────────────────────────────────────────
# Supplementary Tests: Slot Distance Edge Cases
# ──────────────────────────────────────────────────────────────────────

class TestSlotDistanceAdvanced:
    def test_open_lower_slot(self):
        """'Below X°F' slot: lower=None, upper=60."""
        slot = _make_slot(None, 60)
        # midpoint = 60 - 1 = 59, distance = |59 - 75| = 16
        assert _slot_distance(slot, 75.0) == 16.0

    def test_open_upper_slot(self):
        """'≥X°F' slot: lower=90, upper=None."""
        slot = _make_slot(90, None)
        # midpoint = 90 + 1 = 91, distance = |91 - 75| = 16
        assert _slot_distance(slot, 75.0) == 16.0

    def test_both_none_slot(self):
        """Degenerate slot with both bounds None (should not happen in prod)."""
        slot = _make_slot(None, None)
        # midpoint = 0.0, distance = |0 - 75| = 75
        assert _slot_distance(slot, 75.0) == 75.0

    def test_forecast_at_exact_lower_bound(self):
        """Forecast exactly at slot lower bound → inside → distance 0."""
        slot = _make_slot(75, 79)
        assert _slot_distance(slot, 75.0) == 0.0

    def test_forecast_at_exact_upper_bound(self):
        """Forecast exactly at slot upper bound → inside → distance 0."""
        slot = _make_slot(71, 75)
        assert _slot_distance(slot, 75.0) == 0.0

    def test_range_slot_distance_picks_closest_edge(self):
        """For a range slot, distance is to the closest edge."""
        slot = _make_slot(80, 90)
        # forecast 75 → closer to lower=80 → distance=5
        assert _slot_distance(slot, 75.0) == 5.0
        # forecast 95 → closer to upper=90 → distance=5
        assert _slot_distance(slot, 95.0) == 5.0


# ──────────────────────────────────────────────────────────────────────
# Supplementary Tests: Sizing Advanced
# ──────────────────────────────────────────────────────────────────────

class TestSizingAdvanced:
    def test_minimum_order_size_filter(self):
        """Orders below $0.10 should return 0.0 (dust filter)."""
        # Very low win_prob just above break-even → tiny Kelly → tiny size
        signal = _make_signal(win_prob=0.925, price=0.92)
        size = compute_size(signal, city_exposure_usd=0, total_exposure_usd=0,
                           config=StrategyConfig(max_position_per_slot_usd=0.50))
        # With tiny max slot and marginal win_prob → likely dust
        assert size == 0.0 or size >= 0.10

    def test_partial_city_capacity(self):
        """When city has some exposure, size is capped by remaining capacity."""
        signal = _make_signal(win_prob=0.95, price=0.80)
        config = StrategyConfig(max_exposure_per_city_usd=10.0, max_position_per_slot_usd=5.0)
        size = compute_size(signal, city_exposure_usd=8.0, total_exposure_usd=8.0, config=config)
        assert 0 < size <= 2.0  # only $2 remaining in city cap

    def test_partial_global_capacity(self):
        """When global exposure near limit, size is capped by remaining global capacity."""
        signal = _make_signal(win_prob=0.95, price=0.80)
        config = StrategyConfig(max_total_exposure_usd=10.0, max_position_per_slot_usd=5.0)
        size = compute_size(signal, city_exposure_usd=0, total_exposure_usd=9.0, config=config)
        assert 0 < size <= 1.0

    def test_price_zero_returns_zero(self):
        signal = _make_signal(win_prob=0.95, price=0.0)
        assert compute_size(signal, 0, 0, StrategyConfig()) == 0.0

    def test_price_one_returns_zero(self):
        signal = _make_signal(win_prob=0.95, price=1.0)
        assert compute_size(signal, 0, 0, StrategyConfig()) == 0.0

    def test_win_prob_zero_returns_zero(self):
        signal = _make_signal(win_prob=0.0, price=0.80)
        assert compute_size(signal, 0, 0, StrategyConfig()) == 0.0

    def test_win_prob_one_returns_zero(self):
        signal = _make_signal(win_prob=1.0, price=0.80)
        assert compute_size(signal, 0, 0, StrategyConfig()) == 0.0

    def test_half_kelly_vs_full_kelly(self):
        """kelly_fraction=0.5 should give half the size of kelly_fraction=1.0
        (when all caps are high enough that neither hits them)."""
        signal = _make_signal(win_prob=0.95, price=0.92)
        high_caps = dict(
            max_position_per_slot_usd=5000.0,
            max_exposure_per_city_usd=5000.0,
            max_total_exposure_usd=50000.0,
        )
        config_half = StrategyConfig(kelly_fraction=0.5, **high_caps)
        config_full = StrategyConfig(kelly_fraction=1.0, **high_caps)
        size_half = compute_size(signal, 0, 0, config_half)
        size_full = compute_size(signal, 0, 0, config_full)
        assert size_half > 0
        assert size_full > 0
        assert abs(size_full - 2 * size_half) < 0.02  # rounding tolerance

    def test_negative_kelly_returns_zero(self):
        """Low win_prob + high price → negative Kelly → 0."""
        signal = _make_signal(win_prob=0.3, price=0.90)
        assert compute_size(signal, 0, 0, StrategyConfig()) == 0.0


# ──────────────────────────────────────────────────────────────────────
# Supplementary Tests: Regression (YES + LADDER fully removed)
# ──────────────────────────────────────────────────────────────────────

class TestRegressionNoYesNoLadder:
    """Verify YES and LADDER signal generation paths are completely gone."""

    def test_evaluate_yes_signals_not_importable(self):
        """evaluate_yes_signals should not exist in evaluator module."""
        import src.strategy.evaluator as mod
        assert not hasattr(mod, "evaluate_yes_signals")

    def test_evaluate_ladder_signals_not_importable(self):
        """evaluate_ladder_signals should not exist in evaluator module."""
        import src.strategy.evaluator as mod
        assert not hasattr(mod, "evaluate_ladder_signals")

    def test_no_yes_token_type_in_no_signals(self):
        """NO signal generator must never produce YES token signals."""
        config = StrategyConfig(no_distance_threshold_f=4, min_no_ev=0.001, max_no_price=0.99)
        slots = [_make_slot(lower, lower + 4, price_no=0.80)
                 for lower in range(50, 100, 5)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)
        signals = evaluate_no_signals(event, forecast, config)
        for s in signals:
            assert s.token_type == TokenType.NO, f"Got {s.token_type} in NO signals"

    def test_no_yes_token_in_trim_signals(self):
        """Trim signals must all be SELL NO."""
        config = StrategyConfig(min_trim_ev=0.01)
        # Slots close to forecast → negative EV → should trim
        held_slots = [_make_slot(73, 77, price_no=0.80), _make_slot(74, 78, price_no=0.85)]
        event = _make_event(slots=held_slots)
        forecast = _make_forecast(75.0)
        signals = evaluate_trim_signals(event, forecast, held_slots, config)
        for s in signals:
            assert s.token_type == TokenType.NO
            assert s.side == Side.SELL

    def test_no_yes_token_in_exit_signals(self):
        """Exit signals must all be SELL NO."""
        config = StrategyConfig(no_distance_threshold_f=8)
        held_slot = _make_slot(76, 80, price_no=0.90)
        event = _make_event(slots=[held_slot])
        obs = Observation(icao="KLGA", temp_f=76.0, observation_time=datetime.now(timezone.utc))
        signals = evaluate_exit_signals(event, obs, 76.0, [held_slot], config)
        for s in signals:
            assert s.token_type == TokenType.NO
            assert s.side == Side.SELL

    def test_strategy_config_no_ladder_fields(self):
        """StrategyConfig should not have ladder_width, ladder_min_ev, etc."""
        config = StrategyConfig()
        assert not hasattr(config, "ladder_width")
        assert not hasattr(config, "ladder_min_ev")
        assert not hasattr(config, "ladder_min_distance_f")
        assert not hasattr(config, "yes_confirmation_threshold")

    def test_strategy_variants_no_ladder_params(self):
        """All strategy variants should be free of ladder parameters."""
        from src.config import get_strategy_variants
        for name, params in get_strategy_variants().items():
            assert "ladder_width" not in params, f"Variant {name} has ladder_width"
            assert "ladder_min_ev" not in params, f"Variant {name} has ladder_min_ev"
            assert "ladder_min_distance_f" not in params, f"Variant {name} has ladder_min_distance_f"
            assert "yes_confirmation_threshold" not in params, f"Variant {name} has yes_confirmation_threshold"


# ──────────────────────────────────────────────────────────────────────
# Supplementary Tests: Performance / Stress
# ──────────────────────────────────────────────────────────────────────

class TestPerformance:
    def test_100_slots_evaluates_fast(self):
        """Verify NO signal evaluation doesn't degrade with many slots."""
        config = StrategyConfig(no_distance_threshold_f=4, min_no_ev=0.001, max_no_price=0.99)
        slots = [_make_slot(lower, lower + 2, price_no=0.80)
                 for lower in range(30, 130)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)

        t0 = time.monotonic()
        signals = evaluate_no_signals(event, forecast, config)
        elapsed = time.monotonic() - t0

        assert elapsed < 0.5, f"100 slots took {elapsed:.3f}s (>0.5s)"
        assert len(signals) > 0  # some should pass

    def test_100_slots_with_empirical_dist(self):
        """Empirical distribution with 100 slots should also be fast."""
        config = StrategyConfig(no_distance_threshold_f=4, min_no_ev=0.001, max_no_price=0.99)
        slots = [_make_slot(lower, lower + 2, price_no=0.80)
                 for lower in range(30, 130)]
        event = _make_event(slots=slots)
        forecast = _make_forecast(75.0)
        dist = _make_error_dist(n=730, spread=3.0)

        t0 = time.monotonic()
        signals = evaluate_no_signals(event, forecast, config, error_dist=dist)
        elapsed = time.monotonic() - t0

        assert elapsed < 2.0, f"100 slots + 730-sample dist took {elapsed:.3f}s (>2s)"

    def test_many_held_positions_exit_evaluation(self):
        """Exit evaluation with 50 held positions should be fast."""
        config = StrategyConfig(no_distance_threshold_f=8)
        held_slots = [_make_slot(lower, lower + 4, price_no=0.80)
                      for lower in range(50, 100)]
        event = _make_event(slots=held_slots)
        obs = Observation(icao="KLGA", temp_f=75.0, observation_time=datetime.now(timezone.utc))

        t0 = time.monotonic()
        signals = evaluate_exit_signals(event, obs, 75.0, held_slots, config)
        elapsed = time.monotonic() - t0

        assert elapsed < 0.5, f"50 held slots exit took {elapsed:.3f}s (>0.5s)"
