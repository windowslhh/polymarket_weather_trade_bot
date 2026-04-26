"""BUG-3: closed=True but outcomePrices still mid → defer.

Polymarket flips event.closed before its oracle finishes propagating
the per-child-market outcomePrices.  In that small window every slot
reads its last mid-price (e.g. 0.62 / 0.31), not 0/1.  Pre-fix the
settler would label winning_slot='none', mark every NO position as
exit_price=0 or 1 based on the mid (wrong!), and book a phantom PnL.
Fix: when no slot ≥ 0.99 AND not every outcomePrice is exactly 0 or 1,
return None and try again next cycle.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import pytest

from src.settlement.settler import _fetch_settlement_outcome


def _stub_event(closed: bool, outcomes_per_market):
    """Build a minimal Gamma event payload."""
    markets = []
    for q, oc_prices in outcomes_per_market:
        markets.append({
            "question": q,
            "outcomePrices": json.dumps([str(oc_prices[0]), str(oc_prices[1])]),
            "clobTokenIds": json.dumps([f"yes_{q}", f"no_{q}"]),
        })
    return {"id": "ev-test", "closed": closed, "markets": markets}


class _StubClient:
    def __init__(self, event_payload):
        self._event = event_payload

    async def get(self, url):
        class _Resp:
            status_code = 200
            event = self._event

            def raise_for_status(self_):
                pass

            def json(self_):
                return _Resp.event

        _Resp.event = self._event
        return _Resp()


@pytest.mark.asyncio
async def test_outcome_returned_when_real_winner_resolves():
    """Sanity: a clean closed=True with one slot at 1.0 returns winner."""
    payload = _stub_event(closed=True, outcomes_per_market=[
        ("80-81°F?", (1.0, 0.0)),  # YES = 1.0 → winner
        ("78-79°F?", (0.0, 1.0)),
    ])
    out = await _fetch_settlement_outcome(_StubClient(payload), "ev-test")
    assert out is not None
    assert out.winning_slot == "80-81°F?"


@pytest.mark.asyncio
async def test_defers_when_closed_true_but_prices_still_mid(caplog):
    """BUG-3: closed=True + mid-prices → return None (defer to next cycle)."""
    payload = _stub_event(closed=True, outcomes_per_market=[
        ("80-81°F?", (0.62, 0.38)),  # mid — oracle not propagated yet
        ("78-79°F?", (0.31, 0.69)),
    ])
    caplog.set_level(logging.WARNING, logger="src.settlement.settler")
    out = await _fetch_settlement_outcome(_StubClient(payload), "ev-test")
    assert out is None, (
        "BUG-3: closed=True with mid-prices must defer (was incorrectly "
        "returning a 'none' winner that booked phantom PnL)"
    )
    assert any("propagating" in r.message for r in caplog.records), (
        "warning should mention prices still propagating"
    )


@pytest.mark.asyncio
async def test_defers_when_closed_true_and_one_slot_partially_resolved():
    """closed=True, one slot at 1.0 (good) but others at mid-prices.
    Legacy behaviour (still correct): the 1.0 slot is the winner; the
    mid-priced ones are 'NO wins' relative to it.  This case must NOT
    trigger the BUG-3 deferral because we DO have a winning_slot."""
    payload = _stub_event(closed=True, outcomes_per_market=[
        ("80-81°F?", (1.0, 0.0)),
        ("78-79°F?", (0.31, 0.69)),  # mid but harmless — 80-81 won
    ])
    out = await _fetch_settlement_outcome(_StubClient(payload), "ev-test")
    assert out is not None
    assert out.winning_slot == "80-81°F?"


@pytest.mark.asyncio
async def test_legacy_none_winner_path_when_all_prices_are_zero():
    """Genuine "no winner" path: all label_prices are exactly 0.0 (none
    reached 0.99).  This is extremely rare in practice but the existing
    code path handles it — guard must NOT trigger BUG-3 defer here."""
    payload = _stub_event(closed=True, outcomes_per_market=[
        ("80-81°F?", (0.0, 1.0)),
        ("78-79°F?", (0.0, 1.0)),
    ])
    out = await _fetch_settlement_outcome(_StubClient(payload), "ev-test")
    assert out is not None
    assert out.winning_slot == "none"


@pytest.mark.asyncio
async def test_closed_false_returns_none_no_warning(caplog):
    """closed=False is normal (event not yet resolved) — no log spam."""
    payload = _stub_event(closed=False, outcomes_per_market=[
        ("80-81°F?", (0.5, 0.5)),
    ])
    caplog.set_level(logging.WARNING, logger="src.settlement.settler")
    out = await _fetch_settlement_outcome(_StubClient(payload), "ev-test")
    assert out is None
    # No mid-price warning fires when closed=False
    assert not any("propagating" in r.message for r in caplog.records)
