"""Tests for auto-calibrated distance threshold (Module 2A).

Covers: critical paths, boundary conditions, failure branches, performance risks.
"""
from __future__ import annotations

import time

import pytest

from src.strategy.calibrator import (
    DEFAULT_THRESHOLD_F,
    MAX_THRESHOLD_F,
    MIN_CALIBRATION_SAMPLES,
    MIN_THRESHOLD_F,
    _K_HIGH,
    _K_LOW,
    _MEAN_BIAS_LIMIT_F,
    _STD_LIMIT_F,
    calibrate_distance_dynamic,
    calibrate_distance_threshold,
)
from src.weather.historical import ForecastErrorDistribution


def _make_dist(errors: list[float], city: str = "TestCity") -> ForecastErrorDistribution:
    """Create a ForecastErrorDistribution from a list of errors."""
    return ForecastErrorDistribution(city, errors)


def _uniform_errors(n: int, spread: float) -> list[float]:
    """Generate N evenly-spaced errors in [-spread, +spread]."""
    if n <= 1:
        return [0.0] * n
    return [(-spread + 2 * spread * i / (n - 1)) for i in range(n)]


def _constant_errors(n: int, value: float) -> list[float]:
    """Generate N identical errors."""
    return [value] * n


# ──────────────────────────────────────────────────────────────────────
# Critical paths
# ──────────────────────────────────────────────────────────────────────

class TestCalibrateBasic:
    """Core functionality: correct threshold from representative data."""

    def test_symmetric_errors_90pct(self):
        """Symmetric ±5°F uniform errors at 90% → threshold ≈ 4.5-5.0."""
        errors = _uniform_errors(100, 5.0)
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        # 90th percentile of |error| for uniform [-5,5] ≈ 4.5
        assert 4.0 <= threshold <= 5.5

    def test_symmetric_errors_75pct(self):
        """Lower confidence → lower threshold (closer entry allowed)."""
        errors = _uniform_errors(100, 5.0)
        dist = _make_dist(errors)
        t90 = calibrate_distance_threshold(dist, confidence=0.90)
        t75 = calibrate_distance_threshold(dist, confidence=0.75)
        assert t75 < t90

    def test_tight_errors_yield_low_threshold(self):
        """Tight errors (±2°F) should give low threshold, clamped to MIN."""
        errors = _uniform_errors(100, 2.0)
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        assert threshold == MIN_THRESHOLD_F  # clamped to 3

    def test_wide_errors_yield_high_threshold(self):
        """Wide errors (±20°F) should give high threshold, clamped to MAX."""
        errors = _uniform_errors(100, 20.0)
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        assert threshold == MAX_THRESHOLD_F  # clamped to 15

    def test_positive_bias_errors(self):
        """Forecast consistently too high (all errors +3 to +7) → abs same magnitude."""
        errors = [3.0 + 4.0 * i / 99 for i in range(100)]  # 3.0 to 7.0
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        # 90th pctile of |3..7| ≈ 6.6
        assert 6.0 <= threshold <= 7.5

    def test_negative_bias_errors(self):
        """Forecast consistently too low (all errors -7 to -3) → abs same magnitude."""
        errors = [-7.0 + 4.0 * i / 99 for i in range(100)]  # -7.0 to -3.0
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        # abs values same as positive bias
        assert 6.0 <= threshold <= 7.5

    def test_realistic_gaussian_errors(self):
        """Gaussian errors std=3°F at 90% → threshold ≈ 4-6°F."""
        import random
        rng = random.Random(42)
        errors = [rng.gauss(0, 3.0) for _ in range(500)]
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        # For N(0,3): 90th pctile of |X| ≈ 3 * 1.645 ≈ 4.9
        assert 4.0 <= threshold <= 6.5

    def test_higher_confidence_higher_threshold(self):
        """Monotonicity: higher confidence → higher (or equal) threshold."""
        import random
        rng = random.Random(123)
        errors = [rng.gauss(0, 4.0) for _ in range(200)]
        dist = _make_dist(errors)
        t50 = calibrate_distance_threshold(dist, confidence=0.50)
        t75 = calibrate_distance_threshold(dist, confidence=0.75)
        t90 = calibrate_distance_threshold(dist, confidence=0.90)
        t99 = calibrate_distance_threshold(dist, confidence=0.99)
        assert t50 <= t75 <= t90 <= t99

    def test_returns_float(self):
        """Return type is always float."""
        dist = _make_dist(_uniform_errors(50, 5.0))
        result = calibrate_distance_threshold(dist, 0.90)
        assert isinstance(result, float)


# ──────────────────────────────────────────────────────────────────────
# Boundary conditions
# ──────────────────────────────────────────────────────────────────────

class TestCalibrateBoundary:
    """Edge cases for inputs and outputs."""

    def test_exactly_min_samples(self):
        """Exactly MIN_CALIBRATION_SAMPLES → should calibrate, not fallback."""
        errors = _uniform_errors(MIN_CALIBRATION_SAMPLES, 5.0)
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        # Should actually calibrate (not return default)
        assert threshold != DEFAULT_THRESHOLD_F or 4.0 <= threshold <= 5.5

    def test_one_below_min_samples_returns_default(self):
        """MIN_CALIBRATION_SAMPLES - 1 → fallback to default."""
        errors = _uniform_errors(MIN_CALIBRATION_SAMPLES - 1, 5.0)
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        assert threshold == float(DEFAULT_THRESHOLD_F)

    def test_all_zero_errors(self):
        """All errors = 0 → all abs errors = 0 → clamped to MIN."""
        errors = _constant_errors(50, 0.0)
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        assert threshold == float(MIN_THRESHOLD_F)

    def test_all_identical_nonzero_errors(self):
        """All errors = 5.0 → 90th pctile of |5| = 5."""
        errors = _constant_errors(50, 5.0)
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        assert threshold == 5.0

    def test_confidence_0_50(self):
        """50% confidence → median absolute error."""
        errors = _uniform_errors(100, 10.0)
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.50)
        # Median |error| for uniform [-10,10] ≈ 5.0
        assert 4.0 <= threshold <= 6.0

    def test_confidence_0_99(self):
        """99% confidence → near-max absolute error."""
        errors = _uniform_errors(100, 10.0)
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.99)
        # 99th pctile of |uniform[-10,10]| ≈ 10, but clamped to MAX_THRESHOLD_F
        assert threshold <= MAX_THRESHOLD_F

    def test_single_outlier_doesnt_break(self):
        """One extreme outlier among normal data → threshold clamped to MAX."""
        import random
        rng = random.Random(7)
        errors = [rng.gauss(0, 3.0) for _ in range(99)] + [100.0]
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.99)
        assert threshold <= MAX_THRESHOLD_F

    def test_min_clamp_enforced(self):
        """Even with very tight errors, never go below MIN_THRESHOLD_F."""
        errors = _uniform_errors(100, 0.5)  # errors within ±0.5°F
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        assert threshold >= MIN_THRESHOLD_F

    def test_max_clamp_enforced(self):
        """Even with very wide errors, never go above MAX_THRESHOLD_F."""
        errors = _uniform_errors(100, 50.0)  # errors within ±50°F
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.99)
        assert threshold <= MAX_THRESHOLD_F


# ──────────────────────────────────────────────────────────────────────
# Failure branches
# ──────────────────────────────────────────────────────────────────────

class TestCalibrateFailureBranches:
    """Cases where calibration falls back or handles degenerate input."""

    def test_zero_samples_returns_default(self):
        """Empty error list → default threshold."""
        dist = _make_dist([])
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        assert threshold == float(DEFAULT_THRESHOLD_F)

    def test_one_sample_returns_default(self):
        """Single sample < MIN_CALIBRATION_SAMPLES → default."""
        dist = _make_dist([3.0])
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        assert threshold == float(DEFAULT_THRESHOLD_F)

    def test_ten_samples_returns_default(self):
        """10 samples < 30 → default."""
        dist = _make_dist(_uniform_errors(10, 5.0))
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        assert threshold == float(DEFAULT_THRESHOLD_F)

    def test_all_negative_errors(self):
        """All negative errors → abs converts correctly."""
        errors = [-abs(x) for x in _uniform_errors(100, 8.0)]
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        # Same as positive because we take abs()
        assert MIN_THRESHOLD_F <= threshold <= MAX_THRESHOLD_F

    def test_mixed_sign_large_bias(self):
        """Large mean bias but small variance → threshold dominated by bias magnitude."""
        # All errors around +10 (forecast always 10° too high)
        errors = [10.0 + 0.1 * (i - 50) for i in range(100)]  # 5.0 to 15.0
        dist = _make_dist(errors)
        threshold = calibrate_distance_threshold(dist, confidence=0.90)
        # abs(errors) range ~5..15, 90th pctile ≈ 14 → clamped to 15
        assert threshold >= 10.0


# ──────────────────────────────────────────────────────────────────────
# Config integration
# ──────────────────────────────────────────────────────────────────────

class TestConfigIntegration:
    """Verify StrategyConfig fields and their defaults."""

    def test_auto_calibrate_default_on(self):
        from src.config import StrategyConfig
        cfg = StrategyConfig()
        assert cfg.auto_calibrate_distance is True

    def test_calibration_confidence_default(self):
        from src.config import StrategyConfig
        cfg = StrategyConfig()
        assert cfg.calibration_confidence == 0.90

    def test_config_override(self):
        from src.config import StrategyConfig
        cfg = StrategyConfig(auto_calibrate_distance=False, calibration_confidence=0.75)
        assert cfg.auto_calibrate_distance is False
        assert cfg.calibration_confidence == 0.75

    def test_dataclass_replace_works(self):
        """Verify replace() with new fields works (used in rebalancer)."""
        from dataclasses import replace
        from src.config import StrategyConfig
        cfg = StrategyConfig()
        cfg2 = replace(cfg, calibration_confidence=0.80)
        assert cfg2.calibration_confidence == 0.80
        assert cfg.calibration_confidence == 0.90  # original unchanged


# ──────────────────────────────────────────────────────────────────────
# Rebalancer integration (verify calibration is wired correctly)
# ──────────────────────────────────────────────────────────────────────

class TestRebalancerCalibrationWiring:
    """Verify the calibrator is correctly imported and callable from rebalancer context."""

    def test_calibrator_imported_in_rebalancer(self):
        """Rebalancer module should import both calibration functions."""
        import src.strategy.rebalancer as mod
        assert hasattr(mod, "calibrate_distance_threshold")
        assert hasattr(mod, "calibrate_distance_dynamic")

    def test_calibration_affects_strat_cfg(self):
        """Simulate what rebalancer does: replace threshold after calibration."""
        from dataclasses import replace
        from src.config import StrategyConfig

        base_cfg = StrategyConfig(no_distance_threshold_f=8, auto_calibrate_distance=True,
                                  calibration_confidence=0.90)
        # Simulate a city with tight errors → calibrated to 4°F
        dist = _make_dist(_uniform_errors(100, 4.5))
        cal_dist = calibrate_distance_threshold(dist, base_cfg.calibration_confidence)
        strat_cfg = replace(base_cfg, no_distance_threshold_f=round(cal_dist))

        # Should be calibrated (not the original 8)
        assert strat_cfg.no_distance_threshold_f != 8
        assert MIN_THRESHOLD_F <= strat_cfg.no_distance_threshold_f <= MAX_THRESHOLD_F

    def test_calibration_skipped_when_disabled(self):
        """When auto_calibrate_distance=False, threshold stays as configured."""
        from dataclasses import replace
        from src.config import StrategyConfig

        base_cfg = StrategyConfig(no_distance_threshold_f=8, auto_calibrate_distance=False)
        # Simulate rebalancer logic: only calibrate if flag is True
        dist = _make_dist(_uniform_errors(100, 4.5))
        strat_cfg = base_cfg
        if strat_cfg.auto_calibrate_distance and dist is not None:
            cal_dist = calibrate_distance_threshold(dist, strat_cfg.calibration_confidence)
            strat_cfg = replace(strat_cfg, no_distance_threshold_f=round(cal_dist))
        assert strat_cfg.no_distance_threshold_f == 8  # unchanged

    def test_calibration_skipped_when_no_error_dist(self):
        """When error_dist is None, threshold stays as configured."""
        from dataclasses import replace
        from src.config import StrategyConfig

        base_cfg = StrategyConfig(no_distance_threshold_f=8, auto_calibrate_distance=True)
        error_dist = None
        strat_cfg = base_cfg
        if strat_cfg.auto_calibrate_distance and error_dist is not None:
            cal_dist = calibrate_distance_threshold(error_dist, strat_cfg.calibration_confidence)
            strat_cfg = replace(strat_cfg, no_distance_threshold_f=round(cal_dist))
        assert strat_cfg.no_distance_threshold_f == 8  # unchanged


# ──────────────────────────────────────────────────────────────────────
# End-to-end: calibrated threshold feeds into signal generation
# ──────────────────────────────────────────────────────────────────────

class TestCalibratedSignalGeneration:
    """Verify that calibrated threshold actually changes signal output."""

    def _make_slot(self, lower, upper, price_no=0.80):
        from src.markets.models import TempSlot
        label = f"{lower}°F to {upper}°F" if lower and upper else ""
        return TempSlot(
            token_id_yes="yes", token_id_no="no",
            outcome_label=label, temp_lower_f=lower, temp_upper_f=upper,
            price_yes=1.0 - price_no, price_no=price_no,
        )

    def _make_event(self, slots):
        from datetime import date, datetime, timezone
        from src.markets.models import WeatherMarketEvent
        return WeatherMarketEvent(
            event_id="e1", condition_id="c1", city="TestCity",
            market_date=date.today(), slots=slots,
            end_timestamp=datetime(2026, 4, 10, 23, 0, tzinfo=timezone.utc),
            title="Test",
        )

    def _make_forecast(self, high=75.0):
        from datetime import date, datetime, timezone
        from src.weather.models import Forecast
        return Forecast(
            city="TestCity", forecast_date=date.today(),
            predicted_high_f=high, predicted_low_f=high - 15,
            confidence_interval_f=4.0, source="test",
            fetched_at=datetime.now(timezone.utc),
        )

    def test_tight_calibration_allows_more_signals(self):
        """Tight errors → lower calibrated threshold → slot at distance 5 passes."""
        from dataclasses import replace
        from src.config import StrategyConfig
        from src.strategy.evaluator import evaluate_no_signals

        # Slot at distance 5 from forecast (75): slot [80, 84]
        slot = self._make_slot(80, 84)
        event = self._make_event([slot])
        forecast = self._make_forecast(75.0)

        # Default threshold=8 → distance 5 < 8 → NO signal (skipped)
        cfg_default = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.95)
        sig_default = evaluate_no_signals(event, forecast, cfg_default)
        assert len(sig_default) == 0

        # Calibrated threshold=4 (from tight errors) → distance 5 >= 4 → signal!
        dist = _make_dist(_uniform_errors(100, 4.0))
        cal = calibrate_distance_threshold(dist, 0.90)
        cfg_cal = replace(cfg_default, no_distance_threshold_f=round(cal))
        assert cfg_cal.no_distance_threshold_f <= 4
        sig_cal = evaluate_no_signals(event, forecast, cfg_cal)
        assert len(sig_cal) == 1

    def test_wide_calibration_blocks_more_signals(self):
        """Wide errors → higher calibrated threshold → slot at distance 9 blocked."""
        from dataclasses import replace
        from src.config import StrategyConfig
        from src.strategy.evaluator import evaluate_no_signals

        # Slot at distance 9: slot [84, 88], forecast=75, distance=9
        slot = self._make_slot(84, 88)
        event = self._make_event([slot])
        forecast = self._make_forecast(75.0)

        # Default threshold=8 → distance 9 >= 8 → signal
        cfg_default = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01, max_no_price=0.95)
        sig_default = evaluate_no_signals(event, forecast, cfg_default)
        assert len(sig_default) == 1

        # Wide errors → calibrated threshold=12 → distance 9 < 12 → blocked
        dist = _make_dist(_uniform_errors(100, 13.0))
        cal = calibrate_distance_threshold(dist, 0.90)
        cfg_cal = replace(cfg_default, no_distance_threshold_f=round(cal))
        assert cfg_cal.no_distance_threshold_f >= 10
        sig_cal = evaluate_no_signals(event, forecast, cfg_cal)
        assert len(sig_cal) == 0


# ──────────────────────────────────────────────────────────────────────
# calibrate_distance_dynamic (k×std formula)
# ──────────────────────────────────────────────────────────────────────

class TestCalibrateDistanceDynamic:
    """Tests for the k×std dynamic calibration function."""

    # ── Insufficient data ─────────────────────────────────────────────

    def test_no_samples_returns_default(self):
        dist = _make_dist([])
        assert calibrate_distance_dynamic(dist) == float(DEFAULT_THRESHOLD_F)

    def test_insufficient_samples_returns_default(self):
        dist = _make_dist(_uniform_errors(MIN_CALIBRATION_SAMPLES - 1, 3.0))
        assert calibrate_distance_dynamic(dist) == float(DEFAULT_THRESHOLD_F)

    def test_exactly_min_samples_calibrates(self):
        # Accurate city with exactly MIN_CALIBRATION_SAMPLES: should calibrate, not fallback
        dist = _make_dist(_constant_errors(MIN_CALIBRATION_SAMPLES, 0.5))  # mean~0.5, std~0
        result = calibrate_distance_dynamic(dist)
        # std≈0 → raw=k×0=0 → clamped to MIN_THRESHOLD_F
        assert result == float(MIN_THRESHOLD_F)

    # ── k selection ───────────────────────────────────────────────────

    def test_accurate_city_uses_k_low(self):
        """City with |mean| < MEAN_BIAS_LIMIT and std < STD_LIMIT → k = K_LOW."""
        # mean≈0, std=2.0 → accurate
        errors = [2.0 * (i / 99 - 0.5) for i in range(100)]  # mean=0, std≈0.58 ... use bigger range
        errors = _uniform_errors(100, 2.0 * 1.732)  # std≈2.0 for uniform over [-sqrt(3)×2, +sqrt(3)×2]
        # Actually let's just use constant ±1.5 → std=1.5
        errors = ([1.5] * 50) + ([-1.5] * 50)  # mean=0, std=1.5
        dist = _make_dist(errors)
        assert abs(dist.mean) < _MEAN_BIAS_LIMIT_F
        assert dist.std < _STD_LIMIT_F
        result = calibrate_distance_dynamic(dist)
        expected_raw = _K_LOW * dist.std
        expected = max(MIN_THRESHOLD_F, min(MAX_THRESHOLD_F, expected_raw))
        assert abs(result - expected) < 1e-9

    def test_high_mean_bias_uses_k_high(self):
        """City with |mean| >= MEAN_BIAS_LIMIT → k = K_HIGH (even if std is small)."""
        # mean = +2.0°F (> 1.5 limit), std ≈ 0.5
        errors = ([2.5] * 50) + ([1.5] * 50)  # mean=2.0, std=0.5
        dist = _make_dist(errors)
        assert abs(dist.mean) >= _MEAN_BIAS_LIMIT_F
        result = calibrate_distance_dynamic(dist)
        expected_raw = _K_HIGH * dist.std
        expected = max(MIN_THRESHOLD_F, min(MAX_THRESHOLD_F, expected_raw))
        assert abs(result - expected) < 1e-9

    def test_high_std_uses_k_high(self):
        """City with std >= STD_LIMIT → k = K_HIGH (even if mean bias is small)."""
        # mean ≈ 0 (< 1.5), std = 3.0 (> 2.5)
        errors = ([3.0] * 50) + ([-3.0] * 50)  # mean=0, std=3.0
        dist = _make_dist(errors)
        assert abs(dist.mean) < _MEAN_BIAS_LIMIT_F
        assert dist.std >= _STD_LIMIT_F
        result = calibrate_distance_dynamic(dist)
        expected_raw = _K_HIGH * dist.std
        expected = max(MIN_THRESHOLD_F, min(MAX_THRESHOLD_F, expected_raw))
        assert abs(result - expected) < 1e-9

    # ── Real city examples ────────────────────────────────────────────

    def test_las_vegas_profile_gets_min_floor(self):
        """Las Vegas profile: mean=−0.17, std=1.47 → accurate → 1.2×1.47=1.76 → floor to 3°F."""
        errors = ([1.47] * 365) + ([-1.47] * 366)  # mean≈0, std≈1.47
        # Shift by -0.17 to simulate Las Vegas mean
        errors = [e - 0.17 for e in errors]
        dist = _make_dist(errors, "Las Vegas")
        result = calibrate_distance_dynamic(dist)
        assert result == float(MIN_THRESHOLD_F), f"Las Vegas should hit floor, got {result}"

    def test_cleveland_profile_gives_large_threshold(self):
        """Cleveland profile: mean=+3.44, std=4.36 → uncertain → 2.0×4.36=8.72 → 8.7°F."""
        errors = ([3.44 + 4.36] * 365) + ([3.44 - 4.36] * 366)  # mean=3.44, std≈4.36
        dist = _make_dist(errors, "Cleveland")
        result = calibrate_distance_dynamic(dist)
        # 2.0 × 4.36 = 8.72 → within [3, 15]
        assert result > 6.0, f"Cleveland should get wide threshold, got {result}"
        assert result <= MAX_THRESHOLD_F

    def test_denver_profile(self):
        """Denver: mean=+2.04, std=3.59 → uncertain → 2.0×3.59=7.18°F."""
        errors = ([2.04 + 3.59] * 365) + ([2.04 - 3.59] * 366)
        dist = _make_dist(errors, "Denver")
        result = calibrate_distance_dynamic(dist)
        assert 6.0 <= result <= 8.0, f"Denver threshold out of expected range: {result}"

    # ── Clamping ──────────────────────────────────────────────────────

    def test_min_clamp_applied(self):
        """Very small std → raw below MIN_THRESHOLD_F → clamped."""
        errors = _constant_errors(50, 0.1)  # std≈0
        dist = _make_dist(errors)
        assert calibrate_distance_dynamic(dist) >= MIN_THRESHOLD_F

    def test_max_clamp_applied(self):
        """Huge std → raw above MAX_THRESHOLD_F → clamped."""
        errors = ([20.0] * 50) + ([-20.0] * 50)  # std=20
        dist = _make_dist(errors)
        assert calibrate_distance_dynamic(dist) <= MAX_THRESHOLD_F

    # ── Monotonicity ──────────────────────────────────────────────────

    def test_wider_std_gives_larger_or_equal_threshold(self):
        """For same k class, wider std → wider threshold (before clamping)."""
        # Both uncertain (high std): compare std=3.0 vs std=4.0
        e_narrow = ([3.5] * 50) + ([-3.5] * 50)  # std=3.5 (uncertain: std>=2.5)
        e_wide   = ([4.5] * 50) + ([-4.5] * 50)  # std=4.5 (uncertain)
        t_narrow = calibrate_distance_dynamic(_make_dist(e_narrow))
        t_wide   = calibrate_distance_dynamic(_make_dist(e_wide))
        assert t_wide >= t_narrow

    # ── Return type ───────────────────────────────────────────────────

    def test_returns_float(self):
        dist = _make_dist(_uniform_errors(50, 3.0))
        assert isinstance(calibrate_distance_dynamic(dist), float)


# ──────────────────────────────────────────────────────────────────────
# Performance
# ──────────────────────────────────────────────────────────────────────

class TestCalibratePerformance:
    """Ensure calibration is fast even with large error sets."""

    def test_730_samples_fast(self):
        """730 samples (2 years) should calibrate in <10ms."""
        import random
        rng = random.Random(42)
        errors = [rng.gauss(0, 4.0) for _ in range(730)]
        dist = _make_dist(errors)

        t0 = time.monotonic()
        for _ in range(100):
            calibrate_distance_threshold(dist, 0.90)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"100 iterations with 730 samples took {elapsed:.3f}s"

    def test_5000_samples_fast(self):
        """5000 samples (hypothetical long history) should still be fast."""
        import random
        rng = random.Random(99)
        errors = [rng.gauss(0, 5.0) for _ in range(5000)]
        dist = _make_dist(errors)

        t0 = time.monotonic()
        threshold = calibrate_distance_threshold(dist, 0.90)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, f"5000 samples took {elapsed:.3f}s"
        assert MIN_THRESHOLD_F <= threshold <= MAX_THRESHOLD_F
