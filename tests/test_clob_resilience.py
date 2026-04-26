"""FIX-04: timeout + retry + 429 backoff for place_limit_order.

These tests monkeypatch `create_and_post_order` on the underlying py-clob-client
so we drive retry behaviour deterministically without hitting the network.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.markets import clob_client as clob_mod
from src.markets.clob_client import ClobClient


def _make_client() -> ClobClient:
    cfg = SimpleNamespace(
        dry_run=False, paper=False,
        polymarket_api_key="k", polymarket_secret="s", polymarket_passphrase="p",
        eth_private_key="0xabc",
    )
    client = ClobClient(cfg)  # type: ignore[arg-type]
    # Bypass _get_client so we don't import py-clob-client in tests.
    client._client = MagicMock()
    return client


@pytest.mark.asyncio
async def test_order_succeeds_on_first_attempt(monkeypatch):
    client = _make_client()
    client._client.create_and_post_order = MagicMock(return_value={"orderID": "ok"})
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.5, size=10, idempotency_key="key1",
    )
    assert result.success
    assert result.order_id == "ok"
    assert client._client.create_and_post_order.call_count == 1


@pytest.mark.asyncio
async def test_order_retries_on_generic_exception(monkeypatch):
    client = _make_client()
    # Fail twice, succeed on third.
    calls = [RuntimeError("boom"), RuntimeError("still boom"), {"orderID": "ok3"}]

    def side_effect(*_):
        v = calls.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    client._client.create_and_post_order = MagicMock(side_effect=side_effect)

    # Patch asyncio.sleep so the test finishes fast.  Monkeypatch tears down
    # cleanly between tests; a naked `with patch.object(...)` wrapped around
    # it confused the teardown order and leaked a MagicMock sleep into
    # other tests.
    async def _noop(*_):
        return None

    monkeypatch.setattr(clob_mod.asyncio, "sleep", _noop)
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.5, size=10,
    )
    assert result.success
    assert result.order_id == "ok3"
    assert client._client.create_and_post_order.call_count == 3


@pytest.mark.asyncio
async def test_order_gives_up_after_max_attempts(monkeypatch):
    client = _make_client()
    client._client.create_and_post_order = MagicMock(
        side_effect=RuntimeError("persistent"),
    )

    async def _noop(*_):
        return None

    monkeypatch.setattr(clob_mod.asyncio, "sleep", _noop)
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.5, size=10,
    )
    assert not result.success
    assert "persistent" in result.message
    # ORDER_MAX_ATTEMPTS = 3 by default.
    assert client._client.create_and_post_order.call_count == clob_mod.ORDER_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_rate_limit_triggers_longer_backoff(monkeypatch):
    """A 429 response should use the rate-limit backoff, not the generic path."""
    client = _make_client()
    calls = [
        RuntimeError("HTTP 429: Too Many Requests"),
        {"orderID": "after_429"},
    ]

    def side_effect(*_):
        v = calls.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    client._client.create_and_post_order = MagicMock(side_effect=side_effect)

    sleep_args: list[float] = []

    async def _capture_sleep(s):
        sleep_args.append(s)

    monkeypatch.setattr(clob_mod.asyncio, "sleep", _capture_sleep)
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.5, size=10,
    )
    assert result.success
    assert result.order_id == "after_429"
    # The first sleep is the 429 backoff: 2**0 + jitter = 1..2s, bounded by the cap.
    assert sleep_args, "expected at least one sleep on 429"
    assert sleep_args[0] >= 1.0, f"rate-limit sleep too short: {sleep_args}"


@pytest.mark.asyncio
async def test_timeout_does_not_retry(monkeypatch):
    """Review Blocker #1: TimeoutError must NOT trigger a retry.

    `asyncio.timeout` cancels the awaiter but cannot cancel the underlying
    synchronous HTTP POST running in the to_thread worker.  A naive retry
    can create a second order on CLOB — py-clob-client does not surface
    the client-side idempotency_key to Polymarket, so there is no
    server-side dedup we can lean on.

    Expected behaviour: first timeout → immediate failure, one attempt.
    """
    client = _make_client()

    def hang(*_):
        import time
        time.sleep(5)  # longer than our override timeout
        return {"orderID": "never"}

    client._client.create_and_post_order = MagicMock(side_effect=hang)

    # Shrink the timeout so the test is quick.
    monkeypatch.setattr(clob_mod, "ORDER_TIMEOUT_S", 0.05)
    monkeypatch.setattr(clob_mod, "ORDER_MAX_ATTEMPTS", 3)

    async def _noop(*_):
        return None

    monkeypatch.setattr(clob_mod.asyncio, "sleep", _noop)

    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.5, size=10,
    )
    assert not result.success
    assert "timeout" in result.message.lower()
    assert client._client.create_and_post_order.call_count == 1, (
        "Timeout must not trigger a retry — would risk double-order."
    )


@pytest.mark.asyncio
async def test_empty_order_id_no_retry(monkeypatch):
    """FIX-M4 interaction: an explicit empty order_id is a terminal failure,
    NOT a transient retryable error (no point hammering the CLOB for the same
    malformed response)."""
    client = _make_client()
    client._client.create_and_post_order = MagicMock(return_value={"orderID": ""})

    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.5, size=10,
    )
    assert not result.success
    assert client._client.create_and_post_order.call_count == 1


@pytest.mark.asyncio
async def test_place_limit_order_passes_OrderArgs_dataclass(monkeypatch):
    """FIX-2P-1: SDK expects OrderArgs (snake_case dataclass), NOT a dict.

    Pre-fix the function passed a {"tokenID": ..., "size": ..., "side": ...}
    dict, which would crash inside the SDK signer with AttributeError on
    ``order_args.token_id``.  Paper mode early-returns before this line so
    25h of paper trading never exercised the bug — first live order would
    have failed.  This test pins the dataclass type + snake_case field names.
    """
    from py_clob_client.clob_types import OrderArgs

    client = _make_client()
    captured: dict[str, object] = {}

    def _capture(arg, *_rest):
        captured["arg"] = arg
        return {"orderID": "ok"}

    client._client.create_and_post_order = MagicMock(side_effect=_capture)

    result = await client.place_limit_order(
        token_id="tok-abc", side="BUY", price=0.55, size=12.5,
    )
    assert result.success
    arg = captured["arg"]
    assert isinstance(arg, OrderArgs)
    assert arg.token_id == "tok-abc"
    assert arg.price == 0.55
    assert arg.size == 12.5
    assert arg.side == "BUY"
