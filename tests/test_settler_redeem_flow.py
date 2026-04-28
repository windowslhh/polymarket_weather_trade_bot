"""End-to-end: settler detects per-market closed, calls redeemer, marks settled.

The Redeemer is a stand-in (see ``_FakeRedeemer``); the real ``check_settlements``
walks Gamma, classifies winners, claims the row atomically, dispatches
to the redeemer, and writes back tx hash + status.

Failure-retry test pins MAX_REDEEM_ATTEMPTS so a transient RPC failure
loops without immediately giving up — and verifies the alert fires
exactly once when the cap is reached.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.portfolio.store import Store
from src.settlement.redeemer import RedeemResult
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
    def __init__(self, events: dict[str, dict]):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, *args, **kwargs):
        eid = url.rstrip("/").split("/")[-1]
        data = self._events.get(eid)
        if data is None:
            return _FakeResponse(404, {})
        return _FakeResponse(200, data)


def _winner_event(token_id_no: str = "tok_winner") -> dict:
    """Gamma payload where one market is closed with NO at the rail."""
    return {"id": "evt_w", "markets": [{
        "conditionId": "0xc1",
        "closed": True,
        "outcomePrices": json.dumps(["0", "1"]),
        "clobTokenIds": json.dumps(["yes_x", token_id_no]),
    }]}


class _FakeRedeemer:
    """Behaves like ``Redeemer`` for the settler's call sites.

    ``check_condition_resolved_async`` always returns finalised; the
    settler only invokes ``redeem_position`` after that, which we drive
    via ``redeem_results`` (a list popped FIFO so multiple cycles can
    return different outcomes).
    """

    def __init__(self, redeem_results: list[RedeemResult]):
        self._queue = list(redeem_results)
        self.calls: list[tuple[str, bool]] = []

    async def check_condition_resolved_async(self, cid: str):
        return True, "no"

    async def redeem_position(self, condition_id: str, neg_risk: bool) -> RedeemResult:
        self.calls.append((condition_id, neg_risk))
        return self._queue.pop(0)


@pytest.mark.asyncio
async def test_settler_invokes_redeemer_and_marks_settled():
    store = await _mk_store()
    try:
        await store.insert_position(
            event_id="evt_w", token_id="tok_winner", token_type="NO", city="Chicago",
            slot_label="80°F to 84°F", side="BUY", entry_price=0.40,
            size_usd=4.0, shares=10.0, strategy="D", buy_reason="seed",
        )
        # Pre-load condition_id + neg_risk on the row (backfill scenario).
        await store.db.execute(
            "UPDATE positions SET condition_id = ?, neg_risk = 1 WHERE token_id = ?",
            ("0xredeemcid", "tok_winner"),
        )
        await store.db.commit()

        redeemer = _FakeRedeemer([
            RedeemResult(status="success", tx_hash="0xtxhash", redeemed_amount=10_000_000),
        ])
        events = {"evt_w": _winner_event("tok_winner")}

        with patch("src.settlement.settler.httpx.AsyncClient",
                   return_value=_FakeClient(events)):
            results = await check_settlements(
                store, redeemer=redeemer, alerter=None,
            )

        # Redeemer was called with the row's condition_id + neg_risk flag.
        assert redeemer.calls == [("0xredeemcid", True)]

        # Position now settled with redeem metadata.
        async with store.db.execute(
            "SELECT status, exit_price, realized_pnl, redeem_status, redeem_tx_hash "
            "FROM positions WHERE token_id = 'tok_winner'"
        ) as cur:
            row = dict((await cur.fetchall())[0])
        assert row["status"] == "settled"
        assert row["exit_price"] == pytest.approx(1.0)
        assert row["realized_pnl"] == pytest.approx(6.0)
        assert row["redeem_status"] == "success"
        assert row["redeem_tx_hash"] == "0xtxhash"

        assert len(results) == 1
        assert results[0].total_pnl == pytest.approx(6.0)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_settler_retries_on_transient_failure_then_succeeds():
    """First cycle: redeemer says ``rpc_error`` → row rolled back, attempt=1.
    Second cycle: redeemer says ``success`` → row settled.
    """
    store = await _mk_store()
    try:
        await store.insert_position(
            event_id="evt_w", token_id="tok_retry", token_type="NO", city="Chicago",
            slot_label="80°F to 84°F", side="BUY", entry_price=0.40,
            size_usd=4.0, shares=10.0, strategy="D", buy_reason="seed",
        )
        await store.db.execute(
            "UPDATE positions SET condition_id = ?, neg_risk = 1 WHERE token_id = ?",
            ("0xretrycid", "tok_retry"),
        )
        await store.db.commit()

        redeemer = _FakeRedeemer([
            RedeemResult(status="rpc_error", error="timeout"),
            RedeemResult(status="success", tx_hash="0xok", redeemed_amount=10_000_000),
        ])
        events = {"evt_w": _winner_event("tok_retry")}

        with patch("src.settlement.settler.httpx.AsyncClient",
                   return_value=_FakeClient(events)):
            # Cycle 1: failure rolls the row back to status=NULL with attempt=1.
            await check_settlements(store, redeemer=redeemer, alerter=None)

            async with store.db.execute(
                "SELECT redeem_status, redeem_attempt_count FROM positions "
                "WHERE token_id = 'tok_retry'"
            ) as cur:
                row = dict((await cur.fetchall())[0])
            assert row["redeem_status"] is None
            assert row["redeem_attempt_count"] == 1

            # Cycle 2: success.
            await check_settlements(store, redeemer=redeemer, alerter=None)

            async with store.db.execute(
                "SELECT status, redeem_status, redeem_tx_hash FROM positions "
                "WHERE token_id = 'tok_retry'"
            ) as cur:
                row = dict((await cur.fetchall())[0])
            assert row["status"] == "settled"
            assert row["redeem_status"] == "success"
            assert row["redeem_tx_hash"] == "0xok"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_settler_alerts_after_max_attempts():
    """Three consecutive failures → status=failed + alerter called once."""
    store = await _mk_store()
    try:
        await store.insert_position(
            event_id="evt_w", token_id="tok_dead", token_type="NO", city="Chicago",
            slot_label="80°F to 84°F", side="BUY", entry_price=0.40,
            size_usd=4.0, shares=10.0, strategy="D", buy_reason="seed",
        )
        await store.db.execute(
            "UPDATE positions SET condition_id = ?, neg_risk = 1 WHERE token_id = ?",
            ("0xdead", "tok_dead"),
        )
        await store.db.commit()

        redeemer = _FakeRedeemer([
            RedeemResult(status="rpc_error", error="rpc_down_1"),
            RedeemResult(status="rpc_error", error="rpc_down_2"),
            RedeemResult(status="rpc_error", error="rpc_down_3"),
        ])
        alerter = AsyncMock()
        alerter.send = AsyncMock()
        events = {"evt_w": _winner_event("tok_dead")}

        with patch("src.settlement.settler.httpx.AsyncClient",
                   return_value=_FakeClient(events)):
            for _ in range(3):
                await check_settlements(store, redeemer=redeemer, alerter=alerter)

        async with store.db.execute(
            "SELECT redeem_status, redeem_attempt_count FROM positions "
            "WHERE token_id = 'tok_dead'"
        ) as cur:
            row = dict((await cur.fetchall())[0])
        assert row["redeem_status"] == "failed"
        assert row["redeem_attempt_count"] == 3

        # Alert fired once when attempt_count reached MAX_REDEEM_ATTEMPTS.
        alert_calls = [c for c in alerter.send.call_args_list]
        assert len(alert_calls) == 1
        # Severity is the first positional arg.
        assert alert_calls[0].args[0] == "critical"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_settler_skips_when_on_chain_not_finalized():
    """Gamma says closed, but on-chain payoutDenominator==0 → wait."""
    store = await _mk_store()
    try:
        await store.insert_position(
            event_id="evt_w", token_id="tok_pending_chain", token_type="NO",
            city="Chicago", slot_label="80°F to 84°F", side="BUY",
            entry_price=0.40, size_usd=4.0, shares=10.0, strategy="D",
            buy_reason="seed",
        )
        await store.db.execute(
            "UPDATE positions SET condition_id = ?, neg_risk = 1 WHERE token_id = ?",
            ("0xpend", "tok_pending_chain"),
        )
        await store.db.commit()

        # Custom redeemer where check_condition_resolved_async returns False.
        class _NotFinalized(_FakeRedeemer):
            async def check_condition_resolved_async(self, cid: str):
                return False, None

        redeemer = _NotFinalized([])
        events = {"evt_w": _winner_event("tok_pending_chain")}
        with patch("src.settlement.settler.httpx.AsyncClient",
                   return_value=_FakeClient(events)):
            await check_settlements(store, redeemer=redeemer, alerter=None)

        # Position must still be open — no claim, no redeem call.
        async with store.db.execute(
            "SELECT status, redeem_status FROM positions "
            "WHERE token_id = 'tok_pending_chain'"
        ) as cur:
            row = dict((await cur.fetchall())[0])
        assert row["status"] == "open"
        assert row["redeem_status"] is None
        assert redeemer.calls == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_paper_mode_no_redeemer_still_settles_winner():
    """Paper / dry-run: redeemer=None.  Winner still flips to status=settled
    so the dashboard P&L reflects reality; no on-chain call attempted."""
    store = await _mk_store()
    try:
        await store.insert_position(
            event_id="evt_w", token_id="tok_paper", token_type="NO", city="NYC",
            slot_label="80°F to 84°F", side="BUY", entry_price=0.40,
            size_usd=4.0, shares=10.0, strategy="D", buy_reason="seed",
        )
        events = {"evt_w": _winner_event("tok_paper")}
        with patch("src.settlement.settler.httpx.AsyncClient",
                   return_value=_FakeClient(events)):
            results = await check_settlements(store, redeemer=None, alerter=None)
        async with store.db.execute(
            "SELECT status, exit_price, realized_pnl FROM positions"
        ) as cur:
            row = dict((await cur.fetchall())[0])
        assert row["status"] == "settled"
        assert row["exit_price"] == pytest.approx(1.0)
        assert row["realized_pnl"] == pytest.approx(6.0)
        assert results[0].total_pnl == pytest.approx(6.0)
    finally:
        await store.close()
