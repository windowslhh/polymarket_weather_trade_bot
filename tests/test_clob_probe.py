"""Review Blocker #2: ClobClient.probe_order_status must match CLOB state
to our pending orders row (since py-clob-client doesn't echo our
client-side idempotency_key to Polymarket).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.markets.clob_client import ClobClient


def _make_client(*, paper: bool = False, dry_run: bool = False) -> ClobClient:
    cfg = SimpleNamespace(
        dry_run=dry_run, paper=paper,
        polymarket_api_key="k", polymarket_secret="s", polymarket_passphrase="p",
        eth_private_key="0xabc",
    )
    client = ClobClient(cfg)  # type: ignore[arg-type]
    client._client = MagicMock()
    return client


@pytest.mark.asyncio
async def test_paper_mode_is_unreachable():
    client = _make_client(paper=True)
    r = await client.probe_order_status(
        token_id="t", side="BUY", price=0.5, size_shares=10.0,
    )
    assert r.state == "unreachable"


@pytest.mark.asyncio
async def test_dry_run_is_unreachable():
    client = _make_client(dry_run=True)
    r = await client.probe_order_status(
        token_id="t", side="BUY", price=0.5, size_shares=10.0,
    )
    assert r.state == "unreachable"


@pytest.mark.asyncio
async def test_matching_trade_returns_filled():
    client = _make_client()
    client._client.get_trades = MagicMock(return_value=[{
        "id": "t1", "side": "BUY", "price": 0.501, "size": 10.0, "asset_id": "tok1",
    }])
    client._client.get_orders = MagicMock(return_value=[])

    r = await client.probe_order_status(
        token_id="tok1", side="BUY", price=0.5, size_shares=10.0,
    )
    assert r.state == "filled"
    assert r.order_id == "t1"


@pytest.mark.asyncio
async def test_no_match_returns_unknown():
    client = _make_client()
    client._client.get_trades = MagicMock(return_value=[])
    client._client.get_orders = MagicMock(return_value=[])

    r = await client.probe_order_status(
        token_id="tok1", side="BUY", price=0.5, size_shares=10.0,
    )
    assert r.state == "unknown"


@pytest.mark.asyncio
async def test_open_order_returns_open():
    """A resting limit order with matching params → state='open'.

    This is the key path that review Blocker #2 required: we must NOT
    incorrectly mark a still-live CLOB order as 'failed'.
    """
    client = _make_client()
    client._client.get_trades = MagicMock(return_value=[])
    client._client.get_orders = MagicMock(return_value=[{
        "id": "o99", "side": "BUY", "price": 0.5, "original_size": 10.0,
        "asset_id": "tok1",
    }])

    r = await client.probe_order_status(
        token_id="tok1", side="BUY", price=0.5, size_shares=10.0,
    )
    assert r.state == "open"
    assert r.order_id == "o99"


@pytest.mark.asyncio
async def test_side_mismatch_is_not_matched():
    """Same price/size/token but opposite side → no match."""
    client = _make_client()
    client._client.get_trades = MagicMock(return_value=[{
        "id": "t_sell", "side": "SELL", "price": 0.5, "size": 10.0,
    }])
    client._client.get_orders = MagicMock(return_value=[])

    r = await client.probe_order_status(
        token_id="tok1", side="BUY", price=0.5, size_shares=10.0,
    )
    assert r.state == "unknown"


@pytest.mark.asyncio
async def test_dict_wrapped_response_is_unwrapped():
    """py-clob-client sometimes returns {'data': [...], 'next_cursor': ''}."""
    client = _make_client()
    client._client.get_trades = MagicMock(return_value={
        "data": [{"id": "t1", "side": "BUY", "price": 0.5, "size": 10.0}],
        "next_cursor": "",
    })
    client._client.get_orders = MagicMock(return_value={"data": []})

    r = await client.probe_order_status(
        token_id="tok1", side="BUY", price=0.5, size_shares=10.0,
    )
    assert r.state == "filled"


@pytest.mark.asyncio
async def test_price_improvement_still_matches():
    """Review H-3: a price-improvement fill must match the pending order.

    A BUY@0.50 limit can fill at 0.494 when a marketable sell crosses our
    bid.  With the pre-H-3 0.005 tolerance |0.494-0.50|=0.006 failed to
    match; H-3 widens to 0.01 so this real-market behaviour succeeds.
    """
    client = _make_client()
    client._client.get_trades = MagicMock(return_value=[{
        "id": "improved_1", "side": "BUY", "price": 0.494, "size": 10.0,
    }])
    client._client.get_orders = MagicMock(return_value=[])

    r = await client.probe_order_status(
        token_id="tok1", side="BUY", price=0.5, size_shares=10.0,
    )
    assert r.state == "filled", (
        "Price-improved fill (0.494 vs 0.50 limit, delta 0.006) must match"
    )


@pytest.mark.asyncio
async def test_price_outside_tolerance_is_unmatched():
    """H-3 widened tolerance to 0.01; 0.011+ drift still misses intentionally."""
    client = _make_client()
    client._client.get_trades = MagicMock(return_value=[{
        "id": "far", "side": "BUY", "price": 0.48, "size": 10.0,  # 0.02 off
    }])
    client._client.get_orders = MagicMock(return_value=[])

    r = await client.probe_order_status(
        token_id="tok1", side="BUY", price=0.5, size_shares=10.0,
    )
    assert r.state == "unknown"


@pytest.mark.asyncio
async def test_exception_returns_unreachable():
    client = _make_client()
    client._client.get_trades = MagicMock(side_effect=RuntimeError("network"))

    r = await client.probe_order_status(
        token_id="tok1", side="BUY", price=0.5, size_shares=10.0,
    )
    assert r.state == "unreachable"
    assert "network" in r.message
