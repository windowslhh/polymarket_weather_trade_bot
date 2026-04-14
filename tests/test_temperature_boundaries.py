"""Boundary tests for wu_round, locked-win semantics, time factor, and settlement precision.

Tests cover:
- wu_round half-up rounding (not banker's) at .4, .5, .6 boundaries
- Locked-win condition A (below-slot) and B (above-slot) with margins
- Time factor: same temperature at 13:00 vs 17:30 produces different results
- Symmetric slot testing: slots above AND below daily_max
- Backtest settlement rounding
- is_daily_max_final stability window
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.config import StrategyConfig
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.strategy.evaluator import evaluate_locked_win_signals, evaluate_exit_signals, evaluate_trim_signals
from src.strategy.temperature import is_daily_max_final, slot_contains_degree, wu_round
from src.weather.models import Forecast, Observation


# ── Helpers ──────────────────────────────────────────────────────────

def _slot(lower, upper, price_no=0.50, tid_no="no_1"):
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


def _obs(temp_f=79.0):
    return Observation(icao="KLGA", temp_f=temp_f,
                       observation_time=datetime.now(timezone.utc))


def _forecast(high=75.0):
    return Forecast(
        city="NYC", forecast_date=date.today(),
        predicted_high_f=high, predicted_low_f=high - 15,
        confidence_interval_f=4.0, source="test",
        fetched_at=datetime.now(timezone.utc),
    )


# ══════════════════════════════════════════════════════════════════════
# wu_round: half-up rounding
# ══════════════════════════════════════════════════════════════════════

class TestWuRound:
    """Weather Underground uses half-up rounding, not banker's."""

    @pytest.mark.parametrize("temp_f, expected", [
        (54.0, 54),
        (54.1, 54),
        (54.4, 54),
        (54.5, 55),    # half-up → 55, NOT banker's 54
        (54.6, 55),
        (54.9, 55),
        (55.0, 55),
        (55.4, 55),
        (55.5, 56),    # half-up → 56, NOT banker's 56 (same here)
        (55.6, 56),
        (55.9, 56),
        (56.0, 56),
        (0.5, 1),      # edge at zero
        (-0.5, 0),     # negative half-up
        (99.5, 100),
    ])
    def test_half_up_rounding(self, temp_f, expected):
        assert wu_round(temp_f) == expected, (
            f"wu_round({temp_f}) = {wu_round(temp_f)}, expected {expected}"
        )

    def test_bankers_would_differ_at_54_5(self):
        """54.5°F: banker's round() gives 54, wu_round gives 55."""
        assert round(54.5) == 54, "Python banker's rounds 54.5 → 54"
        assert wu_round(54.5) == 55, "wu_round must give 55 (half-up)"


# ══════════════════════════════════════════════════════════════════════
# slot_contains_degree
# ══════════════════════════════════════════════════════════════════════

class TestSlotContainsDegree:

    def test_range_slot_inside(self):
        assert slot_contains_degree(54, 55, 54) is True
        assert slot_contains_degree(54, 55, 55) is True

    def test_range_slot_outside(self):
        assert slot_contains_degree(54, 55, 53) is False
        assert slot_contains_degree(54, 55, 56) is False

    def test_below_x_slot(self):
        # "Below 60°F": degree < 60
        assert slot_contains_degree(None, 60, 59) is True
        assert slot_contains_degree(None, 60, 60) is False  # exclusive upper

    def test_above_x_slot(self):
        # "≥90°F": degree >= 90
        assert slot_contains_degree(90, None, 90) is True
        assert slot_contains_degree(90, None, 89) is False


# ══════════════════════════════════════════════════════════════════════
# is_daily_max_final: time factor
# ══════════════════════════════════════════════════════════════════════

class TestIsDailyMaxFinal:

    def _make_obs(self, temps_and_hours, tz=None):
        """Create observation list with temps at given hours (UTC)."""
        tz = tz or timezone.utc
        base = datetime(2026, 4, 14, 0, 0, tzinfo=tz)
        return [
            ((base + timedelta(hours=h)).isoformat(), temp)
            for h, temp in temps_and_hours
        ]

    def test_before_peak_never_final(self):
        """At 13:00 local, daily max is never final regardless of stability."""
        obs = self._make_obs([(8, 50.0), (10, 55.0), (12, 58.0)])
        local_now = datetime(2026, 4, 14, 13, 0, tzinfo=timezone.utc)
        assert is_daily_max_final(local_now, obs) is False

    def test_after_peak_stable_is_final(self):
        """At 18:00 local, max was at 14:00, 4 hours ago → final."""
        obs = self._make_obs([(8, 50.0), (10, 55.0), (14, 62.0), (17, 58.0)])
        local_now = datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc)
        assert is_daily_max_final(local_now, obs, post_peak_hour=17) is True

    def test_after_peak_recent_high_not_final(self):
        """At 17:30 local, max was set 20 minutes ago → NOT final (< 60 min)."""
        obs = self._make_obs([
            (8, 50.0), (10, 55.0), (14, 62.0), (17, 58.0),
            # New high 10 minutes ago
        ])
        # Add a very recent new high
        recent = datetime(2026, 4, 14, 17, 20, tzinfo=timezone.utc)
        obs.append((recent.isoformat(), 63.0))
        local_now = datetime(2026, 4, 14, 17, 30, tzinfo=timezone.utc)
        assert is_daily_max_final(local_now, obs, post_peak_hour=17) is False

    def test_empty_observations(self):
        local_now = datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc)
        assert is_daily_max_final(local_now, []) is False

    def test_custom_stability_window(self):
        """Custom 30-minute window — final if max was 35 min ago."""
        obs = self._make_obs([(14, 62.0)])
        local_now = datetime(2026, 4, 14, 17, 35, tzinfo=timezone.utc)
        # Max at 14:00, now 17:35 → 215 min > 30 → final
        assert is_daily_max_final(
            local_now, obs, post_peak_hour=17, stability_window_minutes=30,
        ) is True


# ══════════════════════════════════════════════════════════════════════
# Locked-win: wu_round + margin + condition A (below) + condition B (above)
# ══════════════════════════════════════════════════════════════════════

class TestLockedWinWuRoundBoundary:
    """Parametric boundary tests for locked-win with wu_round and margin."""

    CFG = StrategyConfig(locked_win_margin_f=2)

    @pytest.mark.parametrize("daily_max_f, slot_range, expect_locked", [
        # Condition A: below-slot (daily_max above slot upper)
        # Slot [54, 55], margin=2: need wu_round(max) > 55 AND wu_round(max) - 55 >= 2
        # So wu_round(max) >= 57
        (56.4, (54, 55), False),   # wu_round=56, 56-55=1 < 2
        (56.5, (54, 55), True),    # wu_round=57, 57-55=2 >= 2 ✓
        (57.0, (54, 55), True),    # wu_round=57
        (58.0, (54, 55), True),    # wu_round=58

        # Condition A with slot [56, 57]
        (58.4, (56, 57), False),   # wu_round=58, 58-57=1 < 2
        (59.0, (56, 57), True),    # wu_round=59, 59-57=2 >= 2 ✓

        # Condition B: above-slot (daily_max below slot lower)
        # Slot [56, 57], margin=2: need wu_round(max) < 56 AND 56-wu_round(max) >= 2
        # So wu_round(max) <= 54
        (54.6, (56, 57), False),   # wu_round=55, 56-55=1 < 2
        (54.5, (56, 57), False),   # wu_round=55, 56-55=1 < 2
        (54.4, (56, 57), True),    # wu_round=54, 56-54=2 >= 2 ✓
        (54.0, (56, 57), True),    # wu_round=54
        (53.0, (56, 57), True),    # wu_round=53

        # Slot [54, 55], condition B
        (51.4, (54, 55), True),    # wu_round=51, 54-51=3 >= 2 ✓
        (52.4, (54, 55), True),    # wu_round=52, 54-52=2 >= 2 ✓
        (52.5, (54, 55), False),   # wu_round=53, 54-53=1 < 2
    ])
    def test_range_slot_locked_win(self, daily_max_f, slot_range, expect_locked):
        slot = _slot(slot_range[0], slot_range[1])
        event = _event([slot])
        sigs = evaluate_locked_win_signals(
            event, daily_max_f, self.CFG, daily_max_final=True,
        )
        got = len(sigs) > 0
        assert got == expect_locked, (
            f"daily_max={daily_max_f} (wu_round={wu_round(daily_max_f)}), "
            f"slot={slot_range}: expected locked={expect_locked}, got {got}"
        )

    def test_time_factor_blocks_lock(self):
        """Same temperature at 13:00 vs 18:00 — only 18:00 (final) locks."""
        slot = _slot(54, 55)
        event = _event([slot])
        # daily_max=58.0, wu_round=58, 58-55=3 >= 2 margin → condition A
        # But daily_max_final=False → no lock
        sigs_not_final = evaluate_locked_win_signals(
            event, 58.0, self.CFG, daily_max_final=False,
        )
        assert len(sigs_not_final) == 0, "Should NOT lock when daily_max not final"

        sigs_final = evaluate_locked_win_signals(
            event, 58.0, self.CFG, daily_max_final=True,
        )
        assert len(sigs_final) == 1, "Should lock when daily_max is final"


class TestLockedWinSymmetric:
    """Test that both above AND below slots lock for the same daily_max."""

    def test_locks_both_sides(self):
        """daily_max=55.0 → slot [52,53] locked (below) AND slot [58,59] locked (above)."""
        cfg = StrategyConfig(locked_win_margin_f=2)
        below = _slot(52, 53, tid_no="below")   # wu_round(55)=55, 55-53=2 >= 2 ✓
        contains = _slot(54, 55, tid_no="in")    # contains the max → NOT locked
        above = _slot(58, 59, tid_no="above")    # wu_round(55)=55, 58-55=3 >= 2 ✓
        event = _event([below, contains, above])
        sigs = evaluate_locked_win_signals(
            event, 55.0, cfg, daily_max_final=True,
        )
        locked_ids = {s.slot.token_id_no for s in sigs}
        assert "below" in locked_ids, "Below-slot should be locked"
        assert "above" in locked_ids, "Above-slot should be locked"
        assert "in" not in locked_ids, "Containing slot should NOT be locked"

    def test_adjacent_slots_not_locked_with_margin(self):
        """daily_max=55.0 → slot [54,55] and [56,57] NOT locked (within margin=2)."""
        cfg = StrategyConfig(locked_win_margin_f=2)
        # wu_round(55)=55
        adj_below = _slot(54, 55, tid_no="adj_below")  # 55 not > 55
        adj_above = _slot(56, 57, tid_no="adj_above")   # 56-55=1 < 2
        event = _event([adj_below, adj_above])
        sigs = evaluate_locked_win_signals(
            event, 55.0, cfg, daily_max_final=True,
        )
        assert len(sigs) == 0, "Adjacent slots within margin should NOT lock"


class TestLockedWinOpenEnded:
    """Open-ended slot locked-win with wu_round."""

    def test_below_x_slot_locked_below(self):
        """'Below 60°F' with daily_max=63 → wu_round=63, 63-60=3 >= 2 → locked."""
        cfg = StrategyConfig(locked_win_margin_f=2)
        slot = _slot(None, 60)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 63.0, cfg, daily_max_final=True)
        assert len(sigs) == 1

    def test_below_x_slot_not_locked_within_margin(self):
        """'Below 60°F' with daily_max=60.4 → wu_round=60, 60-60=0 < 2 → NOT locked."""
        cfg = StrategyConfig(locked_win_margin_f=2)
        slot = _slot(None, 60)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 60.4, cfg, daily_max_final=True)
        assert len(sigs) == 0

    def test_ge_x_slot_above_locked(self):
        """'≥90°F' with daily_max=86 → wu_round=86, 90-86=4 >= 2 → locked (condition B)."""
        cfg = StrategyConfig(locked_win_margin_f=2)
        slot = _slot(90, None)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 86.0, cfg, daily_max_final=True)
        assert len(sigs) == 1

    def test_ge_x_slot_daily_max_above_threshold(self):
        """'≥90°F' with daily_max=91 → wu_round=91 >= 90 → YES wins → NO loses → skip."""
        cfg = StrategyConfig(locked_win_margin_f=2)
        slot = _slot(90, None)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 91.0, cfg, daily_max_final=True)
        assert len(sigs) == 0

    def test_ge_x_slot_close_below_not_locked(self):
        """'≥90°F' with daily_max=88.6 → wu_round=89, 90-89=1 < 2 → NOT locked."""
        cfg = StrategyConfig(locked_win_margin_f=2)
        slot = _slot(90, None)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 88.6, cfg, daily_max_final=True)
        assert len(sigs) == 0


# ══════════════════════════════════════════════════════════════════════
# Locked-win with margin=0 (aggressive, no safety buffer)
# ══════════════════════════════════════════════════════════════════════

class TestLockedWinZeroMargin:
    """With margin=0, locks as soon as wu_round differs from slot boundary."""

    def test_condition_a_zero_margin(self):
        cfg = StrategyConfig(locked_win_margin_f=0)
        # Slot [54, 55], daily_max=55.5 → wu_round=56, 56>55 AND 56-55=1 >= 0 → locked
        slot = _slot(54, 55)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 55.5, cfg, daily_max_final=True)
        assert len(sigs) == 1

    def test_condition_a_exactly_at_upper_zero_margin(self):
        cfg = StrategyConfig(locked_win_margin_f=0)
        # Slot [54, 55], daily_max=55.4 → wu_round=55, 55 NOT > 55 → NOT locked
        slot = _slot(54, 55)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, 55.4, cfg, daily_max_final=True)
        assert len(sigs) == 0


# ══════════════════════════════════════════════════════════════════════
# Exit/Trim: wu_round in locked-win protection
# ══════════════════════════════════════════════════════════════════════

class TestExitWuRound:
    """Exit Layer 1 locked-win protection uses wu_round."""

    def test_exit_protected_when_wu_round_exceeds(self):
        """daily_max=84.5, slot [80,84]: wu_round(84.5)=85 > 84 → protected."""
        cfg = StrategyConfig(no_distance_threshold_f=10)
        held = _slot(80, 84, price_no=0.90)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(84.5), 84.5, [held], cfg,
            forecast=_forecast(75.0),
        )
        assert len(sigs) == 0, "Should be protected (wu_round=85 > 84)"

    def test_exit_not_protected_when_wu_round_at_boundary(self):
        """daily_max=84.4, slot [80,84]: wu_round(84.4)=84 NOT > 84 → NOT protected."""
        cfg = StrategyConfig(no_distance_threshold_f=10)
        held = _slot(80, 84, price_no=0.90)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(84.4), 84.4, [held], cfg,
            forecast=_forecast(82.0),  # forecast inside slot → negative EV
        )
        # wu_round(84.4)=84, not > 84 → Layer 1 doesn't protect → EV check
        assert len(sigs) == 1, "Should NOT be protected (wu_round=84, not > 84)"


class TestTrimWuRound:
    """Trim locked-win protection uses wu_round."""

    def test_trim_protected_when_wu_round_exceeds(self):
        """daily_max=74.5, slot [70,74]: wu_round=75 > 74 → never trim."""
        cfg = StrategyConfig()
        held = _slot(70, 74, price_no=0.90)
        event = _event([held])
        fc = _forecast(high=72.0)  # forecast inside slot → low win_prob → trim candidate
        sigs = evaluate_trim_signals(event, fc, [held], cfg, daily_max_f=74.5)
        assert len(sigs) == 0, "Should be protected by wu_round trim guard"

    def test_trim_not_protected_when_wu_round_at_boundary(self):
        """daily_max=74.4, slot [70,74]: wu_round=74 NOT > 74 → can trim."""
        cfg = StrategyConfig()
        held = _slot(70, 74, price_no=0.90)
        event = _event([held])
        fc = _forecast(high=72.0)  # forecast inside slot → negative EV
        sigs = evaluate_trim_signals(event, fc, [held], cfg, daily_max_f=74.4)
        assert len(sigs) == 1, "Should NOT be protected (wu_round=74, not > 74)"


# ══════════════════════════════════════════════════════════════════════
# Backtest settlement: wu_round precision
# ══════════════════════════════════════════════════════════════════════

class TestBacktestSettlement:
    """Backtest settlement uses wu_round to match real-world settlement."""

    def test_wu_round_settlement_boundary(self):
        """54.5°F actual should settle as 55 (half-up), landing in [54,55] slot."""
        from src.strategy.temperature import wu_round
        assert wu_round(54.5) == 55
        # Slot [54, 55]: 54 <= 55 <= 55 → YES wins → NO loses
        assert 54 <= wu_round(54.5) <= 55

    def test_actual_just_below_boundary(self):
        """54.4°F → wu_round=54, falls in [54,55] → YES wins."""
        assert wu_round(54.4) == 54
        assert 54 <= wu_round(54.4) <= 55

    def test_actual_just_above_upper(self):
        """55.5°F → wu_round=56, NOT in [54,55] → NO wins."""
        assert wu_round(55.5) == 56
        assert not (54 <= wu_round(55.5) <= 55)

    def test_actual_at_exact_upper(self):
        """55.0°F → wu_round=55, in [54,55] → YES wins."""
        assert wu_round(55.0) == 55
        assert 54 <= wu_round(55.0) <= 55


# ══════════════════════════════════════════════════════════════════════
# Parametric boundary sweep: daily_max values near slot boundaries
# ══════════════════════════════════════════════════════════════════════

class TestBoundarySweep:
    """Sweep daily_max across slot boundaries to verify all combinations."""

    @pytest.mark.parametrize("daily_max_f", [
        54.4, 54.5, 54.6, 54.9, 55.0, 55.4, 55.5, 55.6, 55.9, 56.0,
    ])
    def test_slot_54_55_locked_win(self, daily_max_f):
        """Verify locked-win for slot [54,55] across boundary values."""
        cfg = StrategyConfig(locked_win_margin_f=2)
        slot = _slot(54, 55)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, daily_max_f, cfg, daily_max_final=True)
        rounded = wu_round(daily_max_f)
        # Condition A: rounded > 55 AND rounded - 55 >= 2 → rounded >= 57
        # Condition B: rounded < 54 AND 54 - rounded >= 2 → rounded <= 52
        expected_locked = (rounded >= 57) or (rounded <= 52)
        got_locked = len(sigs) > 0
        assert got_locked == expected_locked, (
            f"daily_max={daily_max_f}, wu_round={rounded}: "
            f"expected locked={expected_locked}, got {got_locked}"
        )

    @pytest.mark.parametrize("daily_max_f", [
        54.4, 54.5, 54.6, 54.9, 55.0, 55.4, 55.5, 55.6, 55.9, 56.0,
    ])
    def test_slot_56_57_locked_win(self, daily_max_f):
        """Verify locked-win for slot [56,57] across boundary values."""
        cfg = StrategyConfig(locked_win_margin_f=2)
        slot = _slot(56, 57)
        event = _event([slot])
        sigs = evaluate_locked_win_signals(event, daily_max_f, cfg, daily_max_final=True)
        rounded = wu_round(daily_max_f)
        # Condition A: rounded > 57 AND rounded - 57 >= 2 → rounded >= 59
        # Condition B: rounded < 56 AND 56 - rounded >= 2 → rounded <= 54
        expected_locked = (rounded >= 59) or (rounded <= 54)
        got_locked = len(sigs) > 0
        assert got_locked == expected_locked, (
            f"daily_max={daily_max_f}, wu_round={rounded}: "
            f"expected locked={expected_locked}, got {got_locked}"
        )
