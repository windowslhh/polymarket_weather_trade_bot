"""Tests for the 3 strategy variants (B / C / D') after FIX-17 (2026-04-24).

A was dropped; D became D' with a narrower footprint (whitelisted cities,
higher EV gate).  B was retuned to kelly=0.5 / per-city cap 20.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.config import (
    AppConfig, CityConfig, SchedulingConfig, StrategyConfig,
    get_strategy_variants,
)
from src.markets.models import Side, TempSlot, WeatherMarketEvent
from src.strategy.evaluator import (
    evaluate_exit_signals,
    evaluate_locked_win_signals,
    evaluate_no_signals,
)
from src.strategy.sizing import compute_size
from src.weather.models import Forecast, Observation


def _make_event(city="New York") -> WeatherMarketEvent:
    return WeatherMarketEvent(
        event_id="evt_1", condition_id="cond_1", city=city,
        market_date=date.today(), slots=[],
    )


def _make_slot(lower=60.0, upper=64.0, price_no=0.65, token_id_no="no_1") -> TempSlot:
    return TempSlot(
        token_id_yes="yes_1", token_id_no=token_id_no,
        outcome_label=f"{lower:.0f}°F to {upper:.0f}°F",
        temp_lower_f=lower, temp_upper_f=upper, price_no=price_no,
    )


def _make_forecast(high=75.0, ci=4.0) -> Forecast:
    return Forecast(
        "New York", date.today(), high, 60.0, ci, "mock",
        datetime.now(timezone.utc),
    )


def _build_strat_cfg(variant_name: str) -> StrategyConfig:
    variants = get_strategy_variants()
    return replace(StrategyConfig(), **variants[variant_name])


# ──────────────────────────────────────────────────────────────────────
# Structure
# ──────────────────────────────────────────────────────────────────────

class TestVariantStructure:

    def test_exactly_three_variants(self):
        variants = get_strategy_variants()
        assert set(variants.keys()) == {"B", "C", "D"}, (
            "FIX-17: A dropped; only B / C / D' remain"
        )

    def test_a_variant_dropped(self):
        assert "A" not in get_strategy_variants()

    def test_all_overrides_are_valid_fields(self):
        variants = get_strategy_variants()
        valid_fields = set(StrategyConfig.__dataclass_fields__.keys())
        for name, overrides in variants.items():
            for key in overrides:
                assert key in valid_fields, f"Strategy {name}: invalid field '{key}'"

    def test_calibration_confidence_not_in_overrides(self):
        """FIX-17: the field was display-only; removing it from overrides
        prevents readers from thinking C's 0.75 affected trading logic."""
        for name, overrides in get_strategy_variants().items():
            assert "calibration_confidence" not in overrides, (
                f"Strategy {name} still has a calibration_confidence override"
            )


# ──────────────────────────────────────────────────────────────────────
# B — Locked Aggressor (FIX-17 retuned)
# ──────────────────────────────────────────────────────────────────────

class TestStrategyBLockedAggressor:

    def test_retuned_kelly_fraction(self):
        """FIX-17: B's kelly_fraction dropped from 0.6 to 0.5."""
        assert _build_strat_cfg("B").kelly_fraction == 0.5

    def test_retuned_city_cap(self):
        """FIX-17: B's per-city cap dropped from 30 to 20."""
        assert _build_strat_cfg("B").max_exposure_per_city_usd == 20.0

    def test_full_kelly_on_locked_wins(self):
        assert _build_strat_cfg("B").locked_win_kelly_fraction == 1.0

    def test_locked_win_cap_preserved(self):
        """B keeps the default 10.0 locked-win cap (matches base)."""
        assert _build_strat_cfg("B").max_locked_win_per_slot_usd == 10.0


# ──────────────────────────────────────────────────────────────────────
# C — Close Range
# ──────────────────────────────────────────────────────────────────────

class TestStrategyCCloseRange:

    def test_higher_ev_bar(self):
        assert _build_strat_cfg("C").min_no_ev == 0.06

    def test_moderate_price_cap(self):
        assert _build_strat_cfg("C").max_no_price == 0.75

    def test_lower_kelly_fraction(self):
        assert _build_strat_cfg("C").kelly_fraction == 0.3


# ──────────────────────────────────────────────────────────────────────
# D' — Quick Exit, whitelisted (FIX-17)
# ──────────────────────────────────────────────────────────────────────

class TestStrategyDPrime:

    def test_higher_ev_gate(self):
        assert _build_strat_cfg("D").min_no_ev == 0.08

    def test_narrow_city_cap(self):
        assert _build_strat_cfg("D").max_exposure_per_city_usd == 10.0

    def test_city_whitelist(self):
        cfg = _build_strat_cfg("D")
        assert cfg.city_whitelist == frozenset({"Los Angeles", "Seattle", "Denver"})

    def test_earlier_force_exit(self):
        assert _build_strat_cfg("D").force_exit_hours == 2.0

    def test_shorter_exit_cooldown(self):
        assert _build_strat_cfg("D").exit_cooldown_hours == 2.0

    def test_lowest_price_cap_across_variants(self):
        cfg_d = _build_strat_cfg("D")
        for name in ("B", "C"):
            assert cfg_d.max_no_price <= _build_strat_cfg(name).max_no_price


# ──────────────────────────────────────────────────────────────────────
# Global defaults changed by FIX-17
# ──────────────────────────────────────────────────────────────────────

class TestGlobalDefaults:

    def test_daily_loss_limit_75(self):
        assert StrategyConfig().daily_loss_limit_usd == 75.0

    def test_locked_win_max_price_090(self):
        assert StrategyConfig().locked_win_max_price == 0.90

    def test_city_whitelist_default_empty(self):
        assert StrategyConfig().city_whitelist == frozenset()


# ──────────────────────────────────────────────────────────────────────
# Cross-variant behaviour
# ──────────────────────────────────────────────────────────────────────

class TestCrossStrategySignals:

    def _make_market(self):
        event = _make_event()
        for i in range(20):
            lower = 55.0 + i * 2
            upper = lower + 2
            dist = abs(75.0 - (lower + upper) / 2)
            price_no = min(0.98, 0.30 + dist * 0.02)
            event.slots.append(_make_slot(lower, upper, round(price_no, 3), f"no_{i}"))
        return event

    def test_all_strategies_produce_signals(self):
        event = self._make_market()
        forecast = _make_forecast(high=75.0)
        for name in ("B", "C", "D"):
            cfg = _build_strat_cfg(name)
            sigs = evaluate_no_signals(event, forecast, cfg)
            assert len(sigs) > 0, f"Strategy {name} should produce signals"

    def test_d_filters_by_price_more_strictly_than_b(self):
        """D's 0.65 max price rejects a slot that B's 0.70 accepts."""
        event = _make_event()
        event.slots.append(_make_slot(60.0, 62.0, 0.68, "no_1"))
        forecast = _make_forecast(high=75.0)
        sigs_b = evaluate_no_signals(event, forecast, _build_strat_cfg("B"))
        sigs_d = evaluate_no_signals(event, forecast, _build_strat_cfg("D"))
        assert len(sigs_b) >= 1
        assert len(sigs_d) == 0

    def test_b_locked_win_sizes_larger_than_c(self):
        """B (full Kelly on locked) sizes larger than C (half Kelly, tighter cap)."""
        event = _make_event()
        slot = _make_slot(55.0, 59.0, 0.50, "no_locked")
        event.slots = [slot]

        locked_b = evaluate_locked_win_signals(event, 80.0, _build_strat_cfg("B"), daily_max_final=True)
        locked_c = evaluate_locked_win_signals(event, 80.0, _build_strat_cfg("C"), daily_max_final=True)
        assert len(locked_b) == 1 and len(locked_c) == 1

        size_b = compute_size(locked_b[0], 0.0, 0.0, _build_strat_cfg("B"))
        size_c = compute_size(locked_c[0], 0.0, 0.0, _build_strat_cfg("C"))
        assert size_b >= size_c


# ──────────────────────────────────────────────────────────────────────
# Boundary
# ──────────────────────────────────────────────────────────────────────

class TestVariantBoundary:

    def test_price_at_exact_boundary_b(self):
        """Slot price exactly at B's max_no_price (0.70) passes (strict >)."""
        event = _make_event()
        event.slots = [_make_slot(60.0, 62.0, 0.70, "no_exact")]
        forecast = _make_forecast(high=75.0)
        sigs = evaluate_no_signals(event, forecast, _build_strat_cfg("B"))
        assert len(sigs) >= 1

    def test_price_one_cent_above_boundary_b(self):
        event = _make_event()
        event.slots = [_make_slot(60.0, 62.0, 0.71, "no_above")]
        forecast = _make_forecast(high=75.0)
        sigs = evaluate_no_signals(event, forecast, _build_strat_cfg("B"))
        assert len(sigs) == 0

    def test_all_strategies_share_common_base(self):
        for name in ("B", "C", "D"):
            cfg = _build_strat_cfg(name)
            assert cfg.enable_locked_wins is True
            assert cfg.auto_calibrate_distance is True

    def test_empty_slots_generate_no_signals(self):
        event = _make_event()
        forecast = _make_forecast()
        for name in ("B", "C", "D"):
            sigs = evaluate_no_signals(event, forecast, _build_strat_cfg(name))
            assert len(sigs) == 0


# ──────────────────────────────────────────────────────────────────────
# Failure branches
# ──────────────────────────────────────────────────────────────────────

class TestVariantFailure:

    def test_unknown_variant_key_raises(self):
        with pytest.raises(KeyError):
            _ = get_strategy_variants()["Z"]

    def test_empty_overrides_produces_base_config(self):
        base = StrategyConfig()
        assert replace(base, **{}) == base

    def test_partial_override_preserves_defaults(self):
        """Variant overrides should not leak into unrelated fields."""
        base = StrategyConfig()
        cfg_b = _build_strat_cfg("B")
        assert cfg_b.max_no_price == 0.70  # overridden
        # Base-derived, NOT overridden by B's dict → inherits base value.
        assert cfg_b.max_slot_spread == base.max_slot_spread
        assert cfg_b.min_market_volume == base.min_market_volume

    def test_d_cooldown_shorter_than_base(self):
        base = StrategyConfig()
        assert _build_strat_cfg("D").exit_cooldown_hours < base.exit_cooldown_hours


# ──────────────────────────────────────────────────────────────────────
# Performance
# ──────────────────────────────────────────────────────────────────────

class TestVariantPerformance:

    def test_all_strategies_evaluate_fast(self):
        import time

        event = _make_event()
        for i in range(40):
            lower = 40.0 + i * 2
            upper = lower + 2
            dist = abs(75.0 - (lower + upper) / 2)
            price_no = min(0.98, 0.30 + dist * 0.02)
            event.slots.append(_make_slot(lower, upper, round(price_no, 3), f"no_{i}"))

        forecast = _make_forecast(high=75.0)
        obs = Observation(
            icao="KLGA", temp_f=74.0,
            observation_time=datetime.now(timezone.utc),
        )

        t0 = time.monotonic()
        for _ in range(100):
            for name in ("B", "C", "D"):
                cfg = _build_strat_cfg(name)
                _ = evaluate_no_signals(event, forecast, cfg)
                _ = evaluate_locked_win_signals(event, 80.0, cfg, daily_max_final=True)
                _ = evaluate_exit_signals(
                    event, obs, 74.0, event.slots[:5], cfg,
                    days_ahead=0, forecast=forecast,
                )
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0, f"900 evaluations took {elapsed:.3f}s"

    def test_get_strategy_variants_is_cheap(self):
        import time
        t0 = time.monotonic()
        for _ in range(10000):
            _ = get_strategy_variants()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"10k calls took {elapsed:.3f}s"
