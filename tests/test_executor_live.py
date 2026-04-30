"""FIX-16: live-mode executor coverage.

The happy path, rate-limit retry, and connection-failure paths are all
exercised via a mocked py-clob-client.  Pins that the full live flow
(ClobClient + Executor + PortfolioTracker + Store) writes exactly one
orders row + one positions row on success, and flips the orders row to
'failed' on a transient error that exhausts retries.
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.execution.executor import Executor
from src.markets import clob_client as clob_mod
from src.markets.clob_client import ClobClient, OrderResult
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker


async def _mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    return store


def _mk_live_config() -> SimpleNamespace:
    return SimpleNamespace(
        dry_run=False, paper=False,
        polymarket_api_key="k", polymarket_secret="s", polymarket_passphrase="p",
        eth_private_key="0xabc",
    )


def _attach_default_book(clob_mock_client) -> None:
    """Install a crossable default book on a mocked SDK client.

    FAK-cross-pricing fix (2026-04-30): ``place_limit_order`` now pre-flights
    ``client.get_order_book`` and substitutes a cross-the-spread limit before
    submission.  Tests that exercise the SDK call need this mock or
    ``get_top_of_book`` returns ``(None, None)`` and the wrapper short-circuits
    with THIN_LIQUIDITY.  ``_build_signal`` defaults to price=0.45, so bid 0.43
    / ask 0.45 gives BUY cross 0.46 = 2.2% slip, comfortably under the 5% gate.
    """
    clob_mock_client.get_order_book = MagicMock(
        return_value={
            "bids": [{"price": "0.43", "size": "1000"}],
            "asks": [{"price": "0.45", "size": "1000"}],
        },
    )


def _build_signal(price: float = 0.45, size: float = 10.0) -> TradeSignal:
    slot = TempSlot(
        token_id_yes="tok_yes", token_id_no="tok_no",
        outcome_label="80-81°F", temp_lower_f=80.0, temp_upper_f=81.0,
        price_no=price,
    )
    event = WeatherMarketEvent(
        event_id="ev1", condition_id="c1", city="Chicago",
        market_date=date(2026, 4, 25), slots=[slot],
    )
    return TradeSignal(
        token_type=TokenType.NO, side=Side.BUY, slot=slot, event=event,
        expected_value=0.05, estimated_win_prob=0.72,
        suggested_size_usd=size, strategy="B", reason="live-path test",
    )


@pytest.mark.asyncio
async def test_live_success_writes_order_and_position():
    """Happy path: CLOB returns an order_id → orders row filled, position inserted."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    clob = ClobClient(_mk_live_config())
    clob._client = MagicMock()
    _attach_default_book(clob._client)
    clob._client.create_and_post_order = MagicMock(
        return_value={"orderID": "clob_live_1", "status": "matched"},
    )

    executor = Executor(clob, tracker)
    await executor.execute_signals([_build_signal()])

    async with store.db.execute(
        "SELECT status, order_id, idempotency_key FROM orders"
    ) as cur:
        orders = [dict(r) for r in await cur.fetchall()]
    async with store.db.execute(
        "SELECT status, source_order_id FROM positions"
    ) as cur:
        positions = [dict(r) for r in await cur.fetchall()]

    assert len(orders) == 1 and orders[0]["status"] == "filled"
    assert orders[0]["order_id"] == "clob_live_1"
    assert orders[0]["idempotency_key"]
    assert len(positions) == 1 and positions[0]["source_order_id"] == "clob_live_1"
    await store.close()


@pytest.mark.asyncio
async def test_live_rate_limit_retries_then_succeeds(monkeypatch):
    """A 429 then a success → order goes through on second attempt."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    clob = ClobClient(_mk_live_config())
    clob._client = MagicMock()
    _attach_default_book(clob._client)
    calls = [
        RuntimeError("HTTP 429 Too Many"),
        {"orderID": "clob_live_ok", "status": "matched"},
    ]

    def side_effect(*_):
        v = calls.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    clob._client.create_and_post_order = MagicMock(side_effect=side_effect)

    async def _noop(*_):
        return None
    monkeypatch.setattr(clob_mod.asyncio, "sleep", _noop)

    executor = Executor(clob, tracker)
    await executor.execute_signals([_build_signal()])

    async with store.db.execute(
        "SELECT status, order_id FROM orders"
    ) as cur:
        orders = [dict(r) for r in await cur.fetchall()]
    assert orders[0]["status"] == "filled"
    assert orders[0]["order_id"] == "clob_live_ok"
    await store.close()


@pytest.mark.asyncio
async def test_live_connection_failure_marks_failed(monkeypatch):
    """A persistent network error → order marked failed with message."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    clob = ClobClient(_mk_live_config())
    clob._client = MagicMock()
    _attach_default_book(clob._client)
    clob._client.create_and_post_order = MagicMock(
        side_effect=RuntimeError("connection refused"),
    )

    async def _noop(*_):
        return None
    monkeypatch.setattr(clob_mod.asyncio, "sleep", _noop)

    executor = Executor(clob, tracker)
    await executor.execute_signals([_build_signal()])

    async with store.db.execute(
        "SELECT status, failure_reason FROM orders"
    ) as cur:
        orders = [dict(r) for r in await cur.fetchall()]
    async with store.db.execute("SELECT COUNT(*) FROM positions") as cur:
        (pos_count,) = await cur.fetchone()

    assert orders[0]["status"] == "failed"
    assert "connection refused" in orders[0]["failure_reason"]
    assert pos_count == 0
    await store.close()


@pytest.mark.asyncio
async def test_probe_matches_filled_trade():
    """FIX-05 + FIX-16: live probe matching a real CLOB trade on
    (token, side, price, size) returns state='filled'."""
    clob = ClobClient(_mk_live_config())
    clob._client = MagicMock()
    clob._client.get_trades = MagicMock(return_value=[{
        "id": "trade_99", "side": "BUY", "price": 0.5, "size": 10.0,
    }])
    clob._client.get_orders = MagicMock(return_value=[])

    result = await clob.probe_order_status(
        token_id="tok1", side="BUY", price=0.5, size_shares=10.0,
    )
    assert result.state == "filled"
    assert result.order_id == "trade_99"
