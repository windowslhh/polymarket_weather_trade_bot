"""Per-market settlement: closed status on a single child market triggers
settlement of just that position, not the whole event.

Pre-Phase 3 the settler walked positions only after the parent *event*
flipped ``closed=true`` — locked-win positions stayed open for hours
after their slot resolved.  This file pins the new behaviour: a child
market's individual ``closed=true`` is enough to settle the held NO,
and other still-open markets in the same event are not touched.

Gamma's HTTP layer is mocked end-to-end so this suite never touches
the real API.  The redeemer is left None — paper-mode semantics —
which means winners get marked settled in the DB without any on-chain
call.  ``test_settler_redeem_flow.py`` covers the redeemer-injected path.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.portfolio.store import Store
from src.settlement.settler import check_settlements


async def _mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    return store


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict):
        self.status_code = status_code
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _FakeClient:
    """Shim covering ``httpx.AsyncClient.get`` — keyed by event_id in URL."""

    def __init__(self, events: dict[str, dict]):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, *args, **kwargs):
        # /events/<id>
        eid = url.rstrip("/").split("/")[-1]
        data = self._events.get(eid)
        if data is None:
            return _FakeResponse(404, {})
        return _FakeResponse(200, data)


def _gamma_event(markets: list[dict]) -> dict:
    return {"id": "evt", "markets": markets}


def _gamma_market(
    *, condition_id: str, token_id_no: str, closed: bool,
    yes_price: float, no_price: float,
) -> dict:
    return {
        "conditionId": condition_id,
        "closed": closed,
        # outcomePrices arrives as a JSON-encoded string from real Gamma.
        "outcomePrices": json.dumps([str(yes_price), str(no_price)]),
        "clobTokenIds": json.dumps(["yes_" + token_id_no, token_id_no]),
    }


@pytest.mark.asyncio
async def test_per_market_closed_settles_only_that_position():
    """Two positions on the same event; only one market is closed."""
    store = await _mk_store()
    try:
        # Position A — slot already closed (NO won, yes_price=0)
        await store.insert_position(
            event_id="evt", token_id="tok_closed", token_type="NO", city="Chicago",
            slot_label="80°F to 84°F", side="BUY", entry_price=0.40,
            size_usd=4.0, shares=10.0, strategy="D", buy_reason="seed",
        )
        # Position B — different slot, still open
        await store.insert_position(
            event_id="evt", token_id="tok_open", token_type="NO", city="Chicago",
            slot_label="85°F to 89°F", side="BUY", entry_price=0.30,
            size_usd=3.0, shares=10.0, strategy="D", buy_reason="seed",
        )

        events = {"evt": _gamma_event([
            _gamma_market(
                condition_id="0xc1", token_id_no="tok_closed", closed=True,
                yes_price=0.0, no_price=1.0,
            ),
            _gamma_market(
                condition_id="0xc2", token_id_no="tok_open", closed=False,
                yes_price=0.55, no_price=0.45,
            ),
        ])}

        with patch("src.settlement.settler.httpx.AsyncClient",
                   return_value=_FakeClient(events)):
            results = await check_settlements(store, redeemer=None, alerter=None)

        # Exactly one position settled (the closed one); other untouched.
        rows = await store.get_open_positions()
        assert len(rows) == 1
        assert rows[0]["token_id"] == "tok_open"

        async with store.db.execute(
            "SELECT token_id, status, exit_price, realized_pnl "
            "FROM positions WHERE token_id = 'tok_closed'"
        ) as cur:
            settled = [dict(r) for r in await cur.fetchall()]
        assert settled[0]["status"] == "settled"
        assert settled[0]["exit_price"] == pytest.approx(1.0)
        # NO won → P&L = (1 - 0.40) × 10 = 6.0
        assert settled[0]["realized_pnl"] == pytest.approx(6.0)

        assert len(results) == 1
        assert results[0].positions_settled == 1
        assert results[0].total_pnl == pytest.approx(6.0)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_resolved_loser_settles_at_zero():
    """closed=true with YES at the rail → bot's NO loses, exit_price=0."""
    store = await _mk_store()
    try:
        await store.insert_position(
            event_id="evt2", token_id="tok_lose", token_type="NO", city="NYC",
            slot_label="80°F to 84°F", side="BUY", entry_price=0.40,
            size_usd=4.0, shares=10.0, strategy="D", buy_reason="seed",
        )
        events = {"evt2": _gamma_event([
            _gamma_market(
                condition_id="0xcl", token_id_no="tok_lose", closed=True,
                yes_price=1.0, no_price=0.0,
            ),
        ])}

        with patch("src.settlement.settler.httpx.AsyncClient",
                   return_value=_FakeClient(events)):
            results = await check_settlements(store, redeemer=None, alerter=None)

        async with store.db.execute(
            "SELECT status, exit_price, realized_pnl FROM positions"
        ) as cur:
            row = dict((await cur.fetchall())[0])
        assert row["status"] == "settled"
        assert row["exit_price"] == pytest.approx(0.0)
        # NO lost → P&L = -0.40 × 10 = -4.0
        assert row["realized_pnl"] == pytest.approx(-4.0)
        assert results[0].total_pnl == pytest.approx(-4.0)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_resolving_skips_until_finality():
    """closed=true but neither side at the rail (mid-dispute) → skip."""
    store = await _mk_store()
    try:
        await store.insert_position(
            event_id="evt3", token_id="tok_pending", token_type="NO", city="NYC",
            slot_label="80°F to 84°F", side="BUY", entry_price=0.40,
            size_usd=4.0, shares=10.0, strategy="D", buy_reason="seed",
        )
        events = {"evt3": _gamma_event([
            _gamma_market(
                condition_id="0xpg", token_id_no="tok_pending", closed=True,
                yes_price=0.55, no_price=0.45,
            ),
        ])}

        with patch("src.settlement.settler.httpx.AsyncClient",
                   return_value=_FakeClient(events)):
            results = await check_settlements(store, redeemer=None, alerter=None)

        rows = await store.get_open_positions()
        assert len(rows) == 1, "RESOLVING must not auto-settle the position"
        assert results == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_event_404_skips_silently():
    """Gamma 404 should not raise; bot retries next cycle."""
    store = await _mk_store()
    try:
        await store.insert_position(
            event_id="evt_missing", token_id="tok", token_type="NO", city="NYC",
            slot_label="80°F to 84°F", side="BUY", entry_price=0.40,
            size_usd=4.0, shares=10.0, strategy="D", buy_reason="seed",
        )
        with patch("src.settlement.settler.httpx.AsyncClient",
                   return_value=_FakeClient({})):
            results = await check_settlements(store, redeemer=None, alerter=None)
        rows = await store.get_open_positions()
        assert len(rows) == 1
        assert results == []
    finally:
        await store.close()
