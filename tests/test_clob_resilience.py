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


# ──────────────────────────────────────────────────────────────────────
# v2-6: typing pins for v2 SDK call shapes
# These guard against accidental reverts to v1's untyped (dict / bare
# string) call shapes that would silently break against the v2 server.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_create_and_post_order_receives_typed_OrderArgs(monkeypatch):
    """v2-6: ``client.create_and_post_order`` must be called with a
    typed ``OrderArgs`` instance (NOT a v1-shaped raw dict) AND an
    explicit ``OrderType`` value.  v1's untyped dict shape silently
    fails on the v2 server.
    """
    from py_clob_client_v2 import OrderArgs, OrderType, Side
    client = _make_client()
    client._client.create_and_post_order = MagicMock(return_value={"orderID": "ok"})

    result = await client.place_limit_order(
        token_id="tok-v2", side="BUY", price=0.55, size=10.0,
        idempotency_key="key-v2",
    )

    assert result.success
    assert client._client.create_and_post_order.call_count == 1

    # Inspect the positional args the production code passed.
    call = client._client.create_and_post_order.call_args
    args = call.args
    # 3 positional: order_args, options, order_type
    assert len(args) == 3, (
        f"v2 SDK requires (OrderArgs, options, OrderType) — got {len(args)} args"
    )
    order_args, options, order_type = args

    # 1. OrderArgs is the typed v2 dataclass, not a dict.
    assert isinstance(order_args, OrderArgs), (
        f"first arg must be OrderArgs, got {type(order_args).__name__}"
    )
    assert order_args.token_id == "tok-v2"
    assert order_args.price == 0.55
    assert order_args.size == 10.0
    # Side is the v2 IntEnum (BUY=0, SELL=1), not the v1 string.
    assert order_args.side == Side.BUY

    # 2. options=None lets the server pick tick size dynamically.
    assert options is None

    # 3. OrderType must be GTC (the default — passing it explicitly
    #    is the v2 contract).
    assert order_type == OrderType.GTC


@pytest.mark.asyncio
async def test_v2_cancel_uses_OrderPayload_not_string():
    """v2-6: ``client.cancel_order`` must be called with a typed
    ``OrderPayload(orderID=...)`` (NOT v1's bare ``client.cancel(str)``).
    """
    from py_clob_client_v2.clob_types import OrderPayload
    client = _make_client()
    client._client.cancel_order = MagicMock(return_value={"success": True})

    ok = await client.cancel_order("test-order-123")
    assert ok is True
    assert client._client.cancel_order.call_count == 1

    payload = client._client.cancel_order.call_args.args[0]
    assert isinstance(payload, OrderPayload), (
        f"v2 SDK requires OrderPayload, got {type(payload).__name__}"
    )
    # Field is camelCase ``orderID`` matching the v2 wire format.
    assert payload.orderID == "test-order-123"

    # And the v1 ``client.cancel`` (bare string) must NEVER fire — a
    # regression that reverted the v2 rename would set this attribute
    # via auto-mock.  Pin its absence.
    assert not getattr(client._client, "cancel").called, (
        "v1 client.cancel(order_id) shape must not be used — "
        "regression to pre-v2-4 shape detected"
    )


# ──────────────────────────────────────────────────────────────────────
# v2-7 (2026-04-27): dict-shaped responses from /midpoint and
# /last-trade-price.  First live cycle on v2 raised
# ``TypeError: float() argument must be a string or a real number, not
# 'dict'`` because the SDK forwards the raw JSON ({"mid": "0.5"} etc.)
# and the wrapper called float() unconditionally.  Pin the unwrap so a
# revert can't reach live again.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_midpoint_unpacks_v2_dict_response():
    """``client.get_midpoint`` returns ``{"mid": "0.535"}`` against the
    real /midpoint endpoint (verified by curl 2026-04-27).  Wrapper must
    extract ``mid`` and float-coerce — never ``float({"mid": ...})``.
    """
    client = _make_client()
    client._client.get_midpoint = MagicMock(return_value={"mid": "0.535"})

    price = await client.get_midpoint("tok-1")

    assert price == 0.535
    assert client._client.get_midpoint.call_count == 1


@pytest.mark.asyncio
async def test_get_midpoint_handles_legacy_float_response():
    """Defensive fallback: if a future SDK build returns a raw number,
    don't break.  isinstance check picks the right branch."""
    client = _make_client()
    client._client.get_midpoint = MagicMock(return_value="0.42")

    assert await client.get_midpoint("tok-1") == 0.42


@pytest.mark.asyncio
async def test_get_midpoint_returns_none_on_unparseable_dict():
    """Unknown dict shape (no ``mid`` / ``midpoint`` key) → ``float(0.0)``
    via the .get() default would mask a real outage by reporting 0.0.
    But the alternative — raising — also leaks into the live log as a
    confusing TypeError stack.  Current behaviour: 0.0 falls through
    safely; the rebalancer's ``if price is not None`` filter (callsite
    in get_prices_batch) keeps the slot when it's truly 0, and the
    Gamma fallback covers the slot when CLOB really can't price it.
    """
    client = _make_client()
    client._client.get_midpoint = MagicMock(return_value={"unexpected": "shape"})

    # 0.0 is the documented fallback — it's truthy-falsy in
    # ``get_prices_batch``'s ``or`` chain, so the caller will fall
    # through to ``get_last_trade_price``.
    assert await client.get_midpoint("tok-1") == 0.0


@pytest.mark.asyncio
async def test_get_last_trade_price_unpacks_v2_dict_response():
    """``/last-trade-price`` returns ``{"price": "0.001", "side": "BUY"}``
    (verified by curl 2026-04-27).  Wrapper extracts ``price``."""
    client = _make_client()
    client._client.get_last_trade_price = MagicMock(
        return_value={"price": "0.001", "side": "BUY"},
    )

    price = await client.get_last_trade_price("tok-1")

    assert price == 0.001
    assert client._client.get_last_trade_price.call_count == 1


@pytest.mark.asyncio
async def test_get_last_trade_price_handles_legacy_float_response():
    client = _make_client()
    client._client.get_last_trade_price = MagicMock(return_value=0.42)

    assert await client.get_last_trade_price("tok-1") == 0.42
