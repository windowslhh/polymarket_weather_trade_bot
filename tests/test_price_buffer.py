"""Tests for the TWAP price buffer with outlier filtering and cross-validation."""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.markets.price_buffer import (
    CLOB_GAMMA_DIVERGENCE_THRESHOLD,
    OUTLIER_MIN_ABSOLUTE,
    OUTLIER_THRESHOLD,
    TWAP_MIN_SAMPLES,
    TWAP_WINDOW_SECONDS,
    PriceBuffer,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts(offset_seconds: float = 0.0) -> float:
    """Fixed base timestamp + offset for deterministic tests."""
    return 1_700_000_000.0 + offset_seconds


# ── 1. Basic TWAP mechanics ───────────────────────────────────────────────────

class TestTWAPBasics:

    def test_single_sample_returns_raw_price(self):
        buf = PriceBuffer()
        result = buf.update("tok", 0.70, now=_ts(0))
        assert result == 0.70

    def test_two_samples_returns_twap_not_raw(self):
        buf = PriceBuffer()
        buf.update("tok", 0.60, now=_ts(0))
        result = buf.update("tok", 0.80, now=_ts(60))
        # TWAP must be between the two prices
        assert 0.60 < result < 0.80

    def test_three_identical_samples_twap_equals_price(self):
        buf = PriceBuffer()
        for i in range(3):
            result = buf.update("tok", 0.75, now=_ts(i * 30))
        assert abs(result - 0.75) < 1e-9

    def test_twap_closer_to_longer_held_price(self):
        """Price held for 240s then jumps: TWAP should be close to the held price."""
        buf = PriceBuffer()
        # Hold 0.60 for 240 seconds
        buf.update("tok", 0.60, now=_ts(0))
        buf.update("tok", 0.60, now=_ts(120))
        buf.update("tok", 0.60, now=_ts(240))
        # Sudden jump at 241s
        result = buf.update("tok", 0.90, now=_ts(241))
        # TWAP should still be much closer to 0.60 than 0.90
        assert result < 0.70, f"TWAP {result:.4f} should be near 0.60 after brief spike"

    def test_get_twap_none_when_empty(self):
        buf = PriceBuffer()
        assert buf.get_twap("nonexistent") is None

    def test_get_twap_matches_last_update(self):
        buf = PriceBuffer()
        buf.update("tok", 0.70, now=_ts(0))
        buf.update("tok", 0.72, now=_ts(60))
        twap_after = buf.get_twap("tok", now=_ts(60))
        assert twap_after is not None
        assert 0.70 <= twap_after <= 0.72

    def test_independent_tokens_do_not_interfere(self):
        buf = PriceBuffer()
        buf.update("tok_a", 0.50, now=_ts(0))
        buf.update("tok_b", 0.90, now=_ts(0))
        assert buf.get_twap("tok_a", now=_ts(0)) == 0.50
        assert buf.get_twap("tok_b", now=_ts(0)) == 0.90


# ── 2. Outlier filtering ──────────────────────────────────────────────────────

class TestOutlierFiltering:

    def test_large_spike_discarded_returns_existing_twap(self):
        """A price >10% from TWAP is discarded; TWAP returned unchanged."""
        buf = PriceBuffer(outlier_threshold=0.10, min_samples=2)
        buf.update("tok", 0.70, now=_ts(0))
        pre_twap = buf.update("tok", 0.70, now=_ts(60))  # establishes TWAP ≈ 0.70

        # 50% spike — far above threshold
        result = buf.update("tok", 1.05, now=_ts(120))
        assert abs(result - pre_twap) < 1e-9, "Spike should be discarded"

    def test_small_move_within_threshold_accepted(self):
        """A price within 10% of TWAP is accepted normally."""
        buf = PriceBuffer(outlier_threshold=0.10, min_samples=2)
        buf.update("tok", 0.70, now=_ts(0))
        buf.update("tok", 0.70, now=_ts(60))

        # 5% move — within threshold
        result = buf.update("tok", 0.735, now=_ts(120))
        # Result should be between 0.70 and 0.735 (accepted, shifts TWAP)
        assert 0.70 <= result <= 0.735

    def test_outlier_not_inserted_into_buffer(self):
        """After discarding an outlier, sample count should not increase."""
        buf = PriceBuffer(outlier_threshold=0.10, min_samples=2)
        buf.update("tok", 0.70, now=_ts(0))
        buf.update("tok", 0.70, now=_ts(60))
        count_before = buf.sample_counts().get("tok", 0)

        buf.update("tok", 1.50, now=_ts(120))  # huge outlier
        count_after = buf.sample_counts().get("tok", 0)
        assert count_after == count_before, "Outlier must not be added to buffer"

    def test_first_two_samples_bypass_outlier_check(self):
        """With fewer than min_samples, all prices are accepted raw."""
        buf = PriceBuffer(outlier_threshold=0.10, min_samples=3)
        # Only 2 samples → window too thin → no outlier check
        buf.update("tok", 0.70, now=_ts(0))
        result = buf.update("tok", 1.50, now=_ts(1))  # 114% above — but window thin
        # Both accepted (min_samples=3, only 2 so far)
        assert result is not None  # didn't crash; result is the TWAP of accepted samples

    def test_downward_spike_also_filtered(self):
        """A crash in price (e.g. 0.70 → 0.30) is also an outlier."""
        buf = PriceBuffer(outlier_threshold=0.10, min_samples=2)
        buf.update("tok", 0.70, now=_ts(0))
        pre = buf.update("tok", 0.70, now=_ts(60))

        result = buf.update("tok", 0.30, now=_ts(120))  # -57% → outlier
        assert abs(result - pre) < 1e-9

    def test_low_price_small_absolute_move_accepted(self):
        """For a low-price token, a large % move that is tiny in absolute terms
        must be ACCEPTED (hybrid threshold requires both % and absolute to trigger).

        TWAP=0.10, new=0.12: +20% but only $0.02 absolute < $0.05 floor → accept.
        """
        buf = PriceBuffer(outlier_threshold=0.10, min_samples=2, min_absolute=0.05)
        buf.update("tok", 0.10, now=_ts(0))
        buf.update("tok", 0.10, now=_ts(60))

        result = buf.update("tok", 0.12, now=_ts(120))  # 20% but $0.02 abs
        # Must be accepted — should shift TWAP toward 0.12
        assert result > 0.10, "Small absolute move on low-price token must be accepted"

    def test_low_price_large_absolute_move_discarded(self):
        """For a low-price token, a move exceeding BOTH % and absolute floor IS discarded.

        TWAP=0.10, new=0.17: +70% and $0.07 absolute > $0.05 floor → discard.
        """
        buf = PriceBuffer(outlier_threshold=0.10, min_samples=2, min_absolute=0.05)
        buf.update("tok", 0.10, now=_ts(0))
        pre = buf.update("tok", 0.10, now=_ts(60))

        result = buf.update("tok", 0.17, now=_ts(120))  # 70% and $0.07 abs
        assert abs(result - pre) < 1e-9, "Move exceeding both thresholds must be discarded"

    def test_high_price_large_pct_and_absolute_discarded(self):
        """For a mid/high-price token, large % + large absolute both trigger rejection.

        TWAP=0.70, new=0.85: 21.4% and $0.15 → discarded.
        """
        buf = PriceBuffer(outlier_threshold=0.10, min_samples=2, min_absolute=0.05)
        buf.update("tok", 0.70, now=_ts(0))
        pre = buf.update("tok", 0.70, now=_ts(60))

        result = buf.update("tok", 0.85, now=_ts(120))
        assert abs(result - pre) < 1e-9


# ── 3. Window expiry ──────────────────────────────────────────────────────────

class TestWindowExpiry:

    def test_samples_expire_after_window(self):
        buf = PriceBuffer(window_seconds=60)
        buf.update("tok", 0.70, now=_ts(0))
        buf.update("tok", 0.70, now=_ts(30))

        # All samples now older than 60s → window empty
        result = buf.get_twap("tok", now=_ts(120))
        assert result is None

    def test_only_fresh_samples_used_in_twap(self):
        buf = PriceBuffer(window_seconds=60, outlier_threshold=1.0)  # no outlier filter
        buf.update("tok", 0.50, now=_ts(0))    # will expire
        buf.update("tok", 0.80, now=_ts(100))  # within window of ts=120

        # At ts=120, only the 0.80 sample is fresh (0.50 expired at ts=60)
        twap = buf.get_twap("tok", now=_ts(120))
        assert twap == 0.80

    def test_max_samples_cap_evicts_oldest(self):
        buf = PriceBuffer(window_seconds=10000, max_samples=3, outlier_threshold=1.0)
        buf.update("tok", 0.60, now=_ts(0))
        buf.update("tok", 0.70, now=_ts(1))
        buf.update("tok", 0.80, now=_ts(2))
        buf.update("tok", 0.90, now=_ts(3))  # evicts 0.60

        twap = buf.get_twap("tok", now=_ts(3))
        assert twap is not None
        # TWAP should not equal 0.60 (it was evicted)
        assert abs(twap - 0.60) > 0.01


# ── 4. apply_batch ────────────────────────────────────────────────────────────

class TestApplyBatch:

    def test_apply_batch_returns_all_tokens(self):
        buf = PriceBuffer()
        prices = {"tok_a": 0.70, "tok_b": 0.85, "tok_c": 0.30}
        result = buf.apply_batch(prices, now=_ts(0))
        assert set(result.keys()) == set(prices.keys())

    def test_apply_batch_single_call_returns_raw(self):
        buf = PriceBuffer()
        result = buf.apply_batch({"tok": 0.75}, now=_ts(0))
        assert result["tok"] == 0.75

    def test_apply_batch_second_call_smooths(self):
        buf = PriceBuffer(outlier_threshold=1.0)  # no outlier filter
        buf.apply_batch({"tok": 0.70}, now=_ts(0))
        result = buf.apply_batch({"tok": 0.80}, now=_ts(60))
        assert 0.70 < result["tok"] < 0.80  # TWAP between the two


# ── 5. CLOB/Gamma cross-validation ───────────────────────────────────────────

class TestCrossValidation:

    def test_no_clob_returns_gamma(self):
        buf = PriceBuffer()
        merged = buf.cross_validate({}, {"tok": 0.70})
        assert merged == {"tok": 0.70}

    def test_no_gamma_returns_clob(self):
        buf = PriceBuffer()
        merged = buf.cross_validate({"tok": 0.72}, {})
        assert merged == {"tok": 0.72}

    def test_agreement_within_threshold_uses_clob(self):
        """CLOB 0.72 vs Gamma 0.70: divergence 2.9% < 5% → use CLOB."""
        buf = PriceBuffer()
        merged = buf.cross_validate({"tok": 0.72}, {"tok": 0.70}, threshold=0.05)
        assert merged["tok"] == 0.72

    def test_divergence_beyond_threshold_uses_gamma(self):
        """CLOB 0.80 vs Gamma 0.70: divergence 14.3% > 5% → fall back to Gamma."""
        buf = PriceBuffer()
        merged = buf.cross_validate({"tok": 0.80}, {"tok": 0.70}, threshold=0.05)
        assert merged["tok"] == 0.70

    def test_divergence_below_threshold_uses_clob(self):
        """Divergence < threshold: CLOB wins."""
        buf = PriceBuffer()
        # 4% divergence: clearly below 5% threshold → CLOB wins
        merged = buf.cross_validate({"tok": 0.728}, {"tok": 0.70}, threshold=0.05)
        assert merged["tok"] == 0.728

    def test_mixed_tokens_handled_correctly(self):
        """Some tokens agree, one diverges — diverging one gets Gamma."""
        buf = PriceBuffer()
        clob = {"tok_a": 0.71, "tok_b": 0.90}
        gamma = {"tok_a": 0.70, "tok_b": 0.70}
        merged = buf.cross_validate(clob, gamma, threshold=0.05)
        assert merged["tok_a"] == 0.71   # agree → CLOB
        assert merged["tok_b"] == 0.70   # diverge → Gamma

    def test_only_in_clob_no_gamma_entry(self):
        buf = PriceBuffer()
        merged = buf.cross_validate({"tok_x": 0.65}, {})
        assert merged["tok_x"] == 0.65

    def test_only_in_gamma_no_clob_entry(self):
        buf = PriceBuffer()
        merged = buf.cross_validate({}, {"tok_y": 0.55})
        assert merged["tok_y"] == 0.55

    def test_zero_gamma_price_does_not_divide_by_zero(self):
        """If Gamma price is 0.0, divergence calculation must not blow up."""
        buf = PriceBuffer()
        # Should not raise
        merged = buf.cross_validate({"tok": 0.50}, {"tok": 0.0}, threshold=0.05)
        # When gamma=0, divergence = 0 (guarded), so CLOB wins
        assert merged["tok"] == 0.50

    def test_cross_validate_logs_divergence(self, caplog):
        import logging
        buf = PriceBuffer()
        with caplog.at_level(logging.WARNING, logger="src.markets.price_buffer"):
            buf.cross_validate({"tok": 0.90}, {"tok": 0.70}, threshold=0.05)
        assert any("divergence" in r.message.lower() for r in caplog.records)


# ── 6. sample_counts diagnostics ─────────────────────────────────────────────

class TestSampleCounts:

    def test_empty_buffer_returns_empty_dict(self):
        buf = PriceBuffer()
        assert buf.sample_counts() == {}

    def test_counts_reflect_live_samples(self):
        buf = PriceBuffer(window_seconds=300, outlier_threshold=1.0)
        buf.update("tok_a", 0.70, now=_ts(0))
        buf.update("tok_a", 0.72, now=_ts(60))
        buf.update("tok_b", 0.85, now=_ts(0))
        counts = buf.sample_counts(now=_ts(60))  # pass same base time to avoid real-clock expiry
        assert counts["tok_a"] == 2
        assert counts["tok_b"] == 1

    def test_expired_samples_not_counted(self):
        buf = PriceBuffer(window_seconds=60)
        buf.update("tok", 0.70, now=_ts(0))
        # At ts=200, the sample at ts=0 is 200s old — beyond 60s window
        buf._evict("tok", _ts(200))
        counts = buf.sample_counts(now=_ts(200))
        assert counts.get("tok", 0) == 0
