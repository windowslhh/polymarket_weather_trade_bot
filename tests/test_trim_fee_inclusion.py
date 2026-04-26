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

    With price=0.80 and win_prob=0.50 (post-FIX-2P-2 fee = 5%):
        raw_ev   = 0.5*(1-0.8) - 0.5*0.8         = -0.30
        fee_adj  = -0.05*0.8*(1-0.8)             = -0.008
        net_ev   = -0.308

    The config's min_trim_ev_absolute = 0.30.  With the pre-FIX-14
    formula (no fee), ev = -0.30; -0.30 < -0.30 is False → no TRIM.
    With FIX-14 applied, ev = -0.308; -0.308 < -0.30 is True → TRIM.
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


def test_fee_formula_matches_post_2026_03_30_rollout():
    """FIX-2P-2: pin the post-rollout 5% fee formula.

    Polymarket's 2026-03-30 rollout set Weather at 5% taker, with the
    canonical formula ``fee_per_dollar = rate * p * (1 - p)`` (no ×2
    factor).  Pre-fix used 1.25% AND ×2 → roughly half the true fee,
    overstating backtest PnL and squeezing the LOCKED_WIN ev>0 safety
    net to where 1-tick paper→live slippage flipped EV negative.
    """
    from src.strategy.gates import TAKER_FEE_RATE

    assert TAKER_FEE_RATE == 0.05
    # peak at p=0.5: 5% * 0.25 = 0.0125 = 1.25 cents per dollar
    assert abs(entry_fee_per_dollar(0.5) - 0.0125) < 1e-9
    # at LOCKED_WIN cap p=0.95: 5% * 0.95 * 0.05 = 0.002375
    assert abs(entry_fee_per_dollar(0.95) - 0.002375) < 1e-9
