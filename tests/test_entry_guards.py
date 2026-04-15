"""Tests for three entry guard fixes:

Bug #1: Post-peak distance filter should use observed daily_max (not just forecast).
Bug #2: Block new BUY NO entries when market is close to settlement.
Bug #3: Block entries when model vs market price divergence exceeds 50pp.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.config import StrategyConfig
from src.markets.models import TempSlot, WeatherMarketEvent
from src.strategy.evaluator import evaluate_no_signals
from src.weather.models import Forecast


# ── Helpers ──────────────────────────────────────────────────────────

def _slot(lower, upper, price_no=0.80, tid_no="no_1"):
    """Default price_no=0.80 keeps model/market gap < 50pp for most tests."""
    label = ""
    if lower is not None and upper is not None:
        label = f"{lower}°F to {upper}°F"
    elif lower is not None:
        label = f"{lower}°F or above"
    return TempSlot(
        token_id_yes="yes_1", token_id_no=tid_no,
        outcome_label=label, temp_lower_f=lower, temp_upper_f=upper,
        price_yes=1.0 - price_no, price_no=price_no,
    )


def _event(slots, city="Denver"):
    return WeatherMarketEvent(
        event_id="e1", condition_id="c1", city=city,
        market_date=date.today(), slots=slots,
        end_timestamp=datetime(2026, 4, 15, 6, 0, tzinfo=timezone.utc),
        title=f"Highest temperature in {city}",
    )


def _forecast(high=67.0):
    return Forecast(
        city="Denver", forecast_date=date.today(),
        predicted_high_f=high, predicted_low_f=high - 15,
        confidence_interval_f=3.0, source="test",
        fetched_at=datetime.now(timezone.utc),
    )


# Wide threshold so distance filter doesn't block by default
_CFG = StrategyConfig(no_distance_threshold_f=3, min_no_ev=-1.0, max_no_price=0.95)


# ──────────────────────────────────────────────────────────────────────
# Bug #1: Post-peak observed distance
# ──────────────────────────────────────────────────────────────────────

class TestPostPeakObsDistance:
    """After peak hours, distance filter should also consider daily_max."""

    def test_post_peak_obs_distance_blocks_when_inside_slot(self):
        """forecast=67, daily_max=56.5, slot=[56,57], hour=18.
        Forecast distance = min(|67-56|,|67-57|) = 10.
        daily_max=56.5 ≤ slot.upper=57 → obs_distance used.
        Obs distance = min(|56.5-56|,|56.5-57|) = 0.5.
        min(10, 0.5) = 0.5 < 3 → blocked."""
        slot = _slot(56, 57, price_no=0.80)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(67.0), _CFG,
            days_ahead=0, daily_max_f=56.5, local_hour=18,
        )
        assert len(sigs) == 0

    def test_post_peak_forecast_closer_still_works(self):
        """When obs distance > forecast distance, forecast value used via min().
        forecast=67, daily_max=80, slot=[56,57].
        Forecast distance = 10. Obs distance = 23.
        min(10, 23) = 10 >= 3 → passes."""
        slot = _slot(56, 57, price_no=0.80)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(67.0), _CFG,
            days_ahead=0, daily_max_f=80.0, local_hour=18,
        )
        assert len(sigs) == 1

    def test_pre_peak_ignores_daily_max_for_distance(self):
        """Before peak (hour=12), obs_distance is NOT used.
        forecast=67, daily_max=58, slot=[56,57].
        Only forecast distance=10 >= 3 → signal generated."""
        slot = _slot(56, 57, price_no=0.80)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(67.0), _CFG,
            days_ahead=0, daily_max_f=58.0, local_hour=12,
        )
        assert len(sigs) == 1

    def test_no_daily_max_uses_forecast_only(self):
        """daily_max=None → obs_distance not computed.
        forecast=67, slot=[56,57]. distance=10 >= 3 → signal."""
        slot = _slot(56, 57, price_no=0.80)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(67.0), _CFG,
            days_ahead=0, daily_max_f=None, local_hour=18,
        )
        assert len(sigs) == 1

    def test_future_market_ignores_daily_max_for_distance(self):
        """days_ahead=1 → peak_conf=None → obs_distance not used.
        forecast=67, daily_max=58, slot=[56,57]. distance=10 >= 3 → signal."""
        slot = _slot(56, 57, price_no=0.80)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(67.0), _CFG,
            days_ahead=1, daily_max_f=58.0, local_hour=18,
        )
        assert len(sigs) == 1

    def test_daily_max_above_upper_skips_obs_distance(self):
        """forecast=67, daily_max=58, slot=[56,57], hour=18.
        daily_max=58 > slot.upper=57 → temp already passed through slot,
        NO is safe → obs_distance NOT used.
        Forecast distance=10 >= 3 → signal generated (not blocked)."""
        slot = _slot(56, 57, price_no=0.80)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(67.0), _CFG,
            days_ahead=0, daily_max_f=58.0, local_hour=18,
        )
        assert len(sigs) == 1

    def test_open_ended_slot_always_uses_obs_distance(self):
        """Open-ended slot (upper=None, e.g. '≥56°F') always uses obs_distance.
        forecast=67, daily_max=56.5, slot=[56,None], hour=18.
        Obs distance = |56.5-56| = 0.5, distance = min(11, 0.5) = 0.5 < 3 → blocked."""
        slot = _slot(56, None, price_no=0.80)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(67.0), _CFG,
            days_ahead=0, daily_max_f=56.5, local_hour=18,
        )
        assert len(sigs) == 0


# ──────────────────────────────────────────────────────────────────────
# Bug #2: Settlement time gate
# ──────────────────────────────────────────────────────────────────────

class TestSettlementGate:
    """Block new NO entries when hours_to_settlement < force_exit_hours."""

    def test_settlement_gate_blocks_near_settlement(self):
        """hours_to_settlement=0.5, force_exit_hours=1.0 → blocked."""
        slot = _slot(56, 57)
        event = _event([slot])
        cfg = StrategyConfig(no_distance_threshold_f=3, min_no_ev=-1.0,
                             max_no_price=0.95, force_exit_hours=1.0)
        sigs = evaluate_no_signals(
            event, _forecast(67.0), cfg,
            days_ahead=0, hours_to_settlement=0.5,
        )
        assert len(sigs) == 0

    def test_settlement_gate_allows_far_market(self):
        """hours_to_settlement=3.0, force_exit_hours=1.0 → allowed."""
        slot = _slot(56, 57)
        event = _event([slot])
        cfg = StrategyConfig(no_distance_threshold_f=3, min_no_ev=-1.0,
                             max_no_price=0.95, force_exit_hours=1.0)
        sigs = evaluate_no_signals(
            event, _forecast(67.0), cfg,
            days_ahead=0, hours_to_settlement=3.0,
        )
        assert len(sigs) == 1

    def test_settlement_gate_blocks_expired(self):
        """hours_to_settlement=-2.0 (expired) → blocked."""
        slot = _slot(56, 57)
        event = _event([slot])
        cfg = StrategyConfig(no_distance_threshold_f=3, min_no_ev=-1.0,
                             max_no_price=0.95, force_exit_hours=1.0)
        sigs = evaluate_no_signals(
            event, _forecast(67.0), cfg,
            days_ahead=0, hours_to_settlement=-2.0,
        )
        assert len(sigs) == 0

    def test_settlement_gate_none_allows(self):
        """hours_to_settlement=None (no end_timestamp) → allowed."""
        slot = _slot(56, 57)
        event = _event([slot])
        cfg = StrategyConfig(no_distance_threshold_f=3, min_no_ev=-1.0,
                             max_no_price=0.95, force_exit_hours=1.0)
        sigs = evaluate_no_signals(
            event, _forecast(67.0), cfg,
            days_ahead=0, hours_to_settlement=None,
        )
        assert len(sigs) == 1

    def test_strategy_d_wider_gate(self):
        """Strategy D: force_exit_hours=2.0, hours_to_settlement=1.5 → blocked."""
        slot = _slot(56, 57)
        event = _event([slot])
        cfg = StrategyConfig(no_distance_threshold_f=3, min_no_ev=-1.0,
                             max_no_price=0.95, force_exit_hours=2.0)
        sigs = evaluate_no_signals(
            event, _forecast(67.0), cfg,
            days_ahead=0, hours_to_settlement=1.5,
        )
        assert len(sigs) == 0


# ──────────────────────────────────────────────────────────────────────
# Bug #3: Price divergence guard
# ──────────────────────────────────────────────────────────────────────

class TestPriceDivergenceGuard:
    """Block entries when model win_prob vs market NO price diverge by >50pp."""

    def test_extreme_divergence_blocked(self):
        """model≈99% but price_no=0.004 (market says 0.4% NO win) → 98.6pp gap → blocked."""
        slot = _slot(56, 57, price_no=0.004)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(67.0), _CFG,
            days_ahead=0,
        )
        assert len(sigs) == 0

    def test_normal_edge_allowed(self):
        """model≈95%, price_no=0.70 → 25pp gap → allowed."""
        slot = _slot(56, 57, price_no=0.70)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(67.0), _CFG,
            days_ahead=0,
        )
        assert len(sigs) == 1

    def test_moderate_gap_allowed(self):
        """model≈95%, price_no=0.60 → 35pp gap → under 50pp → allowed."""
        slot = _slot(56, 57, price_no=0.60)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(67.0), _CFG,
            days_ahead=0,
        )
        assert len(sigs) == 1

    def test_exactly_50pp_allowed(self):
        """model≈99% (capped), price_no=0.49 → |0.99-0.49|=0.50 → exactly 50pp.
        Guard uses > 0.50, so exactly 50pp is NOT blocked."""
        slot = _slot(56, 57, price_no=0.49)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(67.0), _CFG,
            days_ahead=0,
        )
        assert len(sigs) == 1

    def test_just_over_50pp_blocked(self):
        """model≈99% (capped), price_no=0.48 → |0.99-0.48|=0.51 → 51pp > 50pp → blocked."""
        slot = _slot(56, 57, price_no=0.48)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(67.0), _CFG,
            days_ahead=0,
        )
        assert len(sigs) == 0

    def test_post_peak_boost_triggers_divergence(self):
        """Post-peak boost inflates win_prob past divergence threshold.
        slot=[40,45], forecast=48 → distance=3, z=1.0, win_prob≈0.84
        price_no=0.35 → |0.84-0.35|=0.49 < 0.50 → would pass.
        But daily_max=80, hour=18 → obs_prob=0.99 → boosted win_prob=0.99
        → |0.99-0.35|=0.64 > 0.50 → blocked by divergence guard."""
        slot = _slot(40, 45, price_no=0.35)
        event = _event([slot])
        sigs = evaluate_no_signals(
            event, _forecast(48.0), _CFG,
            days_ahead=0, daily_max_f=80.0, local_hour=18,
        )
        assert len(sigs) == 0
