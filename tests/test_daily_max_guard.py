"""Tests for Bug 1 (daily_max in slot blocks NO signal) and Bug 2 (negative hours
doesn't trigger force exit).

Bug 1: evaluate_no_signals() must skip range slots where wu_round(daily_max) falls
inside [lower, upper] — the actual high has entered the slot, so NO is a loser.
The existing guard for open-ended ≥X slots is also verified.

Bug 2: evaluate_exit_signals() Layer 3 force exit must reject negative
hours_to_settlement (expired markets).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.config import StrategyConfig
from src.markets.models import Side, TempSlot, TokenType, WeatherMarketEvent
from src.strategy.evaluator import evaluate_exit_signals, evaluate_no_signals
from src.weather.models import Forecast, Observation


# ── Helpers ──────────────────────────────────────────────────────────

def _slot(lower, upper, price_no=0.80, tid_no="no_1"):
    label = ""
    if lower is not None and upper is not None:
        label = f"{lower}°F to {upper}°F"
    elif lower is not None:
        label = f"{lower}°F or above"
    elif upper is not None:
        label = f"Below {upper}°F"
    return TempSlot(
        token_id_yes="yes_1", token_id_no=tid_no,
        outcome_label=label, temp_lower_f=lower, temp_upper_f=upper,
        price_yes=1.0 - price_no, price_no=price_no,
    )


def _event(slots, city="NYC"):
    return WeatherMarketEvent(
        event_id="e1", condition_id="c1", city=city,
        market_date=date.today(), slots=slots,
        end_timestamp=datetime(2026, 4, 10, 23, 0, tzinfo=timezone.utc),
        title=f"Highest temperature in {city}",
    )


def _forecast(high=75.0):
    return Forecast(
        city="NYC", forecast_date=date.today(),
        predicted_high_f=high, predicted_low_f=high - 15,
        confidence_interval_f=4.0, source="test",
        fetched_at=datetime.now(timezone.utc),
    )


def _obs(temp_f=79.0):
    return Observation(icao="KLGA", temp_f=temp_f,
                       observation_time=datetime.now(timezone.utc))


# Wide distance threshold so the slot passes the distance filter —
# the daily_max guard should be the thing that blocks it.
_CFG = StrategyConfig(no_distance_threshold_f=2.0, min_no_ev=-1.0)


# ──────────────────────────────────────────────────────────────────────
# Bug 1: daily_max in range slot blocks NO signal
# ──────────────────────────────────────────────────────────────────────

class TestDailyMaxRangeSlotGuard:
    """evaluate_no_signals must skip range slots where daily_max is in [L, U]."""

    def test_daily_max_inside_range_blocks_no_signal(self):
        """[70,79] with daily_max=75 → wu_round(75)=75 in [70,79] → skip."""
        slot = _slot(70, 79)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(60.0), _CFG,
            days_ahead=0, daily_max_f=75.0,
        )
        assert len(sigs) == 0

    def test_daily_max_at_lower_bound_blocks(self):
        """[70,79] with daily_max=70 → wu_round(70)=70 in [70,79] → skip."""
        slot = _slot(70, 79)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(60.0), _CFG,
            days_ahead=0, daily_max_f=70.0,
        )
        assert len(sigs) == 0

    def test_daily_max_at_upper_bound_blocks(self):
        """[70,79] with daily_max=79 → wu_round(79)=79 in [70,79] → skip."""
        slot = _slot(70, 79)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(60.0), _CFG,
            days_ahead=0, daily_max_f=79.0,
        )
        assert len(sigs) == 0

    def test_daily_max_below_range_allowed(self):
        """[70,79] with daily_max=65 → wu_round(65)=65 < 70 → allowed."""
        slot = _slot(70, 79)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(60.0), _CFG,
            days_ahead=0, daily_max_f=65.0,
        )
        # Signal should be generated (daily_max is below range)
        assert len(sigs) >= 1

    def test_daily_max_above_range_allowed(self):
        """[70,74] with daily_max=76 → wu_round(76)=76 > 74 → allowed.
        (This is a locked-win scenario handled by evaluate_locked_win_signals.)"""
        slot = _slot(70, 74)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(60.0), _CFG,
            days_ahead=0, daily_max_f=76.0,
        )
        # wu_round(76)=76 > 74 → NOT inside range → not blocked by this guard
        assert len(sigs) >= 1

    def test_wu_round_half_up_enters_range(self):
        """[70,79] with daily_max=69.5 → wu_round(69.5)=70 in [70,79] → skip."""
        slot = _slot(70, 79)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(60.0), _CFG,
            days_ahead=0, daily_max_f=69.5,
        )
        assert len(sigs) == 0

    def test_wu_round_below_half_stays_out(self):
        """[70,79] with daily_max=69.4 → wu_round(69.4)=69 < 70 → allowed."""
        slot = _slot(70, 79)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(60.0), _CFG,
            days_ahead=0, daily_max_f=69.4,
        )
        assert len(sigs) >= 1

    def test_days_ahead_positive_no_guard(self):
        """Guard only applies to same-day (days_ahead==0). Future days pass through."""
        slot = _slot(70, 79)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(60.0), _CFG,
            days_ahead=1, daily_max_f=75.0,
        )
        # days_ahead=1 → guard doesn't fire
        assert len(sigs) >= 1

    def test_daily_max_none_no_guard(self):
        """Guard only fires when daily_max_f is not None."""
        slot = _slot(70, 79)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(60.0), _CFG,
            days_ahead=0, daily_max_f=None,
        )
        assert len(sigs) >= 1


class TestDailyMaxOpenEndedGuard:
    """Existing guard for ≥X slots: wu_round(daily_max) >= X blocks NO signal."""

    def test_ge_slot_daily_max_above_threshold_blocks(self):
        """'≥80°F' with daily_max=82 → wu_round(82)=82 >= 80 → skip."""
        slot = _slot(80, None)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(60.0), _CFG,
            days_ahead=0, daily_max_f=82.0,
        )
        assert len(sigs) == 0

    def test_ge_slot_daily_max_exactly_at_threshold_blocks(self):
        """'≥80°F' with daily_max=80 → wu_round(80)=80 >= 80 → skip."""
        slot = _slot(80, None)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(60.0), _CFG,
            days_ahead=0, daily_max_f=80.0,
        )
        assert len(sigs) == 0

    def test_ge_slot_daily_max_below_threshold_allowed(self):
        """'≥80°F' with daily_max=75 → wu_round(75)=75 < 80 → allowed."""
        slot = _slot(80, None)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(60.0), _CFG,
            days_ahead=0, daily_max_f=75.0,
        )
        assert len(sigs) >= 1


# ──────────────────────────────────────────────────────────────────────
# Bug 2: negative hours_to_settlement must NOT trigger force exit
# ──────────────────────────────────────────────────────────────────────

class TestNegativeHoursToSettlement:
    """Layer 3 force exit must require 0 <= hours_to_settlement."""

    def test_negative_hours_no_force_exit(self):
        """hours_to_settlement=-16.9 (expired market) → no force exit."""
        config = StrategyConfig(no_distance_threshold_f=10, force_exit_hours=1.0)
        # exit_distance = 10*0.25=2.5, slot [85,89], daily_max=84, distance=1 < 2.5
        # EV positive at price_no=0.50 → normally Layer 3 would force exit
        held = _slot(85, 89, price_no=0.50)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(84.0), 84.0, [held], config,
            forecast=_forecast(75.0),
            hours_to_settlement=-16.9,
        )
        # Negative hours → Layer 3 must NOT fire → positive EV → hold
        assert len(sigs) == 0

    def test_negative_hours_large_magnitude(self):
        """hours_to_settlement=-100 → no force exit."""
        config = StrategyConfig(no_distance_threshold_f=10, force_exit_hours=2.0)
        held = _slot(85, 89, price_no=0.50)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(84.0), 84.0, [held], config,
            forecast=_forecast(75.0),
            hours_to_settlement=-100.0,
        )
        assert len(sigs) == 0

    def test_zero_hours_still_triggers(self):
        """hours_to_settlement=0 → at settlement → force exit fires."""
        config = StrategyConfig(no_distance_threshold_f=10, force_exit_hours=1.0)
        held = _slot(85, 89, price_no=0.50)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(84.0), 84.0, [held], config,
            forecast=_forecast(75.0),
            hours_to_settlement=0.0,
        )
        assert len(sigs) == 1
        assert sigs[0].side == Side.SELL

    def test_positive_hours_within_threshold_triggers(self):
        """hours_to_settlement=0.5, force_exit_hours=1.0 → force exit fires."""
        config = StrategyConfig(no_distance_threshold_f=10, force_exit_hours=1.0)
        held = _slot(85, 89, price_no=0.50)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(84.0), 84.0, [held], config,
            forecast=_forecast(75.0),
            hours_to_settlement=0.5,
        )
        assert len(sigs) == 1
        assert sigs[0].side == Side.SELL

    def test_negative_hours_with_negative_ev_still_exits(self):
        """Negative hours blocks Layer 3 only — Layer 2 (negative EV) still exits."""
        config = StrategyConfig(no_distance_threshold_f=10, force_exit_hours=1.0)
        # slot [74,78], daily_max=75, forecast=76 → inside slot → negative EV → Layer 2 sell
        held = _slot(74, 78, price_no=0.85)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(75.0), 75.0, [held], config,
            forecast=_forecast(76.0),
            hours_to_settlement=-5.0,
        )
        # Layer 2 fires (negative EV), regardless of hours
        assert len(sigs) == 1
        assert sigs[0].side == Side.SELL
