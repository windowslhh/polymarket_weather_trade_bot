"""Tests for hybrid exit mode (Module 2C).

Three-layer exit decision:
  Layer 1 — Locked-win protection: never exit guaranteed winners
  Layer 2 — EV-based: hold if EV positive, sell if negative
  Layer 3 — Pre-settlement force: sell near settlement regardless of EV

Also tests: exit cooldown config, backward compatibility.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone

import pytest

from src.config import StrategyConfig
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.strategy.evaluator import evaluate_exit_signals
from src.strategy.trend import TrendState
from src.weather.historical import ForecastErrorDistribution
from src.weather.models import Forecast, Observation


# ── Helpers ──────────────────────────────────────────────────────────

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


def _error_dist(n=100, spread=3.0):
    import random
    rng = random.Random(42)
    errors = [rng.gauss(0, spread) for _ in range(n)]
    return ForecastErrorDistribution("NYC", errors)


# ──────────────────────────────────────────────────────────────────────
# Layer 1: Locked-win protection
# ──────────────────────────────────────────────────────────────────────

class TestLayer1LockedWinProtection:
    """Layer 1: Never exit a slot where daily_max > slot.upper_bound."""

    def test_locked_win_not_exited(self):
        """daily_max=87 > upper=84 + margin(2) → slot is locked win → NO exit."""
        config = StrategyConfig(no_distance_threshold_f=8)
        held = _slot(80, 84)
        event = _event([held])
        # wu_round(87)=87, gap=87-84=3 >= margin(2) → locked-win protected
        sigs = evaluate_exit_signals(
            event, _obs(87.0), 87.0, [held], config,
            forecast=_forecast(75.0),
        )
        assert len(sigs) == 0

    def test_locked_win_dead_zone_not_protected(self):
        """daily_max=85 > upper=84 but gap(1) < margin(2) → NOT protected → exit fires."""
        config = StrategyConfig(no_distance_threshold_f=8)
        held = _slot(80, 84)
        event = _event([held])
        # wu_round(85)=85, gap=85-84=1 < margin(2) → dead zone
        # forecast=82 inside [80,84] → distance=0 < exit_distance → EV negative → SELL
        sigs = evaluate_exit_signals(
            event, _obs(85.0), 85.0, [held], config,
            forecast=_forecast(82.0),
        )
        assert len(sigs) == 1, (
            "Dead zone (gap=1 < margin=2): Layer 1 must not block, "
            "forecast inside slot should produce exit signal"
        )

    def test_locked_win_protection_below_x_slot(self):
        """'Below 60°F' with daily_max=62 → locked → no exit."""
        config = StrategyConfig(no_distance_threshold_f=8)
        held = _slot(None, 60)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(62.0), 62.0, [held], config,
            forecast=_forecast(55.0),
        )
        assert len(sigs) == 0

    def test_open_upper_slot_not_protected(self):
        """'≥80°F' (upper=None) → no locked-win protection → can exit."""
        config = StrategyConfig(no_distance_threshold_f=8)
        held = _slot(80, None, price_no=0.90)
        event = _event([held])
        # daily_max=81 → midpoint=81, distance=0 < exit_distance → exit
        sigs = evaluate_exit_signals(
            event, _obs(81.0), 81.0, [held], config,
            forecast=_forecast(75.0),
        )
        # Open-upper has no locked-win protection, and distance is close → should exit
        assert len(sigs) == 1

    def test_mixed_locked_and_threatened(self):
        """Two held: one locked (safe), one threatened → only threatened exits."""
        config = StrategyConfig(no_distance_threshold_f=8)
        # exit_distance = 8 * 0.4 = 3.2
        locked = _slot(60, 64, tid_no="locked")   # daily_max=79 > 64 → locked
        threatened = _slot(76, 80, tid_no="threat")  # daily_max=79, distance to [76,80]=0 < 3.2 → exit
        event = _event([locked, threatened])
        sigs = evaluate_exit_signals(
            event, _obs(79.0), 79.0, [locked, threatened], config,
            forecast=_forecast(75.0),
        )
        assert len(sigs) == 1
        assert sigs[0].slot.token_id_no == "threat"


# ──────────────────────────────────────────────────────────────────────
# Layer 2: EV-based exit
# ──────────────────────────────────────────────────────────────────────

class TestLayer2EVBasedExit:
    """Layer 2: Hold if EV positive, sell if negative (with forecast info)."""

    def test_positive_ev_holds(self):
        """Slot close to daily_max but still positive EV → hold."""
        config = StrategyConfig(no_distance_threshold_f=10)
        # exit_distance = 10 * 0.4 = 4.0
        # slot [85,89], daily_max=82, distance=3 < 4 → triggers exit check
        # forecast=75, confidence=4; wp_from_max=normal(3,4)≈0.77
        # At price_no=0.50: EV=0.77*0.50-0.23*0.50=0.27 → positive → hold
        held = _slot(85, 89, price_no=0.50)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(82.0), 82.0, [held], config,
            forecast=_forecast(75.0),
        )
        assert len(sigs) == 0

    def test_negative_ev_sells(self):
        """Slot very close to daily_max, forecast also close → negative EV → sell."""
        config = StrategyConfig(no_distance_threshold_f=10)
        # exit_distance = 10 * 0.4 = 4.0
        # slot [74,78], daily_max=75, distance=0 < 4 → triggers
        # forecast=76 → slot contains forecast → low win_prob → negative EV → sell
        held = _slot(74, 78, price_no=0.85)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(75.0), 75.0, [held], config,
            forecast=_forecast(76.0),
        )
        assert len(sigs) == 1
        assert sigs[0].side == Side.SELL
        assert sigs[0].expected_value < 0

    def test_ev_computed_with_real_values(self):
        """Exit signals now carry computed EV and win_prob, not hardcoded zeros."""
        config = StrategyConfig(no_distance_threshold_f=10)
        # Close slot where we expect negative EV
        held = _slot(74, 78, price_no=0.80)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(75.0), 75.0, [held], config,
            forecast=_forecast(76.0),
        )
        assert len(sigs) == 1
        # win_prob and ev should be real values, not zeros
        assert sigs[0].estimated_win_prob > 0
        # EV should be negative (that's why we're exiting)
        assert sigs[0].expected_value < 0

    def test_without_forecast_holds(self):
        """Without forecast data, cannot evaluate EV → hold (do not sell blind).

        Fixed: previously generated a SELL signal when forecast=None, treating
        "unknown EV" the same as "negative EV".  The correct behaviour is to
        hold until a forecast is available — selling without EV data risks
        exiting a winning position unnecessarily.
        """
        config = StrategyConfig(no_distance_threshold_f=10)
        held = _slot(76, 80, price_no=0.90)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(77.0), 77.0, [held], config,
            forecast=None,
        )
        assert len(sigs) == 0, "Should not SELL without forecast — hold is safer"

    def test_with_empirical_dist(self):
        """EV computation should use empirical distribution when provided."""
        config = StrategyConfig(no_distance_threshold_f=10)
        held = _slot(74, 78, price_no=0.85)
        event = _event([held])
        dist = _error_dist(n=100, spread=3.0)
        sigs = evaluate_exit_signals(
            event, _obs(75.0), 75.0, [held], config,
            forecast=_forecast(76.0), error_dist=dist,
        )
        assert len(sigs) == 1
        # Should have real computed values
        assert sigs[0].estimated_win_prob > 0

    def test_conservative_wp_used(self):
        """Exit uses min(forecast_wp, daily_max_wp) — the more conservative estimate."""
        config = StrategyConfig(no_distance_threshold_f=10)
        # Slot [90,94]: forecast=75 gives high wp, but daily_max=91 gives low wp
        # Should use the lower wp (from daily_max)
        held = _slot(90, 94, price_no=0.90)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(91.0), 91.0, [held], config,
            forecast=_forecast(75.0),
        )
        # daily_max=91 inside [90,94] → distance_to_max=0 → wp_from_max≈0.5
        # Forecast 75 → slot far → high wp from forecast
        # min(high, 0.5) = 0.5 → EV with price 0.9 = 0.5*0.1-0.5*0.9 = -0.4 → sell
        assert len(sigs) == 1
        assert sigs[0].expected_value < 0


# ──────────────────────────────────────────────────────────────────────
# Layer 3: Pre-settlement force exit
# ──────────────────────────────────────────────────────────────────────

class TestLayer3ForceExit:
    """Layer 3: Force sell within force_exit_hours of settlement."""

    def test_force_exit_within_threshold(self):
        """Close distance + near settlement → force exit even if EV positive."""
        config = StrategyConfig(no_distance_threshold_f=10, force_exit_hours=1.0)
        # exit_distance = 10*0.25=2.5, slot [85,89], daily_max=84, distance=1 < 2.5
        # price_no=0.50 → positive EV normally → but hours_to_settlement=0.5 → force exit
        held = _slot(85, 89, price_no=0.50)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(84.0), 84.0, [held], config,
            forecast=_forecast(75.0),
            hours_to_settlement=0.5,
        )
        assert len(sigs) == 1
        assert sigs[0].side == Side.SELL

    def test_no_force_exit_outside_threshold(self):
        """Close distance + far from settlement → hold (EV positive)."""
        config = StrategyConfig(no_distance_threshold_f=10, force_exit_hours=1.0)
        held = _slot(85, 89, price_no=0.50)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(82.0), 82.0, [held], config,
            forecast=_forecast(75.0),
            hours_to_settlement=5.0,  # far from settlement
        )
        assert len(sigs) == 0

    def test_force_exit_exactly_at_threshold(self):
        """hours_to_settlement == force_exit_hours → force exit (<=)."""
        config = StrategyConfig(no_distance_threshold_f=10, force_exit_hours=1.0)
        # exit_distance = 10*0.25=2.5, daily_max=84, distance=1 < 2.5
        held = _slot(85, 89, price_no=0.50)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(84.0), 84.0, [held], config,
            forecast=_forecast(75.0),
            hours_to_settlement=1.0,
        )
        assert len(sigs) == 1

    def test_force_exit_none_hours_no_force(self):
        """hours_to_settlement=None → no force exit (Layer 2 applies)."""
        config = StrategyConfig(no_distance_threshold_f=10, force_exit_hours=1.0)
        held = _slot(85, 89, price_no=0.50)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(82.0), 82.0, [held], config,
            forecast=_forecast(75.0),
            hours_to_settlement=None,
        )
        # EV positive + no settlement pressure → hold
        assert len(sigs) == 0

    def test_force_exit_locked_win_still_protected(self):
        """Layer 1 takes precedence: locked wins never exit, even near settlement."""
        config = StrategyConfig(no_distance_threshold_f=10, force_exit_hours=1.0)
        # daily_max=86 > upper=84 → locked win
        held = _slot(80, 84, price_no=0.90)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(86.0), 86.0, [held], config,
            forecast=_forecast(75.0),
            hours_to_settlement=0.1,  # 6 minutes to settlement
        )
        assert len(sigs) == 0  # Layer 1 protects

    def test_custom_force_exit_hours(self):
        """force_exit_hours=2.0 → force exit within 2 hours."""
        config = StrategyConfig(no_distance_threshold_f=10, force_exit_hours=2.0)
        # exit_distance = 10*0.25=2.5, daily_max=84, distance=1 < 2.5
        held = _slot(85, 89, price_no=0.85)
        event = _event([held])
        # 1.5h to settlement, within 2h threshold
        sigs = evaluate_exit_signals(
            event, _obs(84.0), 84.0, [held], config,
            forecast=_forecast(75.0),
            hours_to_settlement=1.5,
        )
        assert len(sigs) == 1


# ──────────────────────────────────────────────────────────────────────
# Backward Compatibility
# ──────────────────────────────────────────────────────────────────────

class TestBackwardCompatibility:
    """Old-style calls without new params still work."""

    def test_old_style_call_no_new_params(self):
        """Call without forecast/error_dist/hours_to_settlement → holds (no blind sell).

        After the forecast=None fix: missing forecast → hold, not sell.
        The old "backward compat" test assumed the pre-fix behaviour (blind sell).
        Updated to match the corrected behaviour.
        """
        config = StrategyConfig(no_distance_threshold_f=8)
        held = _slot(80, 84)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(81.0), 81.0, [held], config,
        )
        # Without forecast we cannot evaluate EV → hold (don't sell blind)
        assert len(sigs) == 0

    def test_old_guards_still_work(self):
        """None observation/daily_max → empty, days_ahead>0 → empty."""
        config = StrategyConfig(no_distance_threshold_f=8)
        held = _slot(80, 84)
        event = _event([held])

        assert evaluate_exit_signals(event, None, 81.0, [held], config) == []
        assert evaluate_exit_signals(event, _obs(), None, [held], config) == []
        assert evaluate_exit_signals(event, _obs(), 81.0, [held], config, days_ahead=1) == []

    def test_trend_adjustment_still_works(self):
        """Trend-based exit distance adjustment preserved.

        Must supply a forecast so Layer 2 EV can be evaluated (after the
        forecast=None fix, no forecast → hold unconditionally).
        The forecast high is set far from the slot (60°F) so EV is very
        negative and the SELL fires once the distance threshold is crossed.
        """
        config = StrategyConfig(no_distance_threshold_f=10)
        # New multipliers: default=0.25→2.5, STABLE=0.3→3.0
        held = _slot(80, 84)
        event = _event([held])
        # Forecast high=60°F → slot [80,84] is far above → win_prob≈1 → EV positive?
        # Actually we want EV negative so the exit fires. Use high=82°F (inside slot).
        fc = _forecast(high=82.0)
        # daily_max=77.5, distance=2.5
        # default: 2.5 < 2.5 is False → no exit; STABLE: 2.5 < 3 → EV check → exit
        sig_stable = evaluate_exit_signals(
            event, _obs(77.5), 77.5, [held], config,
            trend=TrendState.STABLE, forecast=fc,
        )
        sig_default = evaluate_exit_signals(
            event, _obs(77.5), 77.5, [held], config,
            forecast=fc,
        )
        assert len(sig_stable) == 1
        assert len(sig_default) == 0


# ──────────────────────────────────────────────────────────────────────
# Boundary Conditions
# ──────────────────────────────────────────────────────────────────────

class TestExitBoundary:

    def test_distance_exactly_at_exit_threshold(self):
        """distance > exit_distance → no exit."""
        config = StrategyConfig(no_distance_threshold_f=10)
        # exit_distance = 2.5, slot [80,84], daily_max=76, distance=4.0 > 2.5
        held = _slot(80, 84)
        event = _event([held])
        sigs = evaluate_exit_signals(event, _obs(76.0), 76.0, [held], config)
        assert len(sigs) == 0

    def test_daily_max_exactly_at_upper_not_locked(self):
        """daily_max == upper → NOT locked (needs >), so exit logic applies."""
        config = StrategyConfig(no_distance_threshold_f=10)
        # daily_max=84 == upper=84 → NOT locked
        # distance = _slot_distance([80,84], 84) = 0 < exit_distance → exit applies
        held = _slot(80, 84, price_no=0.90)
        event = _event([held])
        sigs = evaluate_exit_signals(
            event, _obs(84.0), 84.0, [held], config,
            forecast=_forecast(75.0),
        )
        # daily_max at boundary → Layer 2 evaluates EV
        # distance_to_max=0, wp_from_max≈0.5 → EV negative → sell
        assert len(sigs) == 1

    def test_empty_held_slots(self):
        config = StrategyConfig(no_distance_threshold_f=8)
        event = _event([])
        sigs = evaluate_exit_signals(event, _obs(), 80.0, [], config)
        assert sigs == []

    def test_multiple_slots_all_exit(self):
        """Multiple close slots all exit."""
        config = StrategyConfig(no_distance_threshold_f=8)
        # exit_distance = 3.2
        slots = [
            _slot(76, 80, tid_no="n1", price_no=0.90),  # dist=0 < 3.2
            _slot(77, 81, tid_no="n2", price_no=0.90),  # dist=0 < 3.2
        ]
        event = _event(slots)
        sigs = evaluate_exit_signals(
            event, _obs(78.0), 78.0, slots, config,
            forecast=_forecast(78.0),  # forecast inside both → negative EV
        )
        assert len(sigs) == 2


# ──────────────────────────────────────────────────────────────────────
# Config Fields
# ──────────────────────────────────────────────────────────────────────

class TestExitConfig:

    def test_force_exit_hours_default(self):
        cfg = StrategyConfig()
        assert cfg.force_exit_hours == 1.0

    def test_exit_cooldown_hours_default(self):
        cfg = StrategyConfig()
        assert cfg.exit_cooldown_hours == 4.0

    def test_override(self):
        cfg = StrategyConfig(force_exit_hours=2.0, exit_cooldown_hours=6.0)
        assert cfg.force_exit_hours == 2.0
        assert cfg.exit_cooldown_hours == 6.0


# ──────────────────────────────────────────────────────────────────────
# Cooldown mechanism (rebalancer-level logic, tested structurally)
# ──────────────────────────────────────────────────────────────────────

class TestExitCooldown:

    def test_rebalancer_has_recent_exits(self):
        """Rebalancer class should have _recent_exits dict."""
        from unittest.mock import MagicMock, AsyncMock
        from src.config import AppConfig
        from src.strategy.rebalancer import Rebalancer
        from src.execution.executor import Executor
        from src.portfolio.tracker import PortfolioTracker

        config = AppConfig()
        reb = Rebalancer(
            config=config,
            clob=MagicMock(),
            portfolio=MagicMock(spec=PortfolioTracker),
            executor=MagicMock(spec=Executor),
        )
        assert hasattr(reb, "_recent_exits")
        assert isinstance(reb._recent_exits, dict)
        assert len(reb._recent_exits) == 0

    def test_cooldown_config_accessible(self):
        """exit_cooldown_hours accessible from StrategyConfig."""
        cfg = StrategyConfig(exit_cooldown_hours=2.0)
        assert cfg.exit_cooldown_hours * 3600 == 7200.0


# ──────────────────────────────────────────────────────────────────────
# Regression: all exit signals are NO/SELL
# ──────────────────────────────────────────────────────────────────────

class TestExitRegression:

    def test_all_signals_no_sell(self):
        """Every exit signal must be NO/SELL."""
        config = StrategyConfig(no_distance_threshold_f=8)
        slots = [
            _slot(74 + i, 78 + i, tid_no=f"n{i}", price_no=0.90)
            for i in range(5)
        ]
        event = _event(slots)
        sigs = evaluate_exit_signals(
            event, _obs(76.0), 76.0, slots, config,
            forecast=_forecast(76.0),
        )
        for s in sigs:
            assert s.token_type == TokenType.NO
            assert s.side == Side.SELL


# ──────────────────────────────────────────────────────────────────────
# Performance
# ──────────────────────────────────────────────────────────────────────

class TestExitPerformance:

    def test_50_slots_with_forecast_fast(self):
        """50 held slots + forecast + error_dist should be fast."""
        config = StrategyConfig(no_distance_threshold_f=8)
        slots = [_slot(70 + i, 74 + i, tid_no=f"n{i}") for i in range(50)]
        event = _event(slots)
        dist = _error_dist(n=500)
        fc = _forecast(76.0)

        t0 = time.monotonic()
        for _ in range(100):
            evaluate_exit_signals(
                event, _obs(76.0), 76.0, slots, config,
                forecast=fc, error_dist=dist,
                hours_to_settlement=2.0,
            )
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"100 × 50-slot exit took {elapsed:.3f}s"
