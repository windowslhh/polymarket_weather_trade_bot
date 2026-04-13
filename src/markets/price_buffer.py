"""TWAP price buffer with outlier filtering and CLOB/Gamma cross-validation.

Maintains a sliding-window time-weighted average price (TWAP) per token ID.
Filters single-point spikes before they can distort strategy decisions.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Constants (tunable) ──────────────────────────────────────────────────────

# How long a price sample stays in the TWAP window (seconds)
TWAP_WINDOW_SECONDS: int = 300  # 5 minutes

# Maximum number of samples kept per token (memory guard)
TWAP_MAX_SAMPLES: int = 20

# If a new price deviates from the current TWAP by more than this fraction,
# treat it as an outlier and discard it (0.10 = 10%).
OUTLIER_THRESHOLD: float = 0.10

# Absolute minimum price change that qualifies as an outlier regardless of %.
# For low-price tokens (e.g. 0.10), a 10% deviation is only $0.01 — well within
# normal market noise.  The absolute floor prevents the percentage check from
# being too aggressive at extremes: a move must exceed BOTH the % threshold
# AND this absolute floor to be discarded.
# Examples at OUTLIER_THRESHOLD=10%:
#   price=0.10, move=0.02 → 20% but only $0.02 → accepted (< floor)
#   price=0.10, move=0.06 → 60% and $0.06 → discarded (both exceeded)
#   price=0.70, move=0.08 → 11.4% and $0.08 → discarded (both exceeded)
OUTLIER_MIN_ABSOLUTE: float = 0.05  # 5 cents

# If CLOB midpoint deviates from Gamma price by more than this fraction,
# fall back to Gamma and log a warning (0.05 = 5%)
CLOB_GAMMA_DIVERGENCE_THRESHOLD: float = 0.05

# Minimum samples in window before TWAP is used (below this → raw price)
TWAP_MIN_SAMPLES: int = 2


@dataclass
class _PriceSample:
    price: float
    ts: float  # Unix timestamp (seconds)


class PriceBuffer:
    """Per-token sliding-window TWAP with outlier filtering.

    Usage:
        buf = PriceBuffer()
        smoothed = buf.update("token_abc", 0.72)   # returns TWAP (or raw if window thin)
        twap = buf.get_twap("token_abc")             # current TWAP without inserting new data
    """

    def __init__(
        self,
        window_seconds: int = TWAP_WINDOW_SECONDS,
        max_samples: int = TWAP_MAX_SAMPLES,
        outlier_threshold: float = OUTLIER_THRESHOLD,
        min_samples: int = TWAP_MIN_SAMPLES,
        min_absolute: float = OUTLIER_MIN_ABSOLUTE,
    ) -> None:
        self._window = window_seconds
        self._max = max_samples
        self._outlier = outlier_threshold
        self._min = min_samples
        self._min_absolute = min_absolute
        self._data: dict[str, list[_PriceSample]] = defaultdict(list)

    # ── Public API ────────────────────────────────────────────────────────────

    def update(self, token_id: str, price: float, now: float | None = None) -> float:
        """Insert a new price sample and return the smoothed (TWAP) price.

        If the sample is an outlier relative to the current window TWAP it is
        discarded and the existing TWAP is returned unchanged.

        Returns the raw price directly when the window has fewer than
        ``min_samples`` points (e.g. on startup).
        """
        ts = now if now is not None else datetime.now(timezone.utc).timestamp()
        self._evict(token_id, ts)

        samples = self._data[token_id]
        current_twap = self._compute_twap(samples)

        # Outlier check: only applies when there are enough reference points.
        # Uses a HYBRID threshold: both the percentage deviation AND the absolute
        # change must exceed their respective floors before a sample is discarded.
        # This prevents the percentage check from being too aggressive on low-price
        # tokens where 10% is only a few cents of normal market noise.
        if current_twap is not None and len(samples) >= self._min:
            abs_change = abs(price - current_twap)
            pct_deviation = abs_change / current_twap if current_twap != 0 else 0.0
            if pct_deviation > self._outlier and abs_change >= self._min_absolute:
                logger.warning(
                    "Price outlier discarded for %s: %.4f vs TWAP %.4f "
                    "(dev=%.1f%%, abs=%.4f)",
                    token_id[:16], price, current_twap, pct_deviation * 100, abs_change,
                )
                return current_twap

        # Accept the sample
        samples.append(_PriceSample(price=price, ts=ts))
        if len(samples) > self._max:
            samples.pop(0)

        result = self._compute_twap(samples)
        return result if result is not None else price

    def get_twap(self, token_id: str, now: float | None = None) -> float | None:
        """Return current TWAP for a token, or None if no data."""
        ts = now if now is not None else datetime.now(timezone.utc).timestamp()
        self._evict(token_id, ts)
        return self._compute_twap(self._data[token_id])

    def apply_batch(
        self,
        prices: dict[str, float],
        now: float | None = None,
    ) -> dict[str, float]:
        """Insert a dict of raw prices; return dict of smoothed prices."""
        ts = now if now is not None else datetime.now(timezone.utc).timestamp()
        return {tid: self.update(tid, p, now=ts) for tid, p in prices.items()}

    def cross_validate(
        self,
        clob_prices: dict[str, float],
        gamma_prices: dict[str, float],
        threshold: float = CLOB_GAMMA_DIVERGENCE_THRESHOLD,
    ) -> dict[str, float]:
        """Merge CLOB and Gamma prices; fall back to Gamma when they diverge.

        For each token:
        - If only one source has a price, use it.
        - If both have prices and they agree (within threshold), use CLOB.
        - If they diverge beyond threshold, use Gamma and log the discrepancy.

        Returns the merged dict suitable for feeding into ``apply_batch``.
        """
        merged: dict[str, float] = {}
        all_tokens = set(clob_prices) | set(gamma_prices)

        for tid in all_tokens:
            clob = clob_prices.get(tid)
            gamma = gamma_prices.get(tid)

            if clob is None:
                merged[tid] = gamma  # type: ignore[assignment]
                continue
            if gamma is None:
                merged[tid] = clob
                continue

            # Both available — check divergence
            divergence = abs(clob - gamma) / gamma if gamma != 0 else 0.0
            if divergence > threshold:
                logger.warning(
                    "CLOB/Gamma divergence for %s: CLOB=%.4f Gamma=%.4f (%.1f%%) → using Gamma",
                    tid[:16], clob, gamma, divergence * 100,
                )
                merged[tid] = gamma
            else:
                merged[tid] = clob  # CLOB wins when consistent

        return merged

    def sample_counts(self, now: float | None = None) -> dict[str, int]:
        """Return number of valid window samples per token (for diagnostics)."""
        ts = now if now is not None else datetime.now(timezone.utc).timestamp()
        return {tid: len([s for s in samps if s.ts >= ts - self._window])
                for tid, samps in self._data.items() if samps}

    # ── Internal ──────────────────────────────────────────────────────────────

    def _evict(self, token_id: str, now: float) -> None:
        """Remove samples older than the window for a single token."""
        cutoff = now - self._window
        samples = self._data[token_id]
        # Evict from the front (oldest)
        while samples and samples[0].ts < cutoff:
            samples.pop(0)

    def _compute_twap(self, samples: list[_PriceSample]) -> float | None:
        """Time-weighted average over the sample list.

        Uses trapezoidal weighting: each sample's weight is the time gap to the
        *next* sample (or 1 second for the last sample so it always contributes).
        Falls back to simple mean when all timestamps are identical.
        """
        if not samples:
            return None
        if len(samples) == 1:
            return samples[0].price

        total_weight = 0.0
        weighted_sum = 0.0
        for i, s in enumerate(samples):
            next_ts = samples[i + 1].ts if i + 1 < len(samples) else s.ts + 1.0
            weight = max(next_ts - s.ts, 0.0) + 1.0  # +1 so every point counts
            weighted_sum += s.price * weight
            total_weight += weight

        return weighted_sum / total_weight if total_weight > 0 else samples[-1].price
