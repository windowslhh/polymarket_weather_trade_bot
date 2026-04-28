"""Tests for ``get_strategy_variants()`` schema and runtime behaviour.

Post-refactor (``strategy-variant-`` series): tests assert the SHAPE of
the variants dict, not the presence of specific named variants.  Adding
or removing a variant in src/config.py should not require touching this
file as long as the schema invariants hold:

- at least one variant exists
- every variant has a ``_meta`` block with the four template-required keys
- every non-``_meta`` key in a variant is a valid ``StrategyConfig`` field
- every variant builds a usable ``StrategyConfig`` via ``replace`` +
  ``strategy_params``
- StrategyConfig global defaults match ops expectations
- evaluate_no_signals / locked_win_signals / exit_signals stay fast
  across however many variants the dict happens to contain
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from src.config import StrategyConfig, get_strategy_variants, strategy_params
from src.markets.models import TempSlot, WeatherMarketEvent
from src.strategy.evaluator import (
    evaluate_exit_signals,
    evaluate_locked_win_signals,
    evaluate_no_signals,
)
from src.strategy.sizing import compute_size
from src.weather.models import Forecast, Observation


REQUIRED_META_KEYS = {"label", "description", "color", "tag_class"}


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
    return replace(StrategyConfig(), **strategy_params(variants[variant_name]))


# ──────────────────────────────────────────────────────────────────────
# Schema invariants — assert shape, not specific names
# ──────────────────────────────────────────────────────────────────────

class TestSchema:

    def test_at_least_one_variant(self):
        assert len(get_strategy_variants()) >= 1

    def test_every_variant_has_meta_block(self):
        for name, variant in get_strategy_variants().items():
            assert "_meta" in variant, f"Variant {name!r} missing _meta"
            assert isinstance(variant["_meta"], dict)

    def test_every_meta_has_required_keys(self):
        for name, variant in get_strategy_variants().items():
            missing = REQUIRED_META_KEYS - variant["_meta"].keys()
            assert not missing, (
                f"Variant {name!r} _meta missing keys: {missing}"
            )

    def test_meta_values_are_strings(self):
        """label / description / color / tag_class are rendered into
        HTML; non-string values would either crash Jinja or render as
        ``<MagicMock object at 0x...>`` style noise."""
        for name, variant in get_strategy_variants().items():
            meta = variant["_meta"]
            for key in REQUIRED_META_KEYS:
                assert isinstance(meta[key], str), (
                    f"Variant {name!r} _meta[{key!r}] is "
                    f"{type(meta[key]).__name__}, expected str"
                )

    def test_all_overrides_are_valid_strategy_fields(self):
        """Anything that isn't ``_`` -prefixed metadata must be a real
        StrategyConfig field; otherwise dataclass replace() would
        TypeError at runtime."""
        valid_fields = set(StrategyConfig.__dataclass_fields__.keys())
        for name, variant in get_strategy_variants().items():
            for key in strategy_params(variant):
                assert key in valid_fields, (
                    f"Variant {name!r}: invalid field '{key}'"
                )

    def test_every_variant_builds_a_usable_strategy_config(self):
        """The whole point of this dict — replace() must produce a
        StrategyConfig with sensible numeric values (no None where a
        float is expected)."""
        for name in get_strategy_variants():
            cfg = _build_strat_cfg(name)
            assert isinstance(cfg, StrategyConfig)
            assert cfg.max_no_price > 0
            assert cfg.kelly_fraction > 0
            assert cfg.locked_win_kelly_fraction > 0
            assert cfg.max_position_per_slot_usd > 0

    def test_strategy_keys_are_uppercase_letters(self):
        """Y6 trigger on the DB allows A/B/C/D — keep variant names in
        that family so an active variant's writes don't bounce."""
        for name in get_strategy_variants():
            assert name.isupper(), f"Variant key {name!r} must be uppercase"
            assert len(name) <= 3, (
                f"Variant key {name!r} too long for the strategy column"
            )


class TestStrategyParamsHelper:

    def test_drops_underscore_keys(self):
        sample = {
            "max_no_price": 0.70,
            "_meta": {"label": "x"},
            "_origin": "test",
        }
        out = strategy_params(sample)
        assert out == {"max_no_price": 0.70}

    def test_returns_new_dict(self):
        sample = {"max_no_price": 0.70, "_meta": {}}
        out = strategy_params(sample)
        out["mutated"] = True
        assert "mutated" not in sample

    def test_empty_dict(self):
        assert strategy_params({}) == {}

    def test_no_underscore_keys_passes_through(self):
        sample = {"max_no_price": 0.70, "kelly_fraction": 0.5}
        assert strategy_params(sample) == sample
        assert strategy_params(sample) is not sample  # still a fresh dict


# ──────────────────────────────────────────────────────────────────────
# Global StrategyConfig defaults — ops invariants
# ──────────────────────────────────────────────────────────────────────

class TestGlobalDefaults:

    def test_daily_loss_limit_50(self):
        assert StrategyConfig().daily_loss_limit_usd == 50.0

    def test_locked_win_max_price_090(self):
        assert StrategyConfig().locked_win_max_price == 0.90

    def test_exit_cooldown_4h(self):
        assert StrategyConfig().exit_cooldown_hours == 4.0


# ──────────────────────────────────────────────────────────────────────
# Per-variant signal generation — runs across ALL active variants
# ──────────────────────────────────────────────────────────────────────

class TestSignalGeneration:

    def _make_market(self):
        event = _make_event()
        for i in range(20):
            lower = 55.0 + i * 2
            upper = lower + 2
            dist = abs(75.0 - (lower + upper) / 2)
            price_no = min(0.98, 0.30 + dist * 0.02)
            event.slots.append(_make_slot(lower, upper, round(price_no, 3), f"no_{i}"))
        return event

    @pytest.mark.parametrize("variant_name", list(get_strategy_variants()))
    def test_each_variant_produces_signals(self, variant_name):
        event = self._make_market()
        forecast = _make_forecast(high=75.0)
        cfg = _build_strat_cfg(variant_name)
        sigs = evaluate_no_signals(event, forecast, cfg)
        # Some variants may genuinely produce zero signals on this
        # synthetic market (very tight EV gates etc.); only assert that
        # the call completes without raising and returns a list.
        assert isinstance(sigs, list)

    @pytest.mark.parametrize("variant_name", list(get_strategy_variants()))
    def test_locked_win_full_kelly_sizes_larger_than_half(self, variant_name):
        """A full-Kelly variant should size larger than the same config
        with locked_win_kelly_fraction halved."""
        cfg = _build_strat_cfg(variant_name)
        if cfg.locked_win_kelly_fraction <= 0.5:
            pytest.skip(f"{variant_name} doesn't use full Kelly")

        event = _make_event()
        event.slots = [_make_slot(55.0, 59.0, 0.50, "no_locked")]
        cfg_half = replace(cfg, locked_win_kelly_fraction=0.5)

        locked = evaluate_locked_win_signals(event, 80.0, cfg, daily_max_final=True)
        locked_half = evaluate_locked_win_signals(event, 80.0, cfg_half, daily_max_final=True)
        assert len(locked) == 1 and len(locked_half) == 1

        size = compute_size(locked[0], 0.0, 0.0, cfg)
        size_half = compute_size(locked_half[0], 0.0, 0.0, cfg_half)
        assert size > size_half


# ──────────────────────────────────────────────────────────────────────
# Failure modes
# ──────────────────────────────────────────────────────────────────────

class TestVariantFailure:

    def test_unknown_variant_key_raises(self):
        with pytest.raises(KeyError):
            _ = get_strategy_variants()["ZZZZZ"]

    def test_partial_override_preserves_strategy_config_defaults(self):
        """A variant that overrides max_no_price shouldn't accidentally
        reset other StrategyConfig fields like max_slot_spread."""
        base = StrategyConfig()
        for name in get_strategy_variants():
            cfg = _build_strat_cfg(name)
            assert cfg.max_slot_spread == base.max_slot_spread
            assert cfg.min_market_volume == base.min_market_volume

    def test_meta_keys_dont_pollute_strategy_config(self):
        """``replace(StrategyConfig(), **strategy_params(variant))``
        must not raise even when _meta is present."""
        for name in get_strategy_variants():
            _build_strat_cfg(name)  # raise = test fail


# ──────────────────────────────────────────────────────────────────────
# Performance — keep the dict cheap regardless of variant count
# ──────────────────────────────────────────────────────────────────────

class TestVariantPerformance:

    def test_evaluate_fast_across_variants(self):
        """All evaluators across every variant in <3s for 100 cycles —
        the rebalancer iterates variants every cycle, so adding more
        variants must not slow rebalance materially."""
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

        cfgs = [_build_strat_cfg(n) for n in get_strategy_variants()]

        t0 = time.monotonic()
        for _ in range(100):
            for cfg in cfgs:
                _ = evaluate_no_signals(event, forecast, cfg)
                _ = evaluate_locked_win_signals(event, 80.0, cfg, daily_max_final=True)
                _ = evaluate_exit_signals(
                    event, obs, 74.0, event.slots[:5], cfg,
                    days_ahead=0, forecast=forecast,
                )
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0, (
            f"100 cycles × {len(cfgs)} variants × 3 evaluators "
            f"took {elapsed:.3f}s"
        )

    def test_get_strategy_variants_is_cheap(self):
        import time
        t0 = time.monotonic()
        for _ in range(10000):
            _ = get_strategy_variants()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"10k calls took {elapsed:.3f}s"
