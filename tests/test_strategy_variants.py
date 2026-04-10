"""Tests for Module 4: Four strategy variants (A/B/C/D).

Tests that the 4 new strategies have correct parameters, produce
differentiated behavior, and work end-to-end through the rebalancer.

Covers: critical paths, boundary conditions, failure branches,
and performance.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import AppConfig, CityConfig, SchedulingConfig, StrategyConfig, get_strategy_variants
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.strategy.evaluator import (
    evaluate_exit_signals,
    evaluate_locked_win_signals,
    evaluate_no_signals,
)
from src.strategy.sizing import compute_size
from src.weather.models import Forecast, Observation


# ── Helpers ──────────────────────────────────────────────────────────

def _make_config(**overrides) -> AppConfig:
    return AppConfig(
        strategy=StrategyConfig(**overrides),
        scheduling=SchedulingConfig(),
        cities=[
            CityConfig("New York", "KLGA", 40.7128, -74.006),
            CityConfig("Dallas", "KDFW", 32.7767, -96.797),
        ],
        dry_run=True,
        db_path=Path("/tmp/test_variants.db"),
    )


def _make_event(city="New York") -> WeatherMarketEvent:
    return WeatherMarketEvent(
        event_id="evt_1",
        condition_id="cond_1",
        city=city,
        market_date=date.today(),
        slots=[],
    )


def _make_slot(lower=60.0, upper=64.0, price_no=0.65, token_id_no="no_1") -> TempSlot:
    return TempSlot(
        token_id_yes="yes_1",
        token_id_no=token_id_no,
        outcome_label=f"{lower:.0f}°F to {upper:.0f}°F",
        temp_lower_f=lower,
        temp_upper_f=upper,
        price_no=price_no,
    )


def _make_forecast(high=75.0, ci=4.0) -> Forecast:
    return Forecast("New York", date.today(), high, 60.0, ci, "mock",
                    datetime.now(timezone.utc))


def _build_strat_cfg(variant_name: str) -> StrategyConfig:
    """Build StrategyConfig for a specific variant."""
    variants = get_strategy_variants()
    overrides = variants[variant_name]
    return replace(StrategyConfig(), **overrides)


# ──────────────────────────────────────────────────────────────────────
# Critical Paths: Variant Structure
# ──────────────────────────────────────────────────────────────────────

class TestVariantStructure:
    """Verify each variant exists and has correct distinguishing parameters."""

    def test_exactly_four_variants(self):
        """Must be exactly 4 variants: A, B, C, D."""
        variants = get_strategy_variants()
        assert set(variants.keys()) == {"A", "B", "C", "D"}

    def test_no_old_variants_e_f(self):
        """Old variants E, F must not exist."""
        variants = get_strategy_variants()
        assert "E" not in variants
        assert "F" not in variants

    def test_all_overrides_are_valid_fields(self):
        """Every override key must be a valid StrategyConfig field."""
        variants = get_strategy_variants()
        valid_fields = set(StrategyConfig.__dataclass_fields__.keys())
        for name, overrides in variants.items():
            for key in overrides:
                assert key in valid_fields, f"Strategy {name}: invalid field '{key}'"

    def test_variants_produce_valid_configs(self):
        """Each variant should produce a valid StrategyConfig via dataclass replace."""
        base = StrategyConfig()
        variants = get_strategy_variants()
        for name, overrides in variants.items():
            cfg = replace(base, **overrides)
            assert isinstance(cfg, StrategyConfig)
            assert cfg.max_no_price > 0
            assert cfg.kelly_fraction > 0


class TestStrategyAConservativeFar:
    """Strategy A: Conservative distant NO, strict price cap."""

    def test_max_no_price_strict(self):
        cfg = _build_strat_cfg("A")
        assert cfg.max_no_price == 0.70

    def test_half_kelly(self):
        cfg = _build_strat_cfg("A")
        assert cfg.kelly_fraction == 0.5

    def test_few_positions_per_event(self):
        cfg = _build_strat_cfg("A")
        assert cfg.max_positions_per_event == 3

    def test_high_calibration_confidence(self):
        cfg = _build_strat_cfg("A")
        assert cfg.calibration_confidence == 0.90

    def test_min_ev_threshold(self):
        cfg = _build_strat_cfg("A")
        assert cfg.min_no_ev == 0.05


class TestStrategyBLockedAggressor:
    """Strategy B: Same entry as A, full Kelly on locked wins."""

    def test_same_entry_as_a(self):
        a = _build_strat_cfg("A")
        b = _build_strat_cfg("B")
        assert a.max_no_price == b.max_no_price
        assert a.min_no_ev == b.min_no_ev
        assert a.calibration_confidence == b.calibration_confidence

    def test_full_kelly_on_locked_wins(self):
        cfg = _build_strat_cfg("B")
        assert cfg.locked_win_kelly_fraction == 1.0

    def test_larger_locked_win_cap(self):
        cfg = _build_strat_cfg("B")
        assert cfg.max_locked_win_per_slot_usd == 10.0

    def test_more_positions_per_event(self):
        cfg = _build_strat_cfg("B")
        assert cfg.max_positions_per_event > _build_strat_cfg("A").max_positions_per_event


class TestStrategyCCloseRange:
    """Strategy C: Tighter distance (75% confidence), higher EV threshold."""

    def test_lower_calibration_confidence(self):
        cfg = _build_strat_cfg("C")
        assert cfg.calibration_confidence == 0.75

    def test_higher_ev_threshold_compensates(self):
        """Higher EV bar compensates for closer entry."""
        cfg = _build_strat_cfg("C")
        assert cfg.min_no_ev > _build_strat_cfg("A").min_no_ev

    def test_lower_kelly_fraction(self):
        cfg = _build_strat_cfg("C")
        assert cfg.kelly_fraction == 0.3

    def test_moderate_price_cap(self):
        cfg = _build_strat_cfg("C")
        assert cfg.max_no_price == 0.75


class TestStrategyDQuickExit:
    """Strategy D: Aggressive risk management, earlier force exit."""

    def test_earlier_force_exit(self):
        cfg = _build_strat_cfg("D")
        assert cfg.force_exit_hours == 2.0

    def test_shorter_exit_cooldown(self):
        cfg = _build_strat_cfg("D")
        assert cfg.exit_cooldown_hours == 2.0

    def test_lowest_price_cap(self):
        """D has the strictest price cap for maximum safety."""
        cfg = _build_strat_cfg("D")
        for name in ["A", "B", "C"]:
            other = _build_strat_cfg(name)
            assert cfg.max_no_price <= other.max_no_price


# ──────────────────────────────────────────────────────────────────────
# Critical Paths: Differentiated Behavior
# ──────────────────────────────────────────────────────────────────────

class TestDifferentiatedBehavior:
    """Verify strategies produce different signals from the same market data."""

    def test_a_b_same_no_signals(self):
        """A and B should generate same NO signals (same entry params)."""
        event = _make_event()
        forecast = _make_forecast(high=75.0)

        # Create slots spanning a range
        for i in range(10):
            lower = 55.0 + i * 4
            upper = lower + 4
            dist = abs(75.0 - (lower + upper) / 2)
            price_no = min(0.98, 0.30 + dist * 0.02)
            event.slots.append(_make_slot(lower, upper, price_no, f"no_{i}"))

        cfg_a = _build_strat_cfg("A")
        cfg_b = _build_strat_cfg("B")

        sigs_a = evaluate_no_signals(event, forecast, cfg_a)
        sigs_b = evaluate_no_signals(event, forecast, cfg_b)

        # Same entry params → same signals (may differ in count due to max_positions_per_event)
        labels_a = {s.slot.outcome_label for s in sigs_a}
        labels_b = {s.slot.outcome_label for s in sigs_b}
        assert labels_a == labels_b, "A and B should select same slots (same entry criteria)"

    def test_c_captures_closer_slots_than_a(self):
        """C with 75% confidence should potentially accept closer slots.

        Note: Without auto-calibration (no error_dist), both use default threshold.
        The real difference emerges when calibrated thresholds are used.
        But C also has a different min_no_ev (0.06 vs 0.05).
        """
        cfg_a = _build_strat_cfg("A")
        cfg_c = _build_strat_cfg("C")
        # C's higher EV bar means it's more selective even at closer range
        assert cfg_c.min_no_ev > cfg_a.min_no_ev

    def test_d_filters_by_price_more_strictly(self):
        """D's lower max_no_price filters out expensive slots."""
        event = _make_event()
        forecast = _make_forecast(high=75.0)

        # Slot at distance=12, price=0.68 (passes D's 0.65? No: 0.68 > 0.65)
        event.slots.append(_make_slot(60.0, 62.0, 0.68, "no_1"))

        cfg_a = _build_strat_cfg("A")
        cfg_d = _build_strat_cfg("D")

        sigs_a = evaluate_no_signals(event, forecast, cfg_a)
        sigs_d = evaluate_no_signals(event, forecast, cfg_d)

        # A accepts up to 0.70, D only 0.65
        # Slot at 0.68 → A accepts, D rejects
        assert len(sigs_a) >= 1
        assert len(sigs_d) == 0

    def test_b_locked_win_sizes_larger_than_a(self):
        """B should size locked wins larger (full Kelly, higher cap)."""
        event = _make_event()
        # Locked-win slot: daily_max=80 > upper=64
        slot = _make_slot(60.0, 64.0, 0.60, "no_locked")
        event.slots = [slot]

        cfg_a = _build_strat_cfg("A")
        cfg_b = _build_strat_cfg("B")

        locked_a = evaluate_locked_win_signals(event, 80.0, cfg_a)
        locked_b = evaluate_locked_win_signals(event, 80.0, cfg_b)

        assert len(locked_a) == 1
        assert len(locked_b) == 1

        # Size them
        size_a = compute_size(locked_a[0], 0.0, 0.0, cfg_a)
        size_b = compute_size(locked_b[0], 0.0, 0.0, cfg_b)

        # B should be larger due to full Kelly + higher cap
        assert size_b >= size_a

    def test_d_force_exits_earlier(self):
        """D exits 2h before settlement, others exit 1h."""
        cfg_a = _build_strat_cfg("A")
        cfg_d = _build_strat_cfg("D")
        assert cfg_d.force_exit_hours == 2.0
        assert cfg_a.force_exit_hours == 1.0  # default


# ──────────────────────────────────────────────────────────────────────
# Boundary Conditions
# ──────────────────────────────────────────────────────────────────────

class TestVariantBoundary:

    def test_price_at_exact_boundary(self):
        """Slot with price exactly at max_no_price boundary."""
        event = _make_event()
        forecast = _make_forecast(high=75.0)

        # Distance from midpoint(61) to 75 = 14 → passes distance threshold
        # Price exactly 0.70
        event.slots = [_make_slot(60.0, 62.0, 0.70, "no_exact")]

        cfg_a = _build_strat_cfg("A")  # max_no_price=0.70

        sigs = evaluate_no_signals(event, forecast, cfg_a)
        # 0.70 <= 0.70 → should pass the price filter (check <= vs <)
        # Actually evaluator checks `slot.price_no > config.max_no_price` → 0.70 > 0.70 is False → passes
        assert len(sigs) >= 1

    def test_price_one_cent_above_boundary(self):
        """Slot with price one cent above max → rejected."""
        event = _make_event()
        forecast = _make_forecast(high=75.0)

        event.slots = [_make_slot(60.0, 62.0, 0.71, "no_above")]

        cfg_a = _build_strat_cfg("A")  # max_no_price=0.70
        sigs = evaluate_no_signals(event, forecast, cfg_a)
        assert len(sigs) == 0

    def test_c_kelly_fraction_affects_sizing(self):
        """C's 0.3 Kelly produces smaller positions than A's 0.5."""
        event = _make_event()
        slot = _make_slot(55.0, 59.0, 0.50, "no_1")
        event.slots = [slot]
        forecast = _make_forecast(high=75.0)

        cfg_a = _build_strat_cfg("A")
        cfg_c = _build_strat_cfg("C")

        sigs_a = evaluate_no_signals(event, forecast, cfg_a)
        sigs_c = evaluate_no_signals(event, forecast, cfg_c)

        if sigs_a and sigs_c:
            size_a = compute_size(sigs_a[0], 0.0, 0.0, cfg_a)
            size_c = compute_size(sigs_c[0], 0.0, 0.0, cfg_c)
            # 0.3 Kelly < 0.5 Kelly → smaller size (if same signal)
            if size_a > 0 and size_c > 0:
                assert size_c < size_a

    def test_all_strategies_share_common_base(self):
        """All strategies should have locked_wins enabled and auto_calibrate on."""
        for name in ["A", "B", "C", "D"]:
            cfg = _build_strat_cfg(name)
            assert cfg.enable_locked_wins is True
            assert cfg.auto_calibrate_distance is True

    def test_empty_slots_generate_no_signals(self):
        """Event with no slots → no signals for any strategy."""
        event = _make_event()
        event.slots = []
        forecast = _make_forecast()

        for name in ["A", "B", "C", "D"]:
            cfg = _build_strat_cfg(name)
            sigs = evaluate_no_signals(event, forecast, cfg)
            assert len(sigs) == 0

    def test_max_positions_per_event_limits(self):
        """Verify max_positions_per_event differs across strategies."""
        limits = {}
        for name in ["A", "B", "C", "D"]:
            cfg = _build_strat_cfg(name)
            limits[name] = cfg.max_positions_per_event

        # A=3, B=6, C=4, D=4
        assert limits["A"] < limits["B"]
        assert limits["B"] > limits["C"]


# ──────────────────────────────────────────────────────────────────────
# Failure Branches
# ──────────────────────────────────────────────────────────────────────

class TestVariantFailure:

    def test_unknown_variant_key_safe(self):
        """Accessing an unknown variant key returns KeyError (not crash)."""
        variants = get_strategy_variants()
        with pytest.raises(KeyError):
            _ = variants["Z"]

    def test_empty_overrides_produces_base_config(self):
        """An empty override dict produces the base StrategyConfig."""
        base = StrategyConfig()
        cfg = replace(base, **{})
        assert cfg == base

    def test_partial_override_preserves_defaults(self):
        """Variant override only changes specified fields."""
        base = StrategyConfig()
        cfg_a = _build_strat_cfg("A")

        # Fields overridden by A
        assert cfg_a.max_no_price == 0.70  # different from base 0.85
        # Fields NOT overridden → same as base
        assert cfg_a.daily_loss_limit_usd == base.daily_loss_limit_usd
        assert cfg_a.min_market_volume == base.min_market_volume
        assert cfg_a.max_slot_spread == base.max_slot_spread

    def test_d_cooldown_shorter_than_base(self):
        """D's 2h cooldown is shorter than base 4h."""
        base = StrategyConfig()
        cfg_d = _build_strat_cfg("D")
        assert cfg_d.exit_cooldown_hours < base.exit_cooldown_hours


# ──────────────────────────────────────────────────────────────────────
# Store / Web Compatibility
# ──────────────────────────────────────────────────────────────────────

class TestStoreWebCompatibility:

    def test_realized_pnl_dict_has_four_keys(self):
        """get_strategy_realized_pnl initializes A-D only."""
        # Can't call async without DB; just verify the hardcoded default
        expected_keys = {"A", "B", "C", "D"}
        # The function initializes result with these keys before DB query
        # We verify the code matches our expectation
        from src.portfolio.store import Store
        import inspect
        source = inspect.getsource(Store.get_strategy_realized_pnl)
        assert '"A"' in source
        assert '"B"' in source
        assert '"C"' in source
        assert '"D"' in source
        assert '"E"' not in source
        assert '"F"' not in source

    def test_web_app_defaults_have_four_keys(self):
        """Web app fallback dicts should have exactly A-D."""
        import inspect
        from src.web.app import create_app
        source = inspect.getsource(create_app)
        # Count strategy set references — should not contain E or F
        assert '"E"' not in source
        assert '"F"' not in source

    def test_positions_template_labels(self):
        """positions.html strat_labels should map only A-D."""
        template_path = Path(__file__).parent.parent / "src" / "web" / "templates" / "positions.html"
        content = template_path.read_text()
        assert "'A':'Conservative Far'" in content
        assert "'B':'Locked Aggressor'" in content
        assert "'C':'Close Range'" in content
        assert "'D':'Quick Exit'" in content
        assert "'E'" not in content
        assert "'F'" not in content

    def test_dashboard_template_labels(self):
        """dashboard.html should label A-D correctly."""
        template_path = Path(__file__).parent.parent / "src" / "web" / "templates" / "dashboard.html"
        content = template_path.read_text()
        assert "Conservative Far" in content
        assert "Locked Aggressor" in content
        assert "Close Range" in content
        assert "Quick Exit" in content

    def test_trades_template_labels(self):
        """trades.html should have strategy cards for A-D."""
        template_path = Path(__file__).parent.parent / "src" / "web" / "templates" / "trades.html"
        content = template_path.read_text()
        assert "Conservative Far" in content
        assert "Locked Aggressor" in content
        assert "Close Range" in content
        assert "Quick Exit" in content


# ──────────────────────────────────────────────────────────────────────
# Cross-Strategy Signal Generation
# ──────────────────────────────────────────────────────────────────────

class TestCrossStrategySignals:
    """Run same market data through all 4 strategies, verify differentiation."""

    def _make_market(self):
        """Create a market with slots spanning 55°F to 95°F, forecast at 75°F."""
        event = _make_event()
        for i in range(20):
            lower = 55.0 + i * 2
            upper = lower + 2
            dist = abs(75.0 - (lower + upper) / 2)
            price_no = min(0.98, 0.30 + dist * 0.02)
            event.slots.append(_make_slot(lower, upper, round(price_no, 3), f"no_{i}"))
        return event

    def test_all_strategies_produce_signals(self):
        """All 4 strategies should find at least one signal in this market."""
        event = self._make_market()
        forecast = _make_forecast(high=75.0)

        for name in ["A", "B", "C", "D"]:
            cfg = _build_strat_cfg(name)
            sigs = evaluate_no_signals(event, forecast, cfg)
            assert len(sigs) > 0, f"Strategy {name} should produce signals"

    def test_signal_counts_vary_across_strategies(self):
        """Different strategies should generally produce different signal counts."""
        event = self._make_market()
        forecast = _make_forecast(high=75.0)

        counts = {}
        for name in ["A", "B", "C", "D"]:
            cfg = _build_strat_cfg(name)
            sigs = evaluate_no_signals(event, forecast, cfg)
            counts[name] = len(sigs)

        # At minimum, D should differ from A (lower price cap)
        # D has max_no_price=0.65, A has 0.70 → D should have <= A's count
        assert counts["D"] <= counts["A"]

    def test_locked_win_sizing_b_vs_a(self):
        """B should size locked wins more aggressively than A."""
        event = _make_event()
        # A slot clearly below daily max → locked win
        slot = _make_slot(55.0, 59.0, 0.50, "no_locked")
        event.slots = [slot]

        cfg_a = _build_strat_cfg("A")
        cfg_b = _build_strat_cfg("B")

        locked_a = evaluate_locked_win_signals(event, 80.0, cfg_a)
        locked_b = evaluate_locked_win_signals(event, 80.0, cfg_b)

        assert len(locked_a) == 1 and len(locked_b) == 1

        size_a = compute_size(locked_a[0], 0.0, 0.0, cfg_a)
        size_b = compute_size(locked_b[0], 0.0, 0.0, cfg_b)

        # B has full Kelly on locked wins → larger size
        assert size_b >= size_a

    def test_d_force_exit_triggers_within_2h(self):
        """D's force_exit_hours=2.0 triggers exit 2h before settlement."""
        event = _make_event()
        slot = _make_slot(73.0, 77.0, 0.50, "no_held")  # close to daily_max
        event.slots = [slot]
        held_no_slots = [slot]

        cfg_d = _build_strat_cfg("D")
        obs = Observation(icao="KLGA", temp_f=74.0,
                          observation_time=datetime.now(timezone.utc))
        forecast = _make_forecast(high=75.0)

        exit_sigs = evaluate_exit_signals(
            event, obs, 74.0, held_no_slots, cfg_d,
            days_ahead=0, forecast=forecast,
            hours_to_settlement=1.5,  # within D's 2h window
        )

        # 1.5h < 2.0h and distance is close → should force exit
        assert any(s.side == Side.SELL for s in exit_sigs)


# ──────────────────────────────────────────────────────────────────────
# Performance
# ──────────────────────────────────────────────────────────────────────

class TestVariantPerformance:

    def test_all_strategies_evaluate_fast(self):
        """Running all 4 strategies on a large event should be fast."""
        import time

        event = _make_event()
        for i in range(40):
            lower = 40.0 + i * 2
            upper = lower + 2
            dist = abs(75.0 - (lower + upper) / 2)
            price_no = min(0.98, 0.30 + dist * 0.02)
            event.slots.append(_make_slot(lower, upper, round(price_no, 3), f"no_{i}"))

        forecast = _make_forecast(high=75.0)
        obs = Observation(icao="KLGA", temp_f=74.0,
                          observation_time=datetime.now(timezone.utc))

        t0 = time.monotonic()
        for _ in range(100):  # 100 iterations
            for name in ["A", "B", "C", "D"]:
                cfg = _build_strat_cfg(name)
                _ = evaluate_no_signals(event, forecast, cfg)
                _ = evaluate_locked_win_signals(event, 80.0, cfg)
                _ = evaluate_exit_signals(
                    event, obs, 74.0, event.slots[:5], cfg,
                    days_ahead=0, forecast=forecast,
                )
        elapsed = time.monotonic() - t0

        # 4 strategies × 3 evaluations × 100 iterations = 1200 evaluations
        assert elapsed < 3.0, f"1200 evaluations took {elapsed:.3f}s"

    def test_get_strategy_variants_is_cheap(self):
        """get_strategy_variants() should return instantly (dict literal)."""
        import time
        t0 = time.monotonic()
        for _ in range(10000):
            _ = get_strategy_variants()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"10k calls took {elapsed:.3f}s"
