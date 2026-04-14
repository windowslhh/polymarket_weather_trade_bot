"""Auto-calibrate distance threshold from historical forecast error data.

Two calibration strategies:

1. calibrate_distance_threshold  — percentile-based (original)
   threshold = P(confidence) percentile of |forecast - actual|

2. calibrate_distance_dynamic    — k×std formula (preferred)
   Scales the threshold by the city's standard deviation, using a
   tighter k for accurate cities and a wider k for uncertain ones:

   Accurate city  (|mean bias| < 1.5°F AND std < 2.5°F): threshold = K_LOW  × std
   Uncertain city (|mean bias| ≥ 1.5°F  OR  std ≥ 2.5°F): threshold = K_HIGH × std

   Rationale: a city with std=1.5°F only needs a 3°F buffer (k=1.2×2.5=3)
   to cover 2σ of forecast error; a city with std=4°F needs 8°F (k=2.0×4).
   Both are calibrated to similar confidence levels relative to local
   forecast reliability.
"""
from __future__ import annotations

import logging

from src.weather.historical import ForecastErrorDistribution

logger = logging.getLogger(__name__)

# Minimum samples required for calibration; below this, return default
MIN_CALIBRATION_SAMPLES = 30

# Hard bounds to prevent pathological thresholds
MIN_THRESHOLD_F = 3
MAX_THRESHOLD_F = 15
DEFAULT_THRESHOLD_F = 8

# ── Dynamic (k×std) calibration constants ────────────────────────────────────
# Boundary for classifying a city as "accurate" or "uncertain"
_MEAN_BIAS_LIMIT_F: float = 1.5   # |mean forecast bias| below this → accurate
_STD_LIMIT_F: float = 2.5          # std of forecast errors below this → accurate

# k multipliers: threshold = k × std
_K_LOW: float = 1.2   # accurate cities — tighter threshold, more trade opportunities
_K_HIGH: float = 2.0  # uncertain cities — wider threshold, higher safety margin

# ── Ensemble spread adjustment ──────────────────────────────────────────────
# Per-city average ensemble spread from 93-day backtest (2026-01-12 to 2026-04-14).
# Only cities with Pearson r >= 0.4 between spread and forecast error are included.
# Cities not in this dict are unaffected by spread adjustment.
_AVG_SPREAD: dict[str, float] = {
    "Denver": 1.56,       # r=0.824
    "Dallas": 1.04,       # r=0.744
    "Atlanta": 1.05,      # r=0.721
    "Los Angeles": 2.16,  # r=0.570
    "Chicago": 1.33,      # r=0.525
}
# Spread ratio clamp bounds — prevent extreme threshold swings
_SPREAD_RATIO_MIN: float = 0.5
_SPREAD_RATIO_MAX: float = 2.0


def calibrate_distance_threshold(
    error_dist: ForecastErrorDistribution,
    confidence: float = 0.90,
) -> float:
    """Compute a distance threshold from the empirical forecast error distribution.

    The threshold is the percentile of |error| at the given confidence level.
    For example, confidence=0.90 means "90% of the time the actual temperature
    is within ±threshold of the forecast".

    Args:
        error_dist: Empirical distribution of (forecast - actual) errors.
        confidence: Confidence level in [0.5, 0.99].

    Returns:
        Calibrated distance threshold in °F, clamped to [MIN_THRESHOLD_F, MAX_THRESHOLD_F].
        Returns DEFAULT_THRESHOLD_F if insufficient data.
    """
    # P2-15: Clamp confidence to valid range
    confidence = max(0.5, min(confidence, 0.99))

    if error_dist._count < MIN_CALIBRATION_SAMPLES:
        logger.debug(
            "Calibration for %s: only %d samples (<%d), using default %d°F",
            error_dist.city, error_dist._count, MIN_CALIBRATION_SAMPLES, DEFAULT_THRESHOLD_F,
        )
        return float(DEFAULT_THRESHOLD_F)

    # Compute absolute errors and sort
    abs_errors = sorted(abs(e) for e in error_dist._errors)

    # Percentile index (0-based)
    idx = int(len(abs_errors) * confidence)
    idx = min(idx, len(abs_errors) - 1)
    raw_threshold = abs_errors[idx]

    # Clamp to hard bounds
    threshold = max(MIN_THRESHOLD_F, min(MAX_THRESHOLD_F, raw_threshold))

    logger.info(
        "Calibrated distance for %s: %.1f°F (raw=%.1f, confidence=%.0f%%, samples=%d)",
        error_dist.city, threshold, raw_threshold, confidence * 100, error_dist._count,
    )
    return threshold


def calibrate_distance_dynamic(
    error_dist: ForecastErrorDistribution,
    *,
    ensemble_spread_f: float | None = None,
    enable_spread_adjustment: bool = True,
) -> float:
    """Compute distance threshold using the k×std formula.

    Classifies each city as "accurate" or "uncertain" based on its empirical
    forecast error statistics, then applies the appropriate k multiplier:

        accurate  (|mean| < 1.5°F AND std < 2.5°F): threshold = K_LOW  × std = 1.2 × std
        uncertain (|mean| ≥ 1.5°F  OR  std ≥ 2.5°F): threshold = K_HIGH × std = 2.0 × std

    When ``enable_spread_adjustment`` is True **and** the city has a known
    average spread (from backtest), the threshold is further scaled by
    ``clamp(current_spread / avg_spread, 0.5, 2.0)``.  This widens the
    threshold when ensemble models disagree (high spread → larger error
    expected) and tightens it when they agree (low spread → smaller error).

    Both are clamped to [MIN_THRESHOLD_F, MAX_THRESHOLD_F].
    Returns DEFAULT_THRESHOLD_F if insufficient data (< MIN_CALIBRATION_SAMPLES).

    Examples with real city data:
        Las Vegas (mean=−0.17, std=1.47): accurate → 1.2×1.47=1.76 → clamped to 3.0°F
        Phoenix   (mean=+1.02, std=1.43): accurate → 1.2×1.43=1.72 → clamped to 3.0°F
        Denver    (mean=+2.04, std=3.59): uncertain → 2.0×3.59=7.18°F
        Cleveland (mean=+3.44, std=4.36): uncertain → 2.0×4.36=8.72°F
    """
    if error_dist._count < MIN_CALIBRATION_SAMPLES:
        logger.debug(
            "Dynamic calibration for %s: only %d samples (<%d), using default %d°F",
            error_dist.city, error_dist._count, MIN_CALIBRATION_SAMPLES, DEFAULT_THRESHOLD_F,
        )
        return float(DEFAULT_THRESHOLD_F)

    is_accurate = (
        abs(error_dist.mean) < _MEAN_BIAS_LIMIT_F
        and error_dist.std < _STD_LIMIT_F
    )
    k = _K_LOW if is_accurate else _K_HIGH
    raw = k * error_dist.std
    threshold = float(max(MIN_THRESHOLD_F, min(MAX_THRESHOLD_F, raw)))

    logger.info(
        "Dynamic distance for %s: %.1f°F "
        "(k=%.1f × std=%.2f, mean=%.2f, %s, samples=%d)",
        error_dist.city, threshold,
        k, error_dist.std, error_dist.mean,
        "accurate" if is_accurate else "uncertain",
        error_dist._count,
    )

    # Spread adjustment: scale threshold by (current_spread / avg_spread)
    avg_spread = _AVG_SPREAD.get(error_dist.city)
    if (
        enable_spread_adjustment
        and ensemble_spread_f is not None
        and avg_spread is not None
    ):
        ratio = ensemble_spread_f / avg_spread
        ratio_clamped = max(_SPREAD_RATIO_MIN, min(_SPREAD_RATIO_MAX, ratio))
        pre_adjust = threshold
        threshold = float(
            max(MIN_THRESHOLD_F, min(MAX_THRESHOLD_F, threshold * ratio_clamped))
        )
        logger.info(
            "Spread adjustment for %s: ratio=%.2f (spread=%.2f/avg=%.2f), "
            "threshold %.1f→%.1f°F",
            error_dist.city, ratio_clamped, ensemble_spread_f, avg_spread,
            pre_adjust, threshold,
        )

    return threshold
