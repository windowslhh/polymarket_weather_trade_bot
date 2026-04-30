"""FAK-cross-pricing fix (2026-04-30): pre-flight order book + cross-the-spread limit.

Root cause this fix addresses: ``signal.price`` ends up as a CLOB midpoint /
last-trade price (see ``ClobClient.get_prices_batch`` → ``get_midpoint``
fallback chain).  Submitting a FAK ("Fill And Kill") order at midpoint is
mathematically guaranteed to never fill: for any positive spread,
midpoint < best_ask AND midpoint > best_bid, so the matcher finds nothing
to cross and either 400's "no orders found to match" or 200's status=delayed
and kills async.  Both paths surfaced in production 2026-04-29 (1/19 BUY
fill, 0/16 SELL fill in the FAK era).

The fix replaces ``price`` with ``best_ask + 1 tick`` for BUY /
``best_bid - 1 tick`` for SELL just before the SDK call, and short-circuits
when the book is empty on the side we'd cross or when the cross is more
than ``max_taker_slippage`` (default 5%) above mid.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.markets import clob_client as clob_mod
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


# ---------------------------------------------------------------------------
# 1-4. ``get_top_of_book`` parses the SDK book correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_top_of_book_normal():
    client = _make_client()
    client._client.get_order_book = MagicMock(return_value={
        "bids": [{"price": "0.49", "size": "100"}, {"price": "0.48", "size": "50"}],
        "asks": [{"price": "0.51", "size": "100"}, {"price": "0.52", "size": "50"}],
    })
    bb, ba = await client.get_top_of_book("tok")
    assert bb == 0.49
    assert ba == 0.51


@pytest.mark.asyncio
async def test_get_top_of_book_empty_bids():
    client = _make_client()
    client._client.get_order_book = MagicMock(return_value={
        "bids": [],
        "asks": [{"price": "0.51", "size": "100"}],
    })
    bb, ba = await client.get_top_of_book("tok")
    assert bb is None
    assert ba == 0.51


@pytest.mark.asyncio
async def test_get_top_of_book_empty_asks():
    client = _make_client()
    client._client.get_order_book = MagicMock(return_value={
        "bids": [{"price": "0.49", "size": "100"}],
        "asks": [],
    })
    bb, ba = await client.get_top_of_book("tok")
    assert bb == 0.49
    assert ba is None


@pytest.mark.asyncio
async def test_get_top_of_book_paper_mode_short_circuits():
    """Paper / dry-run never reach FAK so the wrapper returns (None, None)
    without calling the SDK.  Defensive: production short-circuits earlier
    in ``place_limit_order``, but ``get_top_of_book`` is also exposed to
    test code so it must do its own check."""
    client = _make_client(paper=True)
    client._client.get_order_book = MagicMock()  # explode if reached
    bb, ba = await client.get_top_of_book("tok")
    assert (bb, ba) == (None, None)
    assert client._client.get_order_book.call_count == 0


# ---------------------------------------------------------------------------
# 5-6. Limit price is rewritten to cross the spread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_uses_ask_plus_tick():
    """Caller passes mid 0.50; book ask is 0.51 → wrapper submits at
    cross_price = 0.51 + 0.01 = 0.52.  Mid (0.50) is what the caller sees,
    cross (0.52) is what reaches the SDK."""
    client = _make_client()
    client._client.get_order_book = MagicMock(return_value={
        "bids": [{"price": "0.49"}],
        "asks": [{"price": "0.51"}],
    })
    client._client.create_and_post_order = MagicMock(
        return_value={"orderID": "ok", "status": "matched"},
    )
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.50, size=10.0,
    )
    assert result.success
    order_args = client._client.create_and_post_order.call_args.args[0]
    assert order_args.price == 0.52


@pytest.mark.asyncio
async def test_sell_uses_bid_minus_tick():
    """Caller passes mid 0.50; book bid is 0.49 → wrapper submits at
    cross_price = 0.49 - 0.01 = 0.48."""
    client = _make_client()
    client._client.get_order_book = MagicMock(return_value={
        "bids": [{"price": "0.49"}],
        "asks": [{"price": "0.51"}],
    })
    client._client.create_and_post_order = MagicMock(
        return_value={"orderID": "ok", "status": "matched"},
    )
    result = await client.place_limit_order(
        token_id="tok", side="SELL", price=0.50, size=10.0,
    )
    assert result.success
    order_args = client._client.create_and_post_order.call_args.args[0]
    assert order_args.price == 0.48


# ---------------------------------------------------------------------------
# 7-8. Thin liquidity (book empty on the side we'd cross) → skip + no SDK call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_thin_book_no_ask_returns_skip():
    client = _make_client()
    client._client.get_order_book = MagicMock(return_value={
        "bids": [{"price": "0.49"}],
        "asks": [],
    })
    client._client.create_and_post_order = MagicMock()  # explode if reached
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.50, size=10.0,
    )
    assert not result.success
    assert result.message == "THIN_LIQUIDITY_NO_ASK"
    assert client._client.create_and_post_order.call_count == 0


@pytest.mark.asyncio
async def test_sell_thin_book_no_bid_returns_skip():
    client = _make_client()
    client._client.get_order_book = MagicMock(return_value={
        "bids": [],
        "asks": [{"price": "0.51"}],
    })
    client._client.create_and_post_order = MagicMock()
    result = await client.place_limit_order(
        token_id="tok", side="SELL", price=0.50, size=10.0,
    )
    assert not result.success
    assert result.message == "THIN_LIQUIDITY_NO_BID"
    assert client._client.create_and_post_order.call_count == 0


# ---------------------------------------------------------------------------
# 9-10. Slippage gate — caller's mid diverges from book by > 5%
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_slippage_gate_blocks():
    """Caller mid 0.40, book ask 0.60 → cross 0.61, slip
    (0.61 - 0.40)/0.40 = 52.5% > 5% gate.  Order is not submitted; this
    catches Atlanta-style near-settled markets where last-trade is stale
    and the only remaining ask is far above."""
    client = _make_client()
    client._client.get_order_book = MagicMock(return_value={
        "bids": [{"price": "0.55"}],
        "asks": [{"price": "0.60"}],
    })
    client._client.create_and_post_order = MagicMock()
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.40, size=10.0,
    )
    assert not result.success
    assert "SLIPPAGE_TOO_HIGH" in result.message
    assert "ask=0.6000" in result.message
    assert client._client.create_and_post_order.call_count == 0


@pytest.mark.asyncio
async def test_sell_slippage_gate_blocks():
    """Caller mid 0.50, book bid 0.20 → cross 0.19, slip
    (0.50 - 0.19)/0.50 = 62% > 5%.  Order is not submitted."""
    client = _make_client()
    client._client.get_order_book = MagicMock(return_value={
        "bids": [{"price": "0.20"}],
        "asks": [{"price": "0.25"}],
    })
    client._client.create_and_post_order = MagicMock()
    result = await client.place_limit_order(
        token_id="tok", side="SELL", price=0.50, size=10.0,
    )
    assert not result.success
    assert "SLIPPAGE_TOO_HIGH" in result.message
    assert "bid=0.2000" in result.message
    assert client._client.create_and_post_order.call_count == 0


# ---------------------------------------------------------------------------
# 11-12. Price clamps at Polymarket bounds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_cross_clamped_at_price_cap_1_dollar():
    """ask 0.999 → naive cross 1.009, clamped to 1.00 (Polymarket prices
    cap at 1.0 USDC).  Caller mid 0.99 keeps slip under 5%."""
    client = _make_client()
    client._client.get_order_book = MagicMock(return_value={
        "bids": [{"price": "0.98"}],
        "asks": [{"price": "0.99"}],
    })
    client._client.create_and_post_order = MagicMock(
        return_value={"orderID": "ok", "status": "matched"},
    )
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.99, size=10.0,
    )
    assert result.success
    order_args = client._client.create_and_post_order.call_args.args[0]
    assert order_args.price == 1.00


@pytest.mark.asyncio
async def test_sell_cross_clamped_at_price_floor_1_tick():
    """bid 0.01 → naive cross 0.00 (bid - 1 tick), clamped up to 0.01
    (one-tick floor; Polymarket can't price below tick).

    Caller mid is 0.02 here — a mid of 0.01 would collide with the
    cold-start ``price <= TICK`` guard at the top of place_limit_order
    (review #4) and short-circuit before this branch runs.  The 50%
    slippage relative to bid 0.01 also exceeds the default 5% gate,
    so an explicit ``strategy_config`` with a relaxed ``max_taker_slippage``
    is needed to isolate the floor-clamp behaviour from the slippage gate.
    """
    client = _make_client()
    client._client.get_order_book = MagicMock(return_value={
        "bids": [{"price": "0.01"}],
        "asks": [{"price": "0.03"}],
    })
    client._client.create_and_post_order = MagicMock(
        return_value={"orderID": "ok", "status": "matched"},
    )
    sc = SimpleNamespace(max_taker_slippage=0.99)
    result = await client.place_limit_order(
        token_id="tok", side="SELL", price=0.02, size=10.0,
        strategy_config=sc,
    )
    assert result.success
    order_args = client._client.create_and_post_order.call_args.args[0]
    assert order_args.price == 0.01


# ---------------------------------------------------------------------------
# 13-14. ``max_taker_slippage`` resolution: explicit strategy_config wins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_taker_slippage_from_strategy_config_when_passed():
    """Explicit ``strategy_config`` with ``max_taker_slippage=0.10`` lets a
    7% slip through that the 5% default would block.  Confirms the
    parameter wins over the wrapper's hardcoded fallback.
    """
    client = _make_client()
    # mid 0.50, ask 0.53 → cross 0.54, slip = (0.54-0.50)/0.50 = 8% — over
    # the default 5% gate but inside an override of 10%.
    client._client.get_order_book = MagicMock(return_value={
        "bids": [{"price": "0.49"}],
        "asks": [{"price": "0.53"}],
    })
    client._client.create_and_post_order = MagicMock(
        return_value={"orderID": "ok", "status": "matched"},
    )
    sc = SimpleNamespace(max_taker_slippage=0.10)
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.50, size=10.0,
        strategy_config=sc,
    )
    assert result.success
    assert client._client.create_and_post_order.call_args.args[0].price == 0.54


@pytest.mark.asyncio
async def test_max_taker_slippage_default_when_no_strategy_config():
    """Without ``strategy_config`` and without a ``self._config.strategy``,
    the wrapper falls back to the hardcoded 5%.  An 8% slip is rejected.
    """
    client = _make_client()
    # ``_make_client``'s SimpleNamespace cfg has no ``strategy`` attribute,
    # so the wrapper's fallback chain stops at the hardcoded 5%.
    client._client.get_order_book = MagicMock(return_value={
        "bids": [{"price": "0.49"}],
        "asks": [{"price": "0.53"}],
    })
    client._client.create_and_post_order = MagicMock()
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.50, size=10.0,
    )
    assert not result.success
    assert "SLIPPAGE_TOO_HIGH" in result.message
    assert client._client.create_and_post_order.call_count == 0


# ---------------------------------------------------------------------------
# 15-18. Cold-start ``price <= TICK`` guard (review #4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_skipped_when_mid_below_tick_cold_start_guard():
    """A BUY signal arriving with mid 0.0 (cold-start Gamma 0 leaking
    past PriceStopGate via the 15-min position-check path) is bailed at
    the entry of place_limit_order — neither the order book nor the
    SDK's create_and_post_order are touched.
    """
    client = _make_client()
    client._client.get_order_book = MagicMock()  # explode if reached
    client._client.create_and_post_order = MagicMock()
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.0, size=10.0,
    )
    assert not result.success
    assert result.message == "PRICE_TOO_LOW_FAK_GUARD"
    assert client._client.get_order_book.call_count == 0
    assert client._client.create_and_post_order.call_count == 0


@pytest.mark.asyncio
async def test_sell_skipped_when_mid_below_tick():
    """SELL side mirrors the BUY guard."""
    client = _make_client()
    client._client.get_order_book = MagicMock()
    client._client.create_and_post_order = MagicMock()
    result = await client.place_limit_order(
        token_id="tok", side="SELL", price=0.0, size=10.0,
    )
    assert not result.success
    assert result.message == "PRICE_TOO_LOW_FAK_GUARD"
    assert client._client.get_order_book.call_count == 0
    assert client._client.create_and_post_order.call_count == 0


@pytest.mark.asyncio
async def test_buy_at_tick_boundary_skipped():
    """``<=`` is intentional: a real entry at exactly the tick floor
    (0.01) would still be sub-cent EV after the slippage gate, and we
    never want a 0-divisor in the slip ratio.
    """
    client = _make_client()
    client._client.get_order_book = MagicMock()
    client._client.create_and_post_order = MagicMock()
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.01, size=10.0,
    )
    assert not result.success
    assert result.message == "PRICE_TOO_LOW_FAK_GUARD"
    assert client._client.create_and_post_order.call_count == 0


@pytest.mark.asyncio
async def test_buy_just_above_tick_proceeds():
    """0.011 is just above the guard, so the wrapper proceeds to the
    book lookup and (with a tight enough book + relaxed slip gate) to
    the SDK.  Confirms the guard is strictly bounded — not eating
    legitimate low-price entries.
    """
    client = _make_client()
    # bid 0.01 / ask 0.02 → BUY cross = 0.03, slip = (0.03-0.011)/0.011 ≈ 173%.
    # Pass a relaxed strategy_config so the slippage gate doesn't fire and
    # we can confirm the order actually reaches the SDK.
    client._client.get_order_book = MagicMock(return_value={
        "bids": [{"price": "0.01"}],
        "asks": [{"price": "0.02"}],
    })
    client._client.create_and_post_order = MagicMock(
        return_value={"orderID": "ok", "status": "matched"},
    )
    sc = SimpleNamespace(max_taker_slippage=2.0)
    result = await client.place_limit_order(
        token_id="tok", side="BUY", price=0.011, size=10.0,
        strategy_config=sc,
    )
    assert result.success
    # ask 0.02 + 1 tick = 0.03 reaches the SDK
    assert client._client.create_and_post_order.call_args.args[0].price == 0.03
