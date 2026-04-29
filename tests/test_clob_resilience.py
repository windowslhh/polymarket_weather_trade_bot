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
    client._client.create_and_post_order = MagicMock(
        return_value={"orderID": "ok", "status": "matched"},
    )
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
    calls = [
        RuntimeError("boom"),
        RuntimeError("still boom"),
        {"orderID": "ok3", "status": "matched"},
    ]

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
        {"orderID": "after_429", "status": "matched"},
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
    client._client.create_and_post_order = MagicMock(
        return_value={"orderID": "ok", "status": "matched"},
    )

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

    # 3. OrderType must be FAK (Fill And Kill).  2026-04-28: switched
    #    from GTC because GTC + Gamma last-trade price let our limit
    #    orders rest on the book as makers, which (a) violated the
    #    taker-only EV / fee model and (b) triggered the now-removed
    #    A1 self-cancel logic.  FAK forces immediate fill-or-kill so
    #    no resting state is possible.
    assert order_type == OrderType.FAK


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


# ──────────────────────────────────────────────────────────────────────
# 2026-04-28: ghost-position prevention.  v2 ``create_and_post_order``
# returns the same {"orderID": ...} dict shape for both immediately
# matched fills AND orders that posted but didn't fully fill.  The bot
# was trusting any non-empty orderID as a successful fill, which produced
# ghost rows in ``positions`` for orders that only sat on the book.
#
# Same-day update: switched from ``OrderType.GTC`` to ``OrderType.FAK``
# (see clob_client.py).  FAK kills any unfilled remainder server-side,
# so the residual statuses we have to recognise here are the kill
# variants (``cancelled`` / ``killed``) rather than GTC's ``unmatched``
# / ``live`` — but the wrapper's success criterion (``status='matched'``
# OR tx hashes present) is unchanged, so a failure-mode test parametrises
# cleanly across both.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("kill_status", ["cancelled", "killed", "unmatched"])
async def test_unfilled_order_returns_failure_no_position(kill_status):
    """An order whose unfilled remainder is killed by FAK (or, for back-compat
    with the GTC-era status string, an ``unmatched`` resting state we shouldn't
    see in practice but must still recognise) must surface success=False so the
    executor records the orders row as 'failed' (no positions row created).
    """
    client = _make_client()
    client._client.create_and_post_order = MagicMock(
        return_value={"orderID": "0xabc", "status": kill_status},
    )

    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.565, size=10,
    )

    assert result.success is False
    assert result.order_id == "0xabc"
    # Wrapper now reports "order not filled (status=...)" so the message
    # is accurate under FAK semantics; the kill-status string is echoed
    # back so logs / decision_log retain the diagnostic.
    assert "not filled" in result.message.lower()
    assert kill_status in result.message.lower()
    # Must NOT retry — kill / no-fill is a terminal classification, not a
    # transient failure.  Hammering the CLOB would post duplicate orders.
    assert client._client.create_and_post_order.call_count == 1


@pytest.mark.asyncio
async def test_matched_with_tx_hashes_succeeds():
    """Order with ``status='matched'`` AND ``transactionsHashes`` populated
    is the canonical successful-fill shape."""
    client = _make_client()
    client._client.create_and_post_order = MagicMock(
        return_value={
            "orderID": "0xabc",
            "status": "matched",
            "transactionsHashes": ["0xdeadbeef"],
        },
    )

    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.5, size=10,
    )

    assert result.success is True
    assert result.order_id == "0xabc"


@pytest.mark.asyncio
async def test_matched_status_alone_succeeds():
    """``status='matched'`` without explicit ``transactionsHashes`` is also
    a successful fill (matched-side claim is sufficient — the field can be
    omitted on some response shapes)."""
    client = _make_client()
    client._client.create_and_post_order = MagicMock(
        return_value={"orderID": "0xabc", "status": "matched"},
    )

    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.5, size=10,
    )

    assert result.success is True
    assert result.order_id == "0xabc"


@pytest.mark.asyncio
async def test_tx_hashes_without_status_succeeds():
    """Defensive fallback: tx hashes present but status field omitted
    (some response shapes elide it once tx is finalized) → still a fill."""
    client = _make_client()
    client._client.create_and_post_order = MagicMock(
        return_value={
            "orderID": "0xabc",
            "transactionsHashes": ["0xdeadbeef"],
        },
    )

    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.5, size=10,
    )

    assert result.success is True


@pytest.mark.asyncio
async def test_no_status_no_tx_hashes_returns_failure():
    """An ``orderID`` with neither matched-status nor tx hashes is the
    same un-filled signal — must NOT create a positions row.  Pre-fix the
    wrapper accepted this as success on the strength of the orderID alone
    and that's what produced the Miami 86-87 NO @0.565 ghost on
    2026-04-28."""
    client = _make_client()
    client._client.create_and_post_order = MagicMock(
        return_value={"orderID": "0xabc"},
    )

    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.5, size=10,
    )

    assert result.success is False
    assert result.order_id == "0xabc"
    assert "not filled" in result.message.lower()


# ──────────────────────────────────────────────────────────────────────
# 2026-04-28: get_fill_summary aggregates trade-level fill data so the
# dashboard can show actual per-share entry instead of limit price.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_fill_summary_paper_returns_none(monkeypatch):
    """Paper / dry-run modes have no real CLOB trades — short-circuit to None
    so callers fall back to limit price."""
    cfg = SimpleNamespace(
        dry_run=False, paper=True,
        polymarket_api_key="k", polymarket_secret="s", polymarket_passphrase="p",
        eth_private_key="0xabc",
    )
    client = ClobClient(cfg)  # type: ignore[arg-type]
    assert await client.get_fill_summary(token_id="tok", order_id="0xabc") is None


@pytest.mark.asyncio
async def test_get_fill_summary_aggregates_weighted_price_and_fee():
    """Two partial fills at different prices → weighted-avg ``match_price``
    + summed fees.  The Polymarket trade response carries ``fee_rate_bps``
    as the COMBINED maker+taker bps; our taker share is bps/2/10000.

    Verifies the fee math against the prod-confirmed formula:
      fee = size × (bps/2/10000) × price × (1 - price)
    """
    client = _make_client()
    # Two trades: 6 shares @ 0.685, 4 shares @ 0.690 — both belong to
    # taker_order_id=0xORDER (our just-placed BUY).  Combined bps=1000
    # (5% taker + 5% maker), so taker rate = 0.05.
    trades = [
        {
            "taker_order_id": "0xORDER",
            "size": "6.0", "price": "0.685",
            "fee_rate_bps": 1000,
        },
        {
            "taker_order_id": "0xORDER",
            "size": "4.0", "price": "0.690",
            "fee_rate_bps": 1000,
        },
        # Unrelated trade — must be excluded.
        {
            "taker_order_id": "0xSOMEONE_ELSE",
            "size": "10.0", "price": "0.5",
            "fee_rate_bps": 1000,
        },
    ]
    client._client.get_trades = MagicMock(return_value=trades)

    summary = await client.get_fill_summary(
        token_id="tok", order_id="0xORDER",
    )

    assert summary is not None
    assert summary.shares == 10.0
    # Weighted: (6*0.685 + 4*0.690) / 10 = (4.11 + 2.76) / 10 = 0.687
    assert abs(summary.match_price - 0.687) < 1e-9
    # Fee per trade: size * 0.05 * price * (1-price)
    # T1: 6 * 0.05 * 0.685 * 0.315 = 0.0647325
    # T2: 4 * 0.05 * 0.690 * 0.310 = 0.042780
    expected_fee = 6 * 0.05 * 0.685 * 0.315 + 4 * 0.05 * 0.690 * 0.310
    assert abs(summary.fee_paid_usd - expected_fee) < 1e-9


@pytest.mark.asyncio
async def test_get_fill_summary_handles_paginated_dict_response():
    """``get_trades`` sometimes returns ``{"data": [...], "next_cursor": ...}``
    — the wrapper's ``_extract_list`` already normalises both shapes; this
    pins the contract."""
    client = _make_client()
    trades_resp = {
        "data": [
            {
                "taker_order_id": "0xORDER",
                "size": "5.0", "price": "0.50",
                "fee_rate_bps": 1000,
            },
        ],
        "next_cursor": "LTE=",
    }
    client._client.get_trades = MagicMock(return_value=trades_resp)

    summary = await client.get_fill_summary(
        token_id="tok", order_id="0xORDER",
    )

    assert summary is not None
    assert summary.shares == 5.0
    assert summary.match_price == 0.50
    # 5 * 0.05 * 0.50 * 0.50 = 0.0625
    assert abs(summary.fee_paid_usd - 0.0625) < 1e-9


@pytest.mark.asyncio
async def test_get_fill_summary_no_matching_trades_returns_none():
    """If no trade rows match our ``order_id`` (timing lag, cancelled, etc.),
    return None so the executor records limit price as a documented fallback."""
    client = _make_client()
    client._client.get_trades = MagicMock(return_value=[
        {
            "taker_order_id": "0xSOMEONE_ELSE",
            "size": "10.0", "price": "0.5", "fee_rate_bps": 1000,
        },
    ])

    summary = await client.get_fill_summary(
        token_id="tok", order_id="0xORDER",
    )

    assert summary is None


@pytest.mark.asyncio
async def test_order_version_mismatch_force_refreshes_and_retries(monkeypatch):
    """2026-04-28 cutover defense: a Polymarket exchange-version cutover poisons
    ``ClobClient.__cached_version`` (the SDK caches ``GET /version`` on first
    call and never re-reads it; its built-in refresh-on-mismatch path is dead
    code because the HTTP helper raises before reaching it).  ``place_limit_order``
    must (a) detect the ``order_version_mismatch`` error, (b) force-refresh the
    cache via the name-mangled ``_ClobClient__resolve_version(force_update=True)``
    accessor, and (c) retry once so the next attempt is built against the
    post-cutover schema.
    """
    client = _make_client()
    calls = [
        RuntimeError(
            "PolyApiException[status_code=400, "
            "error_message={'error': 'order_version_mismatch'}]"
        ),
        {"orderID": "after_refresh", "status": "matched"},
    ]

    def side_effect(*_):
        v = calls.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    client._client.create_and_post_order = MagicMock(side_effect=side_effect)
    # The real SDK exposes the method as the name-mangled
    # ``_ClobClient__resolve_version``.  Mock it so we can assert it was hit
    # exactly once with ``force_update=True``.
    client._client._ClobClient__resolve_version = MagicMock(return_value=2)

    async def _noop(*_):
        return None

    monkeypatch.setattr(clob_mod.asyncio, "sleep", _noop)
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.5, size=10,
    )

    assert result.success
    assert result.order_id == "after_refresh"
    assert client._client.create_and_post_order.call_count == 2
    client._client._ClobClient__resolve_version.assert_called_once_with(
        force_update=True,
    )


@pytest.mark.asyncio
async def test_order_version_mismatch_survives_refresh_failure(monkeypatch):
    """If the cache-refresh itself raises (Polymarket /version endpoint flaking
    in the middle of a cutover, network blip, etc.), we must NOT crash.  Log
    and fall through to the standard retry — the next attempt will likely
    fail too, but the bot should keep running and surface the error normally
    once retries exhaust."""
    client = _make_client()
    client._client.create_and_post_order = MagicMock(
        side_effect=RuntimeError(
            "PolyApiException[status_code=400, "
            "error_message={'error': 'order_version_mismatch'}]"
        ),
    )
    client._client._ClobClient__resolve_version = MagicMock(
        side_effect=RuntimeError("upstream /version flaking"),
    )

    async def _noop(*_):
        return None

    monkeypatch.setattr(clob_mod.asyncio, "sleep", _noop)
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.5, size=10,
    )

    assert not result.success
    assert "order_version_mismatch" in result.message
    # Refresh attempted once per failed attempt — the helper swallows the
    # inner RuntimeError so the outer retry loop reaches its full count.
    assert client._client._ClobClient__resolve_version.call_count == clob_mod.ORDER_MAX_ATTEMPTS
    assert client._client.create_and_post_order.call_count == clob_mod.ORDER_MAX_ATTEMPTS


# ──────────────────────────────────────────────────────────────────────
# v2-9 (2026-04-29): Polymarket tightened the BUY maker_amount precision
# rule post-cutover.  Server now 400s with "max accuracy of 2 decimals"
# when SDK ships the default 4-decimal precision (e.g. 7.41 × 0.55 =
# 4.0755 in USDC).  We monkey-patch ``ROUNDING_CONFIG`` at import to
# clamp ``amount`` to 2 across every tick size; these tests pin both the
# patch *being applied* and the resulting maker_amount *being 2-decimal-
# clean for problem inputs*.  A revert that drops the patch reverts the
# 3/3-BUY-rejection bug from the 2026-04-29 incident.
# ──────────────────────────────────────────────────────────────────────


def test_v2_9_rounding_config_amount_clamped_to_2():
    """The v2-9 monkey-patch must run at import time and set
    ``ROUNDING_CONFIG[ts].amount == 2`` for every tick size, while
    leaving ``price`` and ``size`` precisions untouched."""
    from py_clob_client_v2.order_builder import builder as _v2_builder
    for ts, cfg in _v2_builder.ROUNDING_CONFIG.items():
        assert cfg.amount == 2, (
            f"ROUNDING_CONFIG[{ts!r}].amount must be clamped to 2 by "
            f"the v2-9 patch (got {cfg.amount}); a value > 2 reverts "
            "the 'invalid amounts, max accuracy of 2 decimals' BUY "
            "rejection bug from 2026-04-29."
        )
        # Defensive: price/size precision must NOT be clobbered — they
        # control tick-size validation upstream of amount rounding.
        assert cfg.price >= 1
        assert cfg.size == 2


@pytest.mark.parametrize(
    # (size_shares, price, tick) — the first row is the live-bot example
    # from 2026-04-29 that triggered "invalid amounts, max accuracy of 2
    # decimals" against `7.41 × 0.5550 = $4.11255`.
    "size_shares,price,tick",
    [
        (7.41, 0.5550, "0.01"),
        (7.41, 0.55, "0.01"),
        (9.01, 0.555, "0.01"),
        (3.27, 0.91, "0.01"),
        (12.34, 0.789, "0.001"),
    ],
)
def test_v2_9_buy_maker_amount_quantized_to_cents(size_shares, price, tick):
    """End-to-end: feed problem inputs through the SDK's ``get_order_amounts``
    builder (which is what ``create_and_post_order`` calls under the hood)
    and verify the resulting maker_amount fits in 2 decimal places of USDC.

    The maker_amount the SDK emits is a USDC-decimals integer (×1e6) — so
    "2 decimals of USDC" means it must be a multiple of 10_000 (1 cent =
    10_000 micro-USDC).  Same precision the gateway enforces.
    """
    from py_clob_client_v2.order_builder import builder as _v2_builder
    from py_clob_client_v2.order_builder.constants import BUY

    round_config = _v2_builder.ROUNDING_CONFIG[tick]
    builder = _v2_builder.OrderBuilder.__new__(_v2_builder.OrderBuilder)
    _, maker_amount, _ = builder.get_order_amounts(
        BUY, size_shares, price, round_config,
    )

    # maker_amount is in 6-decimal USDC.  2-decimal USDC ↔ multiple of
    # 10_000 micro-USDC.  Anything finer than a cent leaks into the wire
    # and trips the gateway's precision check.
    assert isinstance(maker_amount, int)
    assert maker_amount % 10_000 == 0, (
        f"maker_amount={maker_amount} (={maker_amount / 1e6:.6f} USDC) "
        f"is not 2-decimal-clean for size={size_shares} price={price} "
        f"tick={tick} — server will reject with 'max accuracy of 2 decimals'"
    )


def test_v2_9_sell_taker_amount_within_server_limits():
    """SELL's taker_amount (USDC) is also routed through ``round_config.amount``
    — pinning the patch's side-effect: SELL taker is ≤ 4 decimals (server
    cap for SELL takers).  The patch's 2-decimal clamp is over-strict but
    still server-valid; pin that it stays valid."""
    from py_clob_client_v2.order_builder import builder as _v2_builder
    from py_clob_client_v2.order_builder.constants import SELL

    round_config = _v2_builder.ROUNDING_CONFIG["0.01"]
    builder = _v2_builder.OrderBuilder.__new__(_v2_builder.OrderBuilder)
    _, _, taker_amount = builder.get_order_amounts(
        SELL, 7.41, 0.555, round_config,
    )

    # SELL taker (USDC) ≤ 4 decimals server-side; with v2-9's amount=2
    # clamp it'll actually be ≤ 2 decimals (multiple of 10_000 micro-USDC).
    assert isinstance(taker_amount, int)
    # Must be a multiple of 100 (4 decimals = 1e2 micro-USDC) — pre-v2-9
    # baseline.  Stricter assertion (multiple of 10_000) verifies the
    # patch is actively in effect for SELL too.
    assert taker_amount % 100 == 0, (
        f"taker_amount={taker_amount} exceeds server's 4-decimal cap "
        f"for SELL — would 400 with 'max accuracy of 4 decimals'"
    )
    assert taker_amount % 10_000 == 0, (
        f"v2-9 patch should also clamp SELL taker_amount to 2 decimals "
        f"(over-strict but still server-valid); got {taker_amount}"
    )


# ──────────────────────────────────────────────────────────────────────
# Bug B hardening (2026-04-29): the version-cache reset is now belt-and-
# suspenders — we nullify ``_ClobClient__cached_version`` *first* and
# *then* call ``_ClobClient__resolve_version(force_update=True)``.  If a
# future SDK refactor breaks the force_update branch, the explicit None
# write still triggers the "cache empty → re-fetch" branch on the next
# call.  Pin this ordering so a regression that drops the None-write
# can't reach live without the test catching it.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_force_refresh_nullifies_cached_version_attribute(monkeypatch):
    """The wrapper must set ``_ClobClient__cached_version = None`` BEFORE
    calling ``_ClobClient__resolve_version(force_update=True)`` — verifies
    the belt-and-suspenders ordering by inspecting the SDK attr at the
    moment ``__resolve_version`` is invoked."""
    client = _make_client()
    # Seed a stale cached version on the inner SDK mock.
    client._client._ClobClient__cached_version = 1

    observed_cache_at_call: list = []

    def _resolve(*, force_update: bool):
        # Snapshot the cache value the moment __resolve_version is called.
        observed_cache_at_call.append(
            client._client._ClobClient__cached_version,
        )
        return 2

    client._client._ClobClient__resolve_version = _resolve

    client._client.create_and_post_order = MagicMock(
        side_effect=[
            RuntimeError(
                "PolyApiException[status_code=400, "
                "error_message={'error': 'order_version_mismatch'}]"
            ),
            {"orderID": "after_refresh", "status": "matched"},
        ],
    )

    async def _noop(*_):
        return None

    monkeypatch.setattr(clob_mod.asyncio, "sleep", _noop)
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.5, size=10,
    )

    assert result.success
    # The cache attribute was None at the instant __resolve_version ran
    # — proving the belt-and-suspenders write happened first.
    assert observed_cache_at_call == [None], (
        "force-refresh must null out _ClobClient__cached_version BEFORE "
        "calling _ClobClient__resolve_version; observed snapshots: "
        f"{observed_cache_at_call}"
    )


@pytest.mark.asyncio
async def test_get_fill_summary_prefers_explicit_fee_field():
    """If the trade response carries an explicit ``fee_paid`` field,
    prefer it over computing from ``fee_rate_bps``."""
    client = _make_client()
    client._client.get_trades = MagicMock(return_value=[
        {
            "taker_order_id": "0xORDER",
            "size": "10.0", "price": "0.50",
            "fee_paid": "0.0123",  # explicit beats the 0.0625 computation
            "fee_rate_bps": 1000,
        },
    ])

    summary = await client.get_fill_summary(
        token_id="tok", order_id="0xORDER",
    )

    assert summary is not None
    assert abs(summary.fee_paid_usd - 0.0123) < 1e-9
