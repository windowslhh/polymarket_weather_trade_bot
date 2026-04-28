"""Phase 4: market-state pre-gate routing in the evaluator entry points.

A held NO whose market has flipped to ``closed=true`` (resolution
arrived) must NOT produce a SELL via TRIM / EXIT, and the entry
scanner must NOT issue a BUY against a closed slot — Polymarket
rejects both.  These tests pin the routing behaviour so a future
refactor of evaluator.py / gates.py / market_state.py can't silently
re-enable the rejected-order retry storm Phase 4 was deployed to fix.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.config import StrategyConfig
from src.markets.models import TempSlot, WeatherMarketEvent
from src.strategy.evaluator import (
    evaluate_exit_signals,
    evaluate_locked_win_signals,
    evaluate_no_signals,
    evaluate_trim_signals,
    reset_state_reject_dedup,
)
from src.strategy.market_state import MarketState
from src.weather.models import Forecast, Observation


def _slot(price_no: float = 0.50, tid: str = "tok_no") -> TempSlot:
    return TempSlot(
        token_id_yes="tok_yes", token_id_no=tid,
        outcome_label="80°F to 84°F",
        temp_lower_f=80.0, temp_upper_f=84.0,
        price_yes=1.0 - price_no, price_no=price_no,
    )


def _event(slots: list[TempSlot], market_date: date | None = None) -> WeatherMarketEvent:
    return WeatherMarketEvent(
        event_id="evt_state", condition_id="cond",
        city="Chicago",
        market_date=market_date or date.today(),
        slots=slots,
    )


def _forecast(market_date: date | None = None) -> Forecast:
    return Forecast(
        city="Chicago",
        forecast_date=market_date or date.today(),
        predicted_high_f=82.0, predicted_low_f=68.0,
        confidence_interval_f=4.0,
        source="test", fetched_at=datetime.now(timezone.utc),
    )


@pytest.fixture(autouse=True)
def _clear_dedup():
    reset_state_reject_dedup()
    yield
    reset_state_reject_dedup()


# ── BUY entry: evaluate_no_signals ────────────────────────────────────

def test_no_signals_skips_resolved_winner():
    today = date.today()
    slot = _slot(price_no=0.40)
    event = _event([slot], market_date=today)
    forecast = _forecast(market_date=today)
    cfg = StrategyConfig()

    states = {slot.token_id_no: MarketState.RESOLVED_WINNER}
    rejects: list[dict] = []
    signals = evaluate_no_signals(
        event, forecast, cfg, market_states=states, rejects=rejects,
    )
    assert signals == []
    assert rejects, "rejects sink should record the state-reject row"
    assert rejects[0]["reason"] == "MARKET_RESOLVED_WINNER_AWAIT_REDEEM"


def test_no_signals_skips_resolving():
    today = date.today()
    slot = _slot(price_no=0.40)
    event = _event([slot], market_date=today)
    states = {slot.token_id_no: MarketState.RESOLVING}
    signals = evaluate_no_signals(
        event, _forecast(market_date=today), StrategyConfig(),
        market_states=states,
    )
    assert signals == []


def test_no_signals_unknown_state_skips():
    """No Gamma data → UNKNOWN → defensive skip (do not trade blind)."""
    today = date.today()
    slot = _slot(price_no=0.40)
    event = _event([slot], market_date=today)
    states = {slot.token_id_no: MarketState.UNKNOWN}
    signals = evaluate_no_signals(
        event, _forecast(market_date=today), StrategyConfig(),
        market_states=states,
    )
    assert signals == []


def test_no_signals_no_states_map_treats_as_open():
    """Legacy callers pass market_states=None → no behaviour change."""
    today = date.today()
    slot = _slot(price_no=0.40)
    event = _event([slot], market_date=today)
    # Don't assert on signals being non-empty — entry gates may reject for
    # other reasons (EV, distance).  Assert merely that the call doesn't
    # raise and the state filter doesn't preempt the gate matrix.
    out = evaluate_no_signals(
        event, _forecast(market_date=today), StrategyConfig(),
        market_states=None,
    )
    assert isinstance(out, list)


# ── EXIT path: evaluate_exit_signals ──────────────────────────────────

def test_exit_signals_skips_resolved_winner():
    """A locked-in winner shouldn't get a SELL — settler will redeem."""
    today = date.today()
    slot = _slot(price_no=0.998)  # rail price typical of a closed-NO
    event = _event([slot], market_date=today)
    obs = Observation(icao="KORD", temp_f=85.0,
                      observation_time=datetime.now(timezone.utc))
    states = {slot.token_id_no: MarketState.RESOLVED_WINNER}

    signals = evaluate_exit_signals(
        event, obs, daily_max_f=85.0, held_no_slots=[slot],
        config=StrategyConfig(), forecast=_forecast(market_date=today),
        market_states=states,
    )
    assert signals == []


def test_exit_signals_skips_resolved_loser():
    today = date.today()
    slot = _slot(price_no=0.001, tid="tok_lose")
    event = _event([slot], market_date=today)
    obs = Observation(icao="KORD", temp_f=85.0,
                      observation_time=datetime.now(timezone.utc))
    states = {slot.token_id_no: MarketState.RESOLVED_LOSER}

    signals = evaluate_exit_signals(
        event, obs, daily_max_f=85.0, held_no_slots=[slot],
        config=StrategyConfig(), forecast=_forecast(market_date=today),
        market_states=states,
    )
    assert signals == []


# ── TRIM path: evaluate_trim_signals ──────────────────────────────────

def test_trim_signals_skips_resolved_winner():
    today = date.today()
    slot = _slot(price_no=0.998, tid="tok_winner_trim")
    event = _event([slot], market_date=today)
    states = {slot.token_id_no: MarketState.RESOLVED_WINNER}

    signals = evaluate_trim_signals(
        event, _forecast(market_date=today), held_no_slots=[slot],
        config=StrategyConfig(),
        entry_prices={slot.token_id_no: 0.40},
        entry_ev_map={slot.token_id_no: 0.10},
        daily_max_f=85.0,
        market_states=states,
    )
    assert signals == []


def test_trim_signals_skips_resolving():
    """RESOLVING (closed but ambiguous) → wait for finality."""
    today = date.today()
    slot = _slot(price_no=0.50, tid="tok_resolv")
    event = _event([slot], market_date=today)
    states = {slot.token_id_no: MarketState.RESOLVING}

    signals = evaluate_trim_signals(
        event, _forecast(market_date=today), held_no_slots=[slot],
        config=StrategyConfig(),
        entry_prices={slot.token_id_no: 0.40},
        entry_ev_map={slot.token_id_no: 0.05},
        daily_max_f=80.0,
        market_states=states,
    )
    assert signals == []


# ── Locked-win entry ──────────────────────────────────────────────────

def test_locked_win_skips_resolved_winner():
    """A held locked-win shouldn't trigger a *new* locked-win BUY for an
    already-resolved slot — the settler will redeem the existing position."""
    today = date.today()
    slot = _slot(price_no=0.85, tid="tok_locked")
    event = _event([slot], market_date=today)
    states = {slot.token_id_no: MarketState.RESOLVED_WINNER}

    signals = evaluate_locked_win_signals(
        event, daily_max_f=85.0, config=StrategyConfig(),
        held_token_ids=set(), days_ahead=0,
        market_states=states,
    )
    assert signals == []
