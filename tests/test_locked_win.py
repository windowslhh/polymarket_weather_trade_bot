"""Tests for locked-win signals.

Updated for wu_round + asymmetric time-gate semantics:
- Condition A (below-slot: daily_max > upper): fires immediately, no time gate.
- Condition B (above-slot: daily_max < lower): requires daily_max_final=True.

All tests use locked_win_margin_f=0 to test core locking logic without margin
interference.  Margin-specific tests are in test_temperature_boundaries.py.
"""
from __future__ import annotations

import time
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from src.config import StrategyConfig
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.strategy.evaluator import evaluate_locked_win_signals
from src.strategy.sizing import compute_size
from src.weather.metar import DailyMaxTracker
from src.weather.models import Observation

# Zero-margin config: tests core locking logic without safety buffer
_CFG = StrategyConfig(locked_win_margin_f=0)


def _slot(lower, upper, price_no=0.90, tid_no="no_1"):
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


# ──────────────────────────────────────────────────────────────────────
# Critical Paths: locked-win detection logic
# ──────────────────────────────────────────────────────────────────────

class TestLockedWinDetection:
    """Core locked-win signal generation (condition A: below-slot)."""

    def test_range_slot_locked_when_daily_max_above_upper(self):
        """[70,74] with daily_max=76 → wu_round=76 > 74 → locked win."""
        slot = _slot(70, 74)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 76.0, _CFG, daily_max_final=True)
        assert len(sigs) == 1
        assert sigs[0].token_type == TokenType.NO
        assert sigs[0].side == Side.BUY
        assert sigs[0].estimated_win_prob == 0.99
        assert sigs[0].is_locked_win is True

    def test_range_slot_not_locked_when_daily_max_inside(self):
        """[70,74] with daily_max=72 → NOT locked (temp in range)."""
        slot = _slot(70, 74)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 72.0, _CFG, daily_max_final=True)
        assert len(sigs) == 0

    def test_range_slot_condition_b_locks_when_final(self):
        """[70,74] with daily_max=68, daily_max_final=True → condition B locks.
        wu_round(68)=68 < 70, 70-68=2 >= 0 margin → locked (above-slot)."""
        slot = _slot(70, 74)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 68.0, _CFG, daily_max_final=True)
        # Condition B fires when daily_max_final=True
        assert len(sigs) == 1

    def test_range_slot_condition_b_not_locked_when_not_final(self):
        """[70,74] with daily_max=68, NOT final → no lock (temp could still rise)."""
        slot = _slot(70, 74)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 68.0, _CFG, daily_max_final=False)
        assert len(sigs) == 0

    def test_condition_a_locks_without_daily_max_final(self):
        """[70,74] with daily_max=76, NOT final → Condition A fires immediately.
        daily_max is monotonically increasing — once above upper it can never fall back."""
        slot = _slot(70, 74)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 76.0, _CFG, daily_max_final=False)
        assert len(sigs) == 1, "Condition A (below-slot) must lock even before peak window"

    def test_below_x_slot_locked(self):
        """'Below 60°F' (lower=None, upper=60) with daily_max=62 → locked win."""
        slot = _slot(None, 60)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 62.0, _CFG, daily_max_final=True)
        assert len(sigs) == 1

    def test_below_x_slot_not_locked(self):
        """'Below 60°F' with daily_max=58 → NOT locked (max is below upper)."""
        slot = _slot(None, 60)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 58.0, _CFG, daily_max_final=True)
        assert len(sigs) == 0

    def test_open_upper_slot_skipped_when_max_above(self):
        """'≥90°F' with daily_max=95 → YES wins → NO loses → skip."""
        slot = _slot(90, None)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 95.0, _CFG, daily_max_final=True)
        assert len(sigs) == 0

    def test_open_upper_slot_locked_when_max_below_final(self):
        """'≥90°F' with daily_max=85, final → wu_round=85 < 90 → condition B → locked."""
        slot = _slot(90, None)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 85.0, _CFG, daily_max_final=True)
        assert len(sigs) == 1

    def test_open_upper_slot_not_locked_when_not_final(self):
        """'≥90°F' with daily_max=85, NOT final → could still reach 90 → no lock."""
        slot = _slot(90, None)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 85.0, _CFG, daily_max_final=False)
        assert len(sigs) == 0

    def test_multiple_slots_selective_lock(self):
        """Multiple slots: locks both below AND above daily_max (when final)."""
        slots = [
            _slot(60, 64, tid_no="n1"),   # locked below (76 > 64)
            _slot(65, 69, tid_no="n2"),   # locked below (76 > 69)
            _slot(70, 74, tid_no="n3"),   # locked below (76 > 74)
            _slot(75, 79, tid_no="n4"),   # NOT locked (76 inside, wu_round=76 is in [75,79])
            _slot(80, 84, tid_no="n5"),   # locked above (76 < 80, final)
        ]
        event = _event(slots)
        sigs = evaluate_locked_win_signals(event, 76.0, _CFG, daily_max_final=True)
        locked_ids = {s.slot.token_id_no for s in sigs}
        assert locked_ids == {"n1", "n2", "n3", "n5"}

    def test_ev_positive_for_reasonable_price(self):
        """Locked win at price_no=0.90: EV is positive (after entry fee deduction)."""
        from src.strategy.evaluator import TAKER_FEE_RATE
        slot = _slot(70, 74, price_no=0.90)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 76.0, _CFG, daily_max_final=True)
        assert len(sigs) == 1
        p = 0.90
        fee = TAKER_FEE_RATE * 2.0 * p * (1.0 - p)
        expected_ev = 0.99 * (1.0 - p) - 0.01 * p - fee
        assert abs(sigs[0].expected_value - expected_ev) < 1e-9
        assert sigs[0].expected_value > 0, "EV must remain positive after fee deduction"

    def test_all_signals_are_no_buy(self):
        """Every locked win signal must be NO/BUY."""
        slots = [_slot(60 + i * 5, 64 + i * 5, tid_no=f"n{i}") for i in range(5)]
        event = _event(slots)
        sigs = evaluate_locked_win_signals(event, 90.0, _CFG, daily_max_final=True)
        for s in sigs:
            assert s.token_type == TokenType.NO
            assert s.side == Side.BUY
            assert s.is_locked_win is True


# ──────────────────────────────────────────────────────────────────────
# Boundary Conditions
# ──────────────────────────────────────────────────────────────────────

class TestLockedWinBoundary:

    def test_daily_max_exactly_at_upper_not_locked(self):
        """[70,74] with daily_max=74 → wu_round=74, NOT > 74 → NOT locked."""
        slot = _slot(70, 74)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 74.0, _CFG, daily_max_final=True)
        assert len(sigs) == 0

    def test_daily_max_half_above_upper_locked(self):
        """[70,74] with daily_max=74.5 → wu_round=75 > 74 → locked."""
        slot = _slot(70, 74)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 74.5, _CFG, daily_max_final=True)
        assert len(sigs) == 1

    def test_daily_max_just_below_half_not_locked(self):
        """[70,74] with daily_max=74.4 → wu_round=74, NOT > 74 → NOT locked."""
        slot = _slot(70, 74)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 74.4, _CFG, daily_max_final=True)
        assert len(sigs) == 0

    def test_price_no_zero_skipped(self):
        """price_no=0 → skip (invalid price)."""
        slot = _slot(70, 74, price_no=0.0)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 76.0, _CFG, daily_max_final=True)
        assert len(sigs) == 0

    def test_price_no_one_skipped(self):
        """price_no=1.0 → skip (invalid price)."""
        slot = _slot(70, 74, price_no=1.0)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 76.0, _CFG, daily_max_final=True)
        assert len(sigs) == 0

    def test_price_no_very_high_negative_ev(self):
        """price_no=0.999 → EV negative → no signal."""
        slot = _slot(70, 74, price_no=0.999)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 76.0, _CFG, daily_max_final=True)
        assert len(sigs) == 0

    def test_price_no_very_low_filtered(self):
        """price_no=0.10 < min_no_price(0.20) → filtered out."""
        slot = _slot(70, 74, price_no=0.10)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 76.0, _CFG, daily_max_final=True)
        assert len(sigs) == 0

    def test_price_no_at_min_accepted(self):
        """price_no=0.20 == min_no_price → accepted (not filtered)."""
        slot = _slot(70, 74, price_no=0.20)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 76.0, _CFG, daily_max_final=True)
        assert len(sigs) == 1
        assert sigs[0].expected_value > 0

    def test_both_bounds_none_slot_no_lock(self):
        """Degenerate slot (both None) → no lock."""
        slot = _slot(None, None)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 100.0, _CFG, daily_max_final=True)
        assert len(sigs) == 0

    @pytest.mark.parametrize("daily_max_f,expected_locked", [
        # Slot [50, 54]: Condition A fires when wu_round(daily_max) > 54
        (54.4, False),   # wu_round(54.4) = 54 — NOT > 54
        (54.5, True),    # wu_round(54.5) = 55 — 55 > 54 → Condition A locks
        (54.6, True),    # wu_round(54.6) = 55 — 55 > 54 → Condition A locks
        (55.0, True),    # wu_round(55.0) = 55 — 55 > 54 → Condition A locks
        (55.5, True),    # wu_round(55.5) = 56 — 56 > 54 → Condition A locks
    ])
    def test_wu_round_boundary_condition_a(self, daily_max_f, expected_locked):
        """Verify wu_round half-up semantics at slot upper boundary.
        Condition A does not require daily_max_final."""
        slot = _slot(50, 54)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, daily_max_f, _CFG, daily_max_final=False)
        assert (len(sigs) == 1) == expected_locked, (
            f"daily_max={daily_max_f}: expected locked={expected_locked}"
        )


# ──────────────────────────────────────────────────────────────────────
# Failure Branches
# ──────────────────────────────────────────────────────────────────────

class TestLockedWinFailure:

    def test_none_daily_max_returns_empty(self):
        slot = _slot(70, 74)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, None, _CFG, daily_max_final=True)
        assert sigs == []

    def test_days_ahead_positive_returns_empty(self):
        """Locked wins only for same-day markets."""
        slot = _slot(70, 74)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 76.0, _CFG, days_ahead=1, daily_max_final=True)
        assert sigs == []
        sigs2 = evaluate_locked_win_signals(event, 76.0, _CFG, days_ahead=2, daily_max_final=True)
        assert sigs2 == []

    def test_disabled_config_returns_empty(self):
        """enable_locked_wins=False → no signals."""
        slot = _slot(70, 74)
        event = _event([slot])
        cfg = StrategyConfig(enable_locked_wins=False, locked_win_margin_f=0)
        sigs = evaluate_locked_win_signals(event, 76.0, cfg, daily_max_final=True)
        assert sigs == []

    def test_condition_b_not_final_returns_empty(self):
        """daily_max_final=False blocks Condition B (above-slot) but not Condition A.
        Use a slot where daily_max is BELOW its lower bound to isolate Condition B."""
        slot = _slot(80, 84)  # daily_max=76 < 80 → Condition B scenario
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 76.0, _CFG, daily_max_final=False)
        assert sigs == [], "Condition B must NOT fire without daily_max_final"

    def test_empty_slots_returns_empty(self):
        event = _event([])
        sigs = evaluate_locked_win_signals(event, 76.0, _CFG, daily_max_final=True)
        assert sigs == []

    def test_held_token_filtered(self):
        """Already-held token → skipped."""
        slot = _slot(70, 74, tid_no="held_token")
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 76.0, _CFG,
                                           held_token_ids={"held_token"},
                                           daily_max_final=True)
        assert sigs == []

    def test_held_filter_selective(self):
        """Hold one token, not the other → only unheld gets signal."""
        slot_a = _slot(60, 64, tid_no="held")
        slot_b = _slot(65, 69, tid_no="free")
        event = _event([slot_a, slot_b])
        sigs = evaluate_locked_win_signals(event, 76.0, _CFG,
                                           held_token_ids={"held"},
                                           daily_max_final=True)
        assert len(sigs) == 1
        assert sigs[0].slot.token_id_no == "free"


# ──────────────────────────────────────────────────────────────────────
# Sizing Integration: locked-win full Kelly
# ──────────────────────────────────────────────────────────────────────

class TestLockedWinSizing:

    def _locked_signal(self, price_no=0.90) -> TradeSignal:
        slot = _slot(70, 74, price_no=price_no)
        event = _event([slot])
        return TradeSignal(
            token_type=TokenType.NO, side=Side.BUY,
            slot=slot, event=event,
            expected_value=0.09, estimated_win_prob=0.99,
            is_locked_win=True,
        )

    def _normal_signal(self, price_no=0.90) -> TradeSignal:
        slot = _slot(85, 89, price_no=price_no)
        event = _event([slot])
        return TradeSignal(
            token_type=TokenType.NO, side=Side.BUY,
            slot=slot, event=event,
            expected_value=0.05, estimated_win_prob=0.95,
        )

    def test_locked_uses_higher_cap(self):
        """Locked win uses max_locked_win_per_slot_usd (10) not max_position_per_slot_usd (5)."""
        cfg = StrategyConfig(max_locked_win_per_slot_usd=10.0, max_position_per_slot_usd=5.0)
        locked_size = compute_size(self._locked_signal(), 0, 0, cfg)
        normal_size = compute_size(self._normal_signal(), 0, 0, cfg)
        assert locked_size > normal_size
        assert locked_size <= 10.0
        assert normal_size <= 5.0

    def test_locked_uses_full_kelly(self):
        """Locked win with kelly_fraction=1.0 → larger than half-Kelly normal."""
        cfg = StrategyConfig(
            kelly_fraction=0.5, locked_win_kelly_fraction=1.0,
            max_locked_win_per_slot_usd=100.0, max_position_per_slot_usd=100.0,
            max_exposure_per_city_usd=500.0, max_total_exposure_usd=5000.0,
        )
        locked_size = compute_size(self._locked_signal(), 0, 0, cfg)
        normal_size = compute_size(self._normal_signal(), 0, 0, cfg)
        assert locked_size > normal_size

    def test_locked_respects_city_cap(self):
        """Even locked wins cannot exceed city exposure limit."""
        cfg = StrategyConfig(
            max_locked_win_per_slot_usd=10.0,
            max_exposure_per_city_usd=3.0,
        )
        size = compute_size(self._locked_signal(), city_exposure_usd=2.0,
                           total_exposure_usd=2.0, config=cfg)
        assert size <= 1.0

    def test_locked_respects_global_cap(self):
        """Even locked wins cannot exceed global exposure limit."""
        cfg = StrategyConfig(
            max_locked_win_per_slot_usd=10.0,
            max_total_exposure_usd=5.0,
        )
        size = compute_size(self._locked_signal(), city_exposure_usd=0,
                           total_exposure_usd=4.5, config=cfg)
        assert size <= 0.5

    def test_locked_dust_filter_still_applies(self):
        """Locked wins below $0.10 still filtered as dust."""
        cfg = StrategyConfig(
            max_locked_win_per_slot_usd=0.05,
        )
        size = compute_size(self._locked_signal(), 0, 0, cfg)
        assert size == 0.0

    def test_normal_signal_unaffected(self):
        """Signal without is_locked_win=True uses normal sizing path."""
        cfg = StrategyConfig(
            kelly_fraction=0.5, max_position_per_slot_usd=5.0,
            locked_win_kelly_fraction=1.0, max_locked_win_per_slot_usd=10.0,
        )
        size = compute_size(self._normal_signal(), 0, 0, cfg)
        assert size <= 5.0


# ──────────────────────────────────────────────────────────────────────
# DailyMaxTracker: return type change
# ──────────────────────────────────────────────────────────────────────

class TestDailyMaxTrackerReturnType:

    def test_update_returns_tuple(self):
        tracker = DailyMaxTracker()
        obs = Observation(icao="KLGA", temp_f=72.0,
                         observation_time=datetime(2026, 4, 10, 10, 0, tzinfo=timezone.utc))
        result = tracker.update(obs)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_first_obs_is_new_high(self):
        tracker = DailyMaxTracker()
        obs = Observation(icao="KLGA", temp_f=72.0,
                         observation_time=datetime(2026, 4, 10, 10, 0, tzinfo=timezone.utc))
        daily_max, is_new_high = tracker.update(obs)
        assert daily_max == 72.0
        assert is_new_high is True

    def test_higher_temp_is_new_high(self):
        tracker = DailyMaxTracker()
        obs1 = Observation(icao="KLGA", temp_f=72.0,
                          observation_time=datetime(2026, 4, 10, 10, 0, tzinfo=timezone.utc))
        obs2 = Observation(icao="KLGA", temp_f=78.0,
                          observation_time=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        tracker.update(obs1)
        daily_max, is_new_high = tracker.update(obs2)
        assert daily_max == 78.0
        assert is_new_high is True

    def test_lower_temp_not_new_high(self):
        tracker = DailyMaxTracker()
        obs1 = Observation(icao="KLGA", temp_f=78.0,
                          observation_time=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        obs2 = Observation(icao="KLGA", temp_f=72.0,
                          observation_time=datetime(2026, 4, 10, 18, 0, tzinfo=timezone.utc))
        tracker.update(obs1)
        daily_max, is_new_high = tracker.update(obs2)
        assert daily_max == 78.0
        assert is_new_high is False

    def test_equal_temp_not_new_high(self):
        tracker = DailyMaxTracker()
        obs1 = Observation(icao="KLGA", temp_f=75.0,
                          observation_time=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc))
        obs2 = Observation(icao="KLGA", temp_f=75.0,
                          observation_time=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        tracker.update(obs1)
        daily_max, is_new_high = tracker.update(obs2)
        assert daily_max == 75.0
        assert is_new_high is False

    def test_new_high_on_different_day(self):
        tracker = DailyMaxTracker()
        obs1 = Observation(icao="KLGA", temp_f=80.0,
                          observation_time=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        obs2 = Observation(icao="KLGA", temp_f=65.0,
                          observation_time=datetime(2026, 4, 11, 8, 0, tzinfo=timezone.utc))
        tracker.update(obs1)
        daily_max, is_new_high = tracker.update(obs2)
        assert daily_max == 65.0
        assert is_new_high is True

    def test_get_max_still_works(self):
        tracker = DailyMaxTracker()
        obs = Observation(icao="KLGA", temp_f=80.0,
                         observation_time=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        tracker.update(obs)
        assert tracker.get_max("KLGA", day=date(2026, 4, 10)) == 80.0
        assert tracker.get_max("KLGA", day=date(2026, 4, 9)) is None


# ──────────────────────────────────────────────────────────────────────
# Config fields
# ──────────────────────────────────────────────────────────────────────

class TestLockedWinConfig:

    def test_defaults(self):
        cfg = StrategyConfig()
        assert cfg.enable_locked_wins is True
        assert cfg.locked_win_kelly_fraction == 1.0
        assert cfg.max_locked_win_per_slot_usd == 10.0
        assert cfg.locked_win_margin_f == 2
        assert cfg.post_peak_hour == 17
        assert cfg.stability_window_minutes == 60

    def test_override(self):
        cfg = StrategyConfig(enable_locked_wins=False,
                            locked_win_kelly_fraction=0.8,
                            max_locked_win_per_slot_usd=20.0,
                            locked_win_margin_f=3)
        assert cfg.enable_locked_wins is False
        assert cfg.locked_win_kelly_fraction == 0.8
        assert cfg.max_locked_win_per_slot_usd == 20.0
        assert cfg.locked_win_margin_f == 3

    def test_dataclass_replace(self):
        cfg = StrategyConfig()
        cfg2 = replace(cfg, enable_locked_wins=False)
        assert cfg2.enable_locked_wins is False
        assert cfg.enable_locked_wins is True


# ──────────────────────────────────────────────────────────────────────
# Regression: locked wins never produce YES signals
# ──────────────────────────────────────────────────────────────────────

class TestLockedWinRegression:

    def test_never_produces_yes(self):
        """Locked win signals must always be NO/BUY."""
        slots = [_slot(50 + i * 5, 54 + i * 5, tid_no=f"n{i}") for i in range(10)]
        event = _event(slots)
        sigs = evaluate_locked_win_signals(event, 110.0, _CFG, daily_max_final=True)
        assert len(sigs) > 0
        for s in sigs:
            assert s.token_type == TokenType.NO
            assert s.side == Side.BUY

    def test_rebalancer_imports_locked_win(self):
        """Rebalancer module should have evaluate_locked_win_signals."""
        import src.strategy.rebalancer as mod
        assert hasattr(mod, "evaluate_locked_win_signals")


# ──────────────────────────────────────────────────────────────────────
# Performance
# ──────────────────────────────────────────────────────────────────────

class TestLockedWinPerformance:

    def test_100_slots_fast(self):
        """100 slots locked-win evaluation should be <50ms."""
        slots = [_slot(50 + i, 50 + i + 4, tid_no=f"n{i}") for i in range(100)]
        event = _event(slots)

        t0 = time.monotonic()
        for _ in range(100):
            evaluate_locked_win_signals(event, 80.0, _CFG, daily_max_final=True)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"100×100 slots took {elapsed:.3f}s"

    def test_tracker_many_updates_fast(self):
        """1000 tracker updates in <100ms."""
        tracker = DailyMaxTracker()
        base = datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc)
        from datetime import timedelta
        t0 = time.monotonic()
        for i in range(1000):
            obs = Observation(
                icao="KLGA", temp_f=60.0 + (i % 30),
                observation_time=base + timedelta(minutes=i),
            )
            tracker.update(obs)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, f"1000 updates took {elapsed:.3f}s"
