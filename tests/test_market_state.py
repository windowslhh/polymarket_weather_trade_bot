"""Tests for ``src.strategy.market_state.classify_market``.

Covers each MarketState branch end-to-end so a regression in the
detection priority (Gamma `closed` → outcomePrices rails → price
heuristic → UNKNOWN) flips a test rather than silently letting a
RESOLVED slot back into the strategy hot path.
"""
from __future__ import annotations

import pytest

from src.strategy.market_state import (
    MarketState,
    STATE_REJECT_REASONS,
    classify_market,
)


# ── Cheap path: no Gamma data → price heuristic / UNKNOWN ─────────────

def test_no_gamma_data_low_price_unknown():
    """No data, mid-book price → can't tell anything; UNKNOWN."""
    assert classify_market("tok", None, 0.50) is MarketState.UNKNOWN


def test_no_gamma_data_rail_price_winner():
    """No data, price at NO rail (0.99+) → assume bot's NO won.

    Settler will re-confirm on-chain before redeeming, so the
    optimistic classification is safe — false positives only delay
    re-classification by one cycle.
    """
    assert classify_market("tok", None, 0.999) is MarketState.RESOLVED_WINNER
    assert classify_market("tok", None, 0.99) is MarketState.RESOLVED_WINNER


def test_no_gamma_data_almost_rail_still_unknown():
    """0.989 is below the 0.99 threshold → UNKNOWN, not WINNER."""
    assert classify_market("tok", None, 0.989) is MarketState.UNKNOWN


# ── Primary path: Gamma `closed` field ────────────────────────────────

def test_open_market():
    gamma = {"closed": False, "outcomePrices": ["0.6", "0.4"]}
    assert classify_market("tok", gamma, 0.40) is MarketState.OPEN


def test_open_market_missing_closed_defaults_open():
    """Gamma payload without `closed` key → treat as open (safe default).

    Gamma occasionally omits the key in transient states; we don't want
    a missing field to read as "winner" and skip a tradeable slot.
    """
    gamma = {"outcomePrices": ["0.6", "0.4"]}
    assert classify_market("tok", gamma, 0.40) is MarketState.OPEN


def test_resolved_no_winner():
    """closed=true, NO at the rail → bot wins."""
    gamma = {"closed": True, "outcomePrices": ["0", "1"]}
    assert classify_market("tok", gamma, 0.999) is MarketState.RESOLVED_WINNER


def test_resolved_yes_winner_we_lose():
    """closed=true, YES at the rail → our NO is worthless."""
    gamma = {"closed": True, "outcomePrices": ["1", "0"]}
    assert classify_market("tok", gamma, 0.001) is MarketState.RESOLVED_LOSER


def test_resolving_neither_rail():
    """closed=true but neither side at the rail → mid-dispute window."""
    gamma = {"closed": True, "outcomePrices": ["0.5", "0.5"]}
    assert classify_market("tok", gamma, 0.50) is MarketState.RESOLVING


def test_resolving_unparseable_outcome_prices():
    """closed=true with junk outcomePrices → RESOLVING (safe — caller skips)."""
    gamma = {"closed": True, "outcomePrices": "garbage-not-json"}
    assert classify_market("tok", gamma, 0.50) is MarketState.RESOLVING


def test_resolving_missing_outcome_prices():
    """closed=true without outcomePrices at all → RESOLVING."""
    gamma = {"closed": True}
    assert classify_market("tok", gamma, 0.50) is MarketState.RESOLVING


def test_resolving_short_outcome_prices():
    """closed=true with only one outcome price → RESOLVING."""
    gamma = {"closed": True, "outcomePrices": ["1"]}
    assert classify_market("tok", gamma, 0.50) is MarketState.RESOLVING


# ── outcomePrices comes back as a JSON-encoded string ─────────────────

def test_outcome_prices_as_json_string_no_winner():
    """Gamma sometimes serialises outcomePrices as a JSON-encoded string;
    the classifier must json.loads transparently."""
    gamma = {"closed": True, "outcomePrices": '["0", "1"]'}
    assert classify_market("tok", gamma, 0.999) is MarketState.RESOLVED_WINNER


def test_outcome_prices_as_json_string_yes_winner():
    gamma = {"closed": True, "outcomePrices": '["1.0", "0.0"]'}
    assert classify_market("tok", gamma, 0.001) is MarketState.RESOLVED_LOSER


# ── Reason codes are exhaustive ───────────────────────────────────────

def test_reason_codes_cover_all_non_open_states():
    """Every non-OPEN state has a decision_log REJECT reason code so the
    evaluator's _check_market_state path can append a row.  A new state
    added without a code would silently skip auditing — guard against it.
    """
    non_open = {s for s in MarketState if s is not MarketState.OPEN}
    assert set(STATE_REJECT_REASONS.keys()) == non_open
    # Codes must be uppercase snake (the existing decision_log convention).
    for code in STATE_REJECT_REASONS.values():
        assert code == code.upper()
        assert " " not in code
