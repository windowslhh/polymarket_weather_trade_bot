"""Auto-calibrate distance threshold from historical forecast error data.

Uses empirical forecast error distributions to compute a city-specific
safe distance threshold, replacing the fixed default (8°F).

The threshold is the P(confidence) percentile of |forecast - actual|,
meaning that `confidence`% of the time, the actual temperature lands
within ± threshold of the forecast.
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
