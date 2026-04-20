"""Unit tests for individual gates in src/strategy/gates.py.

Each gate is exercised at least twice — once with a context that should
pass (``check`` returns None) and once where the gate fires.  These
tests complement the integration coverage in ``test_strategy.py`` /
``test_locked_win.py`` / ``test_trim_signals.py`` by locking each
gate's invariant in isolation, so a future breakage shows up at the
smallest possible granularity.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.config import StrategyConfig
from src.markets.models import TempSlot, WeatherMarketEvent
from src.strategy.gates import (
    AbsoluteEvGate,
    DailyMaxAboveLowerGate,
    DailyMaxBelowUpperGate,
    DailyMaxInSlotGate,
    DistanceGate,
    EvThresholdGate,
    ExitLockedWinProtectionGate,
    GATE_MATRIX,
    GateContext,
    HeldTokenGate,
    LockedWinDetectionGate,
    LockedWinEvPositiveGate,
    LockedWinPriceCapGate,
    PriceBoundsGate,
    PriceCeilingGate,
    PriceDivergenceGate,
    PriceFloorGate,
    PriceStopGate,
    RelativeEvDecayGate,
    SignalKind,
    TrimLockedWinGuardGate,
    post_peak_confidence,
)
from src.weather.models import Forecast


# ── Helpers ──────────────────────────────────────────────────────────

def _slot(lower, upper, price_no=0.80, token_no="no_1"):
    label = ""
    if lower is not None and upper is not None:
        label = f"{lower}°F to {upper}°F"
    elif lower is not None:
        label = f"{lower}°F or above"
    elif upper is not None:
        label = f"Below {upper}°F"
    return TempSlot(
        token_id_yes="yes_1", token_id_no=token_no,
        outcome_label=label,
        temp_lower_f=lower, temp_upper_f=upper,
        price_yes=1.0 - price_no, price_no=price_no,
    )


def _event(slots=None, city="NYC"):
    return WeatherMarketEvent(
        event_id="e1", condition_id="c1", city=city,
        market_date=date(2026, 4, 15), slots=slots or [],
        end_timestamp=datetime(2026, 4, 16, 4, 0, tzinfo=timezone.utc),
        title=f"Highest temperature in {city}",
    )


def _forecast(high=75.0):
    return Forecast(
        city="NYC", forecast_date=date(2026, 4, 15),
        predicted_high_f=high, predicted_low_f=high - 15,
        confidence_interval_f=3.0, source="test",
        fetched_at=datetime.now(timezone.utc),
    )


def _ctx(slot, *, config=None, **kw) -> GateContext:
    return GateContext(
        slot=slot, event=_event([slot]),
        config=config or StrategyConfig(), **kw,
    )


# ──────────────────────────────────────────────────────────────────────
# HeldTokenGate
# ──────────────────────────────────────────────────────────────────────

class TestHeldTokenGate:
    def test_pass_when_token_unheld(self):
        ctx = _ctx(_slot(80, 84, token_no="unheld"))
        assert HeldTokenGate().check(ctx) is None

    def test_fire_silently_when_token_held(self):
        slot = _slot(80, 84, token_no="held_abc")
        ctx = _ctx(slot, held_token_ids=frozenset({"held_abc"}))
        result = HeldTokenGate().check(ctx)
        assert result is not None
        assert result.code == "HELD"
        assert result.silent is True


# ──────────────────────────────────────────────────────────────────────
# PriceBoundsGate / PriceFloorGate / PriceCeilingGate
# ──────────────────────────────────────────────────────────────────────

class TestPriceBoundsGate:
    def test_pass_when_in_range(self):
        assert PriceBoundsGate().check(_ctx(_slot(80, 84, price_no=0.50))) is None

    def test_fire_when_zero(self):
        result = PriceBoundsGate().check(_ctx(_slot(80, 84, price_no=0.0)))
        assert result is not None and result.code == "PRICE_INVALID"

    def test_fire_when_one(self):
        result = PriceBoundsGate().check(_ctx(_slot(80, 84, price_no=1.0)))
        assert result is not None and result.code == "PRICE_INVALID"


class TestPriceFloorGate:
    def test_pass_above_floor(self):
        cfg = StrategyConfig(min_no_price=0.20)
        assert PriceFloorGate().check(_ctx(_slot(80, 84, price_no=0.30), config=cfg)) is None

    def test_fire_below_floor(self):
        cfg = StrategyConfig(min_no_price=0.20)
        result = PriceFloorGate().check(_ctx(_slot(80, 84, price_no=0.10), config=cfg))
        assert result is not None and result.code == "PRICE_TOO_LOW"


class TestPriceCeilingGate:
    def test_pass_at_ceiling(self):
        """`>` semantics — price == max passes."""
        cfg = StrategyConfig(max_no_price=0.80)
        assert PriceCeilingGate().check(_ctx(_slot(80, 84, price_no=0.80), config=cfg)) is None

    def test_fire_above_ceiling(self):
        cfg = StrategyConfig(max_no_price=0.80)
        result = PriceCeilingGate().check(_ctx(_slot(80, 84, price_no=0.81), config=cfg))
        assert result is not None and result.code == "PRICE_TOO_HIGH"


# ──────────────────────────────────────────────────────────────────────
# Daily-max guards
# ──────────────────────────────────────────────────────────────────────

class TestDailyMaxAboveLowerGate:
    def test_pass_when_range_slot(self):
        """≥X guard only applies to open-upper slots."""
        ctx = _ctx(_slot(80, 84), daily_max_f=90.0)
        assert DailyMaxAboveLowerGate().check(ctx) is None

    def test_fire_when_open_upper_and_max_above(self):
        ctx = _ctx(_slot(80, None), daily_max_f=82.0)
        result = DailyMaxAboveLowerGate().check(ctx)
        assert result is not None and result.code == "DAILY_MAX_ABOVE_LOWER"

    def test_pass_when_future_market(self):
        ctx = _ctx(_slot(80, None), daily_max_f=90.0, days_ahead=1)
        assert DailyMaxAboveLowerGate().check(ctx) is None


class TestDailyMaxInSlotGate:
    def test_pass_when_max_outside(self):
        ctx = _ctx(_slot(80, 84), daily_max_f=90.0)
        assert DailyMaxInSlotGate().check(ctx) is None

    def test_fire_when_max_inside_range(self):
        ctx = _ctx(_slot(80, 84), daily_max_f=82.0)
        result = DailyMaxInSlotGate().check(ctx)
        assert result is not None and result.code == "DAILY_MAX_IN_SLOT"

    def test_wu_round_half_up_boundary(self):
        """daily_max=79.5 → wu_round=80 → in [80, 84]."""
        ctx = _ctx(_slot(80, 84), daily_max_f=79.5)
        assert DailyMaxInSlotGate().check(ctx) is not None


class TestDailyMaxBelowUpperGate:
    def test_pass_pre_peak(self):
        """Pre-peak: peak_conf is None → gate is inactive."""
        ctx = _ctx(_slot(None, 75), daily_max_f=70.0, peak_conf=None)
        assert DailyMaxBelowUpperGate().check(ctx) is None

    def test_fire_post_peak_below_upper(self):
        ctx = _ctx(_slot(None, 75), daily_max_f=70.0, peak_conf=1.5)
        result = DailyMaxBelowUpperGate().check(ctx)
        assert result is not None and result.code == "DAILY_MAX_BELOW_UPPER"

    def test_pass_post_peak_above_upper(self):
        ctx = _ctx(_slot(None, 75), daily_max_f=76.0, peak_conf=1.5)
        assert DailyMaxBelowUpperGate().check(ctx) is None


# ──────────────────────────────────────────────────────────────────────
# DistanceGate
# ──────────────────────────────────────────────────────────────────────

class TestDistanceGate:
    def test_pass_when_far_enough(self):
        cfg = StrategyConfig(no_distance_threshold_f=8)
        # forecast=75, slot [85,89] → distance = 10, passes.
        ctx = _ctx(_slot(85, 89), config=cfg, forecast=_forecast(75.0))
        assert DistanceGate().check(ctx) is None
        assert ctx.distance == 10.0

    def test_fire_when_too_close(self):
        cfg = StrategyConfig(no_distance_threshold_f=8)
        ctx = _ctx(_slot(73, 77), config=cfg, forecast=_forecast(75.0))
        result = DistanceGate().check(ctx)
        assert result is not None and result.code == "DIST_TOO_CLOSE"

    def test_post_peak_obs_min_merge(self):
        """Post-peak: obs_distance can tighten the check."""
        cfg = StrategyConfig(no_distance_threshold_f=3)
        # forecast distance 10 would pass; obs_distance 0.5 will block.
        ctx = _ctx(
            _slot(56, 57), config=cfg, forecast=_forecast(67.0),
            daily_max_f=56.5, peak_conf=1.5,
        )
        result = DistanceGate().check(ctx)
        assert result is not None and result.code == "DIST_TOO_CLOSE"


# ──────────────────────────────────────────────────────────────────────
# EvThresholdGate
# ──────────────────────────────────────────────────────────────────────

class TestEvThresholdGate:
    def test_pass_when_ev_above_threshold(self):
        cfg = StrategyConfig(min_no_ev=0.01, no_distance_threshold_f=8)
        ctx = _ctx(
            _slot(85, 89, price_no=0.80), config=cfg,
            forecast=_forecast(75.0), ev_threshold=0.01,
        )
        assert EvThresholdGate().check(ctx) is None
        assert ctx.ev is not None and ctx.ev > 0.01

    def test_fire_when_ev_below_threshold(self):
        cfg = StrategyConfig(min_no_ev=0.60)
        ctx = _ctx(
            _slot(80, 84, price_no=0.80), config=cfg,
            forecast=_forecast(75.0), ev_threshold=0.60,
        )
        result = EvThresholdGate().check(ctx)
        assert result is not None and result.code == "EV_BELOW_GATE"


# ──────────────────────────────────────────────────────────────────────
# PriceDivergenceGate
# ──────────────────────────────────────────────────────────────────────

class TestPriceDivergenceGate:
    def test_pass_when_under_threshold(self):
        ctx = _ctx(_slot(80, 84, price_no=0.60))
        ctx.win_prob = 0.90
        assert PriceDivergenceGate().check(ctx) is None

    def test_fire_when_above_threshold(self):
        ctx = _ctx(_slot(80, 84, price_no=0.30))
        ctx.win_prob = 0.99
        result = PriceDivergenceGate().check(ctx)
        assert result is not None and result.code == "PRICE_DIVERGENCE"
        assert result.extra["gap"] > 0.50


# ──────────────────────────────────────────────────────────────────────
# Locked-win gates
# ──────────────────────────────────────────────────────────────────────

class TestLockedWinDetectionGate:
    def test_below_lock_fires_on_range_slot(self):
        """daily_max above slot upper → Condition A (below-slot lock)."""
        cfg = StrategyConfig(locked_win_margin_f=0)
        ctx = _ctx(
            _slot(70, 74, price_no=0.80),
            config=cfg, daily_max_f=76.0, daily_max_final=True,
        )
        assert LockedWinDetectionGate().check(ctx) is None
        assert ctx.is_locked is True
        assert ctx.is_below_lock is True

    def test_above_lock_needs_final(self):
        """daily_max below slot lower + final → Condition B (above-slot)."""
        cfg = StrategyConfig(locked_win_margin_f=0)
        ctx_final = _ctx(
            _slot(80, 84, price_no=0.80),
            config=cfg, daily_max_f=76.0, daily_max_final=True,
        )
        assert LockedWinDetectionGate().check(ctx_final) is None
        assert ctx_final.is_locked is True
        assert ctx_final.is_below_lock is False

        ctx_not_final = _ctx(
            _slot(80, 84, price_no=0.80),
            config=cfg, daily_max_f=76.0, daily_max_final=False,
        )
        result = LockedWinDetectionGate().check(ctx_not_final)
        assert result is not None and result.silent

    def test_open_upper_with_max_above_silently_blocks(self):
        """≥X slot where max >= X → YES guaranteed → silent skip."""
        cfg = StrategyConfig(locked_win_margin_f=0)
        ctx = _ctx(
            _slot(90, None, price_no=0.80),
            config=cfg, daily_max_f=95.0, daily_max_final=True,
        )
        result = LockedWinDetectionGate().check(ctx)
        assert result is not None and result.silent
        assert ctx.is_locked is False


class TestLockedWinPriceCapGate:
    def test_pass_at_or_below_cap(self):
        cfg = StrategyConfig(locked_win_max_price=0.95)
        ctx = _ctx(_slot(70, 74, price_no=0.95), config=cfg)
        assert LockedWinPriceCapGate().check(ctx) is None

    def test_fire_above_cap(self):
        cfg = StrategyConfig(locked_win_max_price=0.95)
        ctx = _ctx(_slot(70, 74, price_no=0.96), config=cfg)
        result = LockedWinPriceCapGate().check(ctx)
        assert result is not None and result.silent


class TestLockedWinEvPositiveGate:
    def test_pass_below_lock_reasonable_price(self):
        cfg = StrategyConfig()
        ctx = _ctx(_slot(70, 74, price_no=0.80), config=cfg)
        ctx.is_below_lock = True
        assert LockedWinEvPositiveGate().check(ctx) is None
        assert ctx.win_prob == 0.999
        assert ctx.ev is not None and ctx.ev > 0

    def test_fire_when_fee_wipes_ev(self):
        """At price ~0.9999, fee exceeds the win margin → ev ≤ 0."""
        cfg = StrategyConfig()
        ctx = _ctx(_slot(70, 74, price_no=0.9999), config=cfg)
        ctx.is_below_lock = True
        result = LockedWinEvPositiveGate().check(ctx)
        assert result is not None and result.silent


# ──────────────────────────────────────────────────────────────────────
# TRIM gates
# ──────────────────────────────────────────────────────────────────────

class TestTrimLockedWinGuardGate:
    def test_pass_normal_slot(self):
        ctx = _ctx(_slot(70, 74, token_no="tn"), daily_max_f=71.0)
        assert TrimLockedWinGuardGate().check(ctx) is None

    def test_fire_when_in_locked_win_set(self):
        slot = _slot(70, 74, token_no="held_locked")
        ctx = _ctx(slot, locked_win_token_ids=frozenset({"held_locked"}))
        result = TrimLockedWinGuardGate().check(ctx)
        assert result is not None and result.code == "TRIM_SKIP_LOCKED"

    def test_fire_when_daily_max_above_upper_plus_margin(self):
        cfg = StrategyConfig(locked_win_margin_f=2)
        ctx = _ctx(_slot(70, 74, token_no="tn"), config=cfg, daily_max_f=77.0)
        result = TrimLockedWinGuardGate().check(ctx)
        assert result is not None and result.code == "TRIM_SKIP_LOCKED_LIKE"


class TestAbsoluteEvGate:
    def test_pass_when_ev_not_below_threshold(self):
        cfg = StrategyConfig(min_trim_ev_absolute=0.03)
        ctx = _ctx(_slot(70, 74), config=cfg)
        ctx.ev = -0.01  # above -0.03 → pass
        assert AbsoluteEvGate().check(ctx) is None

    def test_fire_on_hard_negative(self):
        cfg = StrategyConfig(min_trim_ev_absolute=0.03)
        ctx = _ctx(_slot(70, 74), config=cfg)
        ctx.ev = -0.10
        result = AbsoluteEvGate().check(ctx)
        assert result is not None and result.code == "absolute"


class TestRelativeEvDecayGate:
    def test_pass_when_entry_ev_unknown(self):
        cfg = StrategyConfig(trim_ev_decay_ratio=0.75)
        ctx = _ctx(_slot(70, 74, token_no="tn"), config=cfg)
        ctx.ev = -0.10  # would fire absolute, but relative must be inactive
        assert RelativeEvDecayGate().check(ctx) is None

    def test_pass_when_entry_ev_nonpositive(self):
        """Relative gate only fires when entry_ev > 0 (rich entry)."""
        cfg = StrategyConfig(trim_ev_decay_ratio=0.75)
        ctx = _ctx(
            _slot(70, 74, token_no="tn"), config=cfg,
            entry_ev_map={"tn": 0.0},
        )
        ctx.ev = -0.10
        assert RelativeEvDecayGate().check(ctx) is None

    def test_fire_when_ev_decayed_past_gate(self):
        cfg = StrategyConfig(trim_ev_decay_ratio=0.75)
        ctx = _ctx(
            _slot(70, 74, token_no="tn"), config=cfg,
            entry_ev_map={"tn": 0.08},
        )
        ctx.ev = -0.01  # gate = 0.08*0.25 = 0.02; -0.01 < 0.02 → fires
        result = RelativeEvDecayGate().check(ctx)
        assert result is not None and result.code == "relative"


class TestPriceStopGate:
    def test_pass_when_price_holds(self):
        cfg = StrategyConfig(trim_price_stop_ratio=0.25)
        ctx = _ctx(
            _slot(70, 74, price_no=0.35, token_no="tn"), config=cfg,
            entry_prices={"tn": 0.40},
        )
        assert PriceStopGate().check(ctx) is None

    def test_fire_on_large_drop(self):
        cfg = StrategyConfig(trim_price_stop_ratio=0.25)
        ctx = _ctx(
            _slot(70, 74, price_no=0.28, token_no="tn"), config=cfg,
            entry_prices={"tn": 0.40},
        )
        result = PriceStopGate().check(ctx)
        assert result is not None and result.code == "price_stop"

    def test_disabled_when_ratio_out_of_range(self):
        cfg = StrategyConfig(trim_price_stop_ratio=1.5)
        ctx = _ctx(
            _slot(70, 74, price_no=0.01, token_no="tn"), config=cfg,
            entry_prices={"tn": 0.40},
        )
        assert PriceStopGate().check(ctx) is None


# ──────────────────────────────────────────────────────────────────────
# ExitLockedWinProtectionGate
# ──────────────────────────────────────────────────────────────────────

class TestExitLockedWinProtectionGate:
    def test_pass_when_max_below_upper_plus_margin(self):
        cfg = StrategyConfig(locked_win_margin_f=2)
        ctx = _ctx(_slot(70, 74), config=cfg, daily_max_f=75.0)
        assert ExitLockedWinProtectionGate().check(ctx) is None

    def test_fire_when_max_exceeds_margin(self):
        cfg = StrategyConfig(locked_win_margin_f=2)
        ctx = _ctx(_slot(70, 74), config=cfg, daily_max_f=77.0)
        result = ExitLockedWinProtectionGate().check(ctx)
        assert result is not None and result.code == "EXIT_SKIP_LOCKED"


# ──────────────────────────────────────────────────────────────────────
# GATE_MATRIX structural invariants
# ──────────────────────────────────────────────────────────────────────

class TestGateMatrixInvariants:
    """Lock the cross-branch invariants the M2 refactor exists to enforce."""

    def test_all_signal_kinds_registered(self):
        expected = {
            SignalKind.FORECAST_NO,
            SignalKind.LOCKED_WIN,
            SignalKind.TRIM,
            SignalKind.EXIT_PREFILTER,
        }
        assert set(GATE_MATRIX.keys()) == expected

    def test_price_divergence_appears_on_both_entry_branches(self):
        """Bug #1's structural guard — the duplicated gate lives in one
        list, so either both entry branches import it or neither does."""
        for kind in (SignalKind.FORECAST_NO, SignalKind.LOCKED_WIN):
            gate_types = {type(g).__name__ for g in GATE_MATRIX[kind]}
            assert "PriceDivergenceGate" in gate_types, (
                f"PRICE_DIVERGENCE missing from {kind.value} — same class "
                f"of bug as Houston 2026-04-17.  Add it to GATE_MATRIX."
            )

    def test_held_token_gate_is_first_on_entry_branches(self):
        """Ordering invariant — HELD must be checked before any gate
        that would write a decision_log entry, otherwise already-held
        slots flood the observability channel."""
        for kind in (SignalKind.FORECAST_NO, SignalKind.LOCKED_WIN):
            first_gate = GATE_MATRIX[kind][0]
            assert type(first_gate).__name__ == "HeldTokenGate", (
                f"{kind.value} first gate must be HeldTokenGate; got "
                f"{type(first_gate).__name__}"
            )


# ──────────────────────────────────────────────────────────────────────
# post_peak_confidence helper
# ──────────────────────────────────────────────────────────────────────

class TestPostPeakConfidenceHelper:
    def test_before_peak(self):
        assert post_peak_confidence(10) is None

    def test_in_peak_window(self):
        assert post_peak_confidence(15) == 3.0

    def test_post_peak(self):
        assert post_peak_confidence(18) == 1.5
