"""FIX-14: TRIM's EV must subtract the taker fee, same as the entry gate.

Without this fix, a held position's current EV looked ~2 * taker_fee
richer than it actually was, delaying the relative-decay gate from
firing and prolonging losing positions.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.config import StrategyConfig
from src.markets.models import TempSlot, WeatherMarketEvent
from src.strategy.evaluator import evaluate_trim_signals
from src.strategy.gates import entry_fee_per_dollar
from src.weather.models import Forecast


def _event():
    return WeatherMarketEvent(
        event_id="e1", condition_id="c1", city="NYC",
        market_date=date(2026, 4, 25), slots=[],
    )


def _forecast():
    return Forecast(
        "NYC", date(2026, 4, 25), 72.0, 60.0, 3.0, "test",
        datetime.now(timezone.utc),
    )


def _slot(price_no: float, token_no: str = "tn"):
    return TempSlot(
        token_id_yes="y", token_id_no=token_no,
        outcome_label="70°F to 74°F", temp_lower_f=70.0, temp_upper_f=74.0,
        price_no=price_no,
    )


def test_trim_ev_subtracts_taker_fee():
    """Make the absolute-EV gate fire exactly on the boundary so we can
    observe the taker-fee delta.

    With price=0.80 and win_prob=0.50:
        raw_ev   = 0.5*(1-0.8) - 0.5*0.8         = -0.30
        fee_adj  = -2*0.015*0.8*(1-0.8)          = -0.0048 (per gates.py constants)
        net_ev   = -0.3048

    The config's min_trim_ev_absolute = 0.30.  With the pre-FIX-14
    formula (no fee), ev = -0.30; -0.30 < -0.30 is False → no TRIM.
    With FIX-14 applied, ev = -0.3048; -0.3048 < -0.30 is True → TRIM.
    """
    slot = _slot(price_no=0.80, token_no="tn_trim")
    event = _event()
    cfg = StrategyConfig(
        min_trim_ev_absolute=0.30,     # precise boundary
        trim_ev_decay_ratio=0.99,      # effectively disables the relative gate
        trim_price_stop_ratio=1.5,     # disables the price-stop gate
    )

    # Rig win_prob ≈ 0.5 via a slot far enough from forecast that the
    # empirical/normal approximation lands near 0.5.  Our slot midpoint is
    # 72, forecast high is 72, confidence=3; distance 0.  Use a forecast
    # where the slot is EXACTLY at the forecast high so we split 50/50.
    forecast = Forecast(
        "NYC", date(2026, 4, 25), 72.0, 60.0, 3.0, "test",
        datetime.now(timezone.utc),
    )

    signals = evaluate_trim_signals(
        event, forecast, [slot], cfg,
        entry_prices={"tn_trim": 0.80}, daily_max_f=72.0,
    )
    # Confirm the fee correction made the difference.  If FIX-14 were
    # reverted, the TRIM would NOT fire here.
    assert len(signals) >= 1, (
        "FIX-14 regression: without taker-fee in TRIM EV, the absolute "
        "gate sits right on the boundary and fails to fire"
    )


def test_fee_helper_constant():
    """Sanity: the fee helper returns a strictly-positive value on any
    price in (0, 1) and 0 at the extremes."""
    assert entry_fee_per_dollar(0.5) > 0
    assert entry_fee_per_dollar(0.0) == 0
    assert entry_fee_per_dollar(1.0) == 0


# ──────────────────────────────────────────────────────────────────────
# Fix C (2026-04-28): TAKER_FEE_RATE calibration.  Pre-fix the constant
# was 0.0125, which underweighted the actual on-chain fee by exactly 2x
# (a single-side rate was being multiplied by 2 to reach a "round-trip"
# value).  Verified against on-chain Miami / Chicago trades:
#   Miami:   3.12 × 0.05 × 0.69 × 0.31 = $0.0334  (matches receipt)
#   Chicago: 2.48 × 0.05 × 0.76 × 0.24 = $0.0226  (matches receipt)
# So the prod-confirmed schedule is fee_per_dollar = 0.05 × p × (1-p),
# i.e. ``TAKER_FEE_RATE * 2 * p * (1 - p)`` with TAKER_FEE_RATE=0.025.
# ──────────────────────────────────────────────────────────────────────


def test_fee_helper_matches_onchain_calibration():
    """``entry_fee_per_dollar`` must collapse to ``0.05 × p × (1-p)`` —
    the per-share fee Polymarket actually deducts as taker.  Any drift
    here means ``EvThresholdGate`` is comparing against the wrong cost."""
    # Peak fee at 50/50 → 0.05 × 0.5 × 0.5 = 0.0125 per dollar.
    assert abs(entry_fee_per_dollar(0.5) - 0.0125) < 1e-12

    # Miami's ~0.69 NO entry → 0.05 × 0.69 × 0.31 ≈ 0.010695 per dollar.
    assert abs(entry_fee_per_dollar(0.69) - 0.010695) < 1e-9

    # Chicago's 0.76 NO entry → 0.05 × 0.76 × 0.24 = 0.00912 per dollar.
    assert abs(entry_fee_per_dollar(0.76) - 0.00912) < 1e-9

    # Endpoints reduce to zero (no fee on a guaranteed outcome).
    assert entry_fee_per_dollar(0.0) == 0.0
    assert entry_fee_per_dollar(1.0) == 0.0


def test_taker_fee_rate_constant_is_per_side():
    """Pin the per-side rate at 0.025 so a future revert (or a half-baked
    rename to "round-trip rate") gets caught here."""
    from src.strategy.gates import TAKER_FEE_RATE
    assert TAKER_FEE_RATE == 0.025, (
        "TAKER_FEE_RATE must be 0.025 (per-side).  See gates.py docstring "
        "and the 2026-04-28 calibration comment."
    )
