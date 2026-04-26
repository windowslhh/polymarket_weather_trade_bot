"""Tests for strategy B — the sole live variant from 2026-04-26.

A / C / D' were retired when the bot moved to local live trading with $200
capital — running a single, well-tuned variant simplifies sizing math and
concentrates capital where it works.  DB schema retains the strategy column
(Y6 trigger still allows A/B/C/D) so historical rows remain queryable for
audit; this file just covers the *active* strategy surface.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from src.config import StrategyConfig, get_strategy_variants
from src.markets.models import TempSlot, WeatherMarketEvent
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


class TestVariantStructure:

    def test_only_b_variant_active(self):
        variants = get_strategy_variants()
        assert set(variants.keys()) == {"B"}, (
            "B is the only live variant from 2026-04-26"
        )

    def test_a_c_d_dropped(self):
        variants = get_strategy_variants()
        assert "A" not in variants
        assert "C" not in variants
        assert "D" not in variants

    def test_all_overrides_are_valid_fields(self):
        valid_fields = set(StrategyConfig.__dataclass_fields__.keys())
        for name, overrides in get_strategy_variants().items():
            for key in overrides:
                assert key in valid_fields, f"Strategy {name}: invalid field '{key}'"


class TestStrategyBParams:

    def test_kelly_fraction(self):
        assert _build_strat_cfg("B").kelly_fraction == 0.5

    def test_locked_win_kelly_fraction(self):
        assert _build_strat_cfg("B").locked_win_kelly_fraction == 1.0

    def test_max_no_price(self):
        assert _build_strat_cfg("B").max_no_price == 0.70

    def test_min_no_ev(self):
        assert _build_strat_cfg("B").min_no_ev == 0.05

    def test_max_exposure_per_city(self):
        assert _build_strat_cfg("B").max_exposure_per_city_usd == 20.0

    def test_max_positions_per_event(self):
        assert _build_strat_cfg("B").max_positions_per_event == 4

    def test_max_locked_win_per_slot(self):
        assert _build_strat_cfg("B").max_locked_win_per_slot_usd == 10.0

    def test_max_position_per_slot(self):
        assert _build_strat_cfg("B").max_position_per_slot_usd == 5.0


class TestGlobalDefaults:

    def test_daily_loss_limit_75(self):
        assert StrategyConfig().daily_loss_limit_usd == 75.0

    def test_locked_win_max_price_090(self):
        assert StrategyConfig().locked_win_max_price == 0.90

    def test_exit_cooldown_4h(self):
        assert StrategyConfig().exit_cooldown_hours == 4.0


class TestStrategyBBehaviour:

    def _make_market(self):
        event = _make_event()
        for i in range(20):
            lower = 55.0 + i * 2
            upper = lower + 2
            dist = abs(75.0 - (lower + upper) / 2)
            price_no = min(0.98, 0.30 + dist * 0.02)
            event.slots.append(_make_slot(lower, upper, round(price_no, 3), f"no_{i}"))
        return event

    def test_b_produces_signals(self):
        event = self._make_market()
        forecast = _make_forecast(high=75.0)
        cfg = _build_strat_cfg("B")
        sigs = evaluate_no_signals(event, forecast, cfg)
        assert len(sigs) > 0

    def test_b_locked_win_full_kelly(self):
        """B uses full Kelly on locked wins → larger size than half-Kelly base."""
        event = _make_event()
        event.slots = [_make_slot(55.0, 59.0, 0.50, "no_locked")]

        cfg_b = _build_strat_cfg("B")
        cfg_half = replace(cfg_b, locked_win_kelly_fraction=0.5)

        locked_b = evaluate_locked_win_signals(event, 80.0, cfg_b, daily_max_final=True)
        locked_half = evaluate_locked_win_signals(event, 80.0, cfg_half, daily_max_final=True)
        assert len(locked_b) == 1 and len(locked_half) == 1

        size_b = compute_size(locked_b[0], 0.0, 0.0, cfg_b)
        size_half = compute_size(locked_half[0], 0.0, 0.0, cfg_half)
        assert size_b > size_half


class TestVariantBoundary:

    def test_price_at_exact_boundary_passes(self):
        """Slot price exactly at B's max_no_price (0.70) passes (strict >)."""
        event = _make_event()
        event.slots = [_make_slot(60.0, 62.0, 0.70, "no_exact")]
        forecast = _make_forecast(high=75.0)
        sigs = evaluate_no_signals(event, forecast, _build_strat_cfg("B"))
        assert len(sigs) >= 1

    def test_price_one_cent_above_boundary_rejected(self):
        event = _make_event()
        event.slots = [_make_slot(60.0, 62.0, 0.71, "no_above")]
        forecast = _make_forecast(high=75.0)
        sigs = evaluate_no_signals(event, forecast, _build_strat_cfg("B"))
        assert len(sigs) == 0

    def test_b_inherits_common_base(self):
        cfg = _build_strat_cfg("B")
        assert cfg.enable_locked_wins is True
        assert cfg.auto_calibrate_distance is True

    def test_empty_slots_generate_no_signals(self):
        event = _make_event()
        forecast = _make_forecast()
        sigs = evaluate_no_signals(event, forecast, _build_strat_cfg("B"))
        assert len(sigs) == 0


class TestVariantFailure:

    def test_unknown_variant_key_raises(self):
        with pytest.raises(KeyError):
            _ = get_strategy_variants()["Z"]

    def test_a_c_d_lookups_raise(self):
        variants = get_strategy_variants()
        for retired in ("A", "C", "D"):
            with pytest.raises(KeyError):
                _ = variants[retired]

    def test_partial_override_preserves_defaults(self):
        base = StrategyConfig()
        cfg_b = _build_strat_cfg("B")
        assert cfg_b.max_no_price == 0.70  # overridden
        # Base-derived, NOT overridden → inherits base value.
        assert cfg_b.max_slot_spread == base.max_slot_spread
        assert cfg_b.min_market_volume == base.min_market_volume


class TestVariantPerformance:

    def test_evaluate_fast(self):
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

        cfg = _build_strat_cfg("B")
        t0 = time.monotonic()
        for _ in range(100):
            _ = evaluate_no_signals(event, forecast, cfg)
            _ = evaluate_locked_win_signals(event, 80.0, cfg, daily_max_final=True)
            _ = evaluate_exit_signals(
                event, obs, 74.0, event.slots[:5], cfg,
                days_ahead=0, forecast=forecast,
            )
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0, f"300 evaluations took {elapsed:.3f}s"

    def test_get_strategy_variants_is_cheap(self):
        import time
        t0 = time.monotonic()
        for _ in range(10000):
            _ = get_strategy_variants()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"10k calls took {elapsed:.3f}s"
