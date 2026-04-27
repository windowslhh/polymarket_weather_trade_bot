"""Tests for src.markets.gamma_prices.refresh_gamma_prices_only.

The helper is the cheap-Gamma-batch path used by both position_check
(held tokens) and the new entry scan (active-event tokens).  Behaviour
contract: best-effort, partial-success, swallows per-batch errors,
returns ``{token_id: float}``.
"""
from __future__ import annotations

import json as _json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.markets.gamma_prices import (
    DEFAULT_BATCH_SIZE,
    refresh_gamma_prices_only,
)


def _make_mkt(token_ids: list[str], prices: list[str]) -> dict:
    """Gamma /markets payload shape: clobTokenIds + outcomePrices as
    JSON-encoded strings (not lists)."""
    return {
        "clobTokenIds": _json.dumps(token_ids),
        "outcomePrices": _json.dumps(prices),
    }


def _stub_client(payloads: list):
    """Build an AsyncClient context-manager mock whose .get() returns
    the next item from ``payloads`` each call.  Each item can be a dict
    (parsed JSON), a list, or an Exception to raise."""
    responses = []
    for p in payloads:
        if isinstance(p, Exception):
            responses.append(p)
            continue
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=p)
        responses.append(resp)

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    async def _get(*args, **kwargs):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client.get = _get
    return client


@pytest.mark.asyncio
async def test_empty_input_returns_empty():
    out = await refresh_gamma_prices_only([])
    assert out == {}


@pytest.mark.asyncio
async def test_single_market_returns_token_to_price():
    payload = [_make_mkt(["t_yes", "t_no"], ["0.3", "0.7"])]
    with patch("src.markets.gamma_prices.httpx.AsyncClient",
               return_value=_stub_client([payload])):
        out = await refresh_gamma_prices_only(["t_yes", "t_no"])
    assert out == {"t_yes": 0.3, "t_no": 0.7}


@pytest.mark.asyncio
async def test_batches_at_default_size():
    """22 tokens → 2 batches at default size 20."""
    tokens = [f"tok_{i}" for i in range(22)]
    # Each batch returns one market with two of the requested tokens —
    # we only care that there are 2 calls.
    payloads = [
        [_make_mkt(tokens[:20], ["0.5"] * 20)],   # 20-token batch
        [_make_mkt(tokens[20:22], ["0.5"] * 2)],  # 2-token batch
    ]
    client = _stub_client(payloads)
    with patch("src.markets.gamma_prices.httpx.AsyncClient", return_value=client):
        out = await refresh_gamma_prices_only(tokens)
    assert len(out) == 22
    assert all(0.0 <= v <= 1.0 for v in out.values())


@pytest.mark.asyncio
async def test_per_batch_failure_is_swallowed_and_partial_returned():
    """First batch raises; second batch succeeds.  Helper returns the
    second batch's results — does not propagate the first batch's error."""
    tokens = [f"tok_{i}" for i in range(40)]
    payloads = [
        Exception("HTTP 500"),                     # batch 0 fails
        [_make_mkt(tokens[20:40], ["0.4"] * 20)],  # batch 1 succeeds
    ]
    client = _stub_client(payloads)
    with patch("src.markets.gamma_prices.httpx.AsyncClient", return_value=client):
        out = await refresh_gamma_prices_only(tokens)
    # Only the second batch's tokens come back.
    assert len(out) == 20
    assert all(k.startswith("tok_") and 20 <= int(k[4:]) < 40 for k in out)


@pytest.mark.asyncio
async def test_garbage_price_value_skipped_not_zeroed():
    """Gamma occasionally returns "" or null for outcomePrices.  Helper
    should NOT coerce those to 0.0 — it should omit the entry so the
    caller's existing cache stays canonical for that token."""
    payload = [_make_mkt(["t_a", "t_b"], ["0.6", ""])]
    with patch("src.markets.gamma_prices.httpx.AsyncClient",
               return_value=_stub_client([payload])):
        out = await refresh_gamma_prices_only(["t_a", "t_b"])
    assert out == {"t_a": 0.6}
    assert "t_b" not in out  # NOT 0.0


@pytest.mark.asyncio
async def test_non_list_response_skipped():
    """Some error states return a dict (e.g. {"error": "..."}) instead
    of a list.  Helper should not crash."""
    with patch("src.markets.gamma_prices.httpx.AsyncClient",
               return_value=_stub_client([{"error": "bad"}])):
        out = await refresh_gamma_prices_only(["t_a"])
    assert out == {}


@pytest.mark.asyncio
async def test_batch_size_override():
    """Custom batch_size respected — 10 tokens with batch_size=5 → 2 calls."""
    tokens = [f"tok_{i}" for i in range(10)]
    call_count = 0

    async def _counting_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=[
            _make_mkt(tokens[(call_count - 1) * 5:call_count * 5], ["0.5"] * 5),
        ])
        return resp

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = _counting_get

    with patch("src.markets.gamma_prices.httpx.AsyncClient", return_value=client):
        await refresh_gamma_prices_only(tokens, batch_size=5)
    assert call_count == 2


def test_module_constants_sane():
    """Defaults are documented + don't drift accidentally."""
    assert DEFAULT_BATCH_SIZE == 20
    # Other module constants live in gamma_prices itself; just confirm
    # the public surface stays minimal.
    from src.markets import gamma_prices
    public = [n for n in dir(gamma_prices) if not n.startswith("_")]
    # Allow only the two explicit public names + stdlib re-exports we need.
    expected = {"refresh_gamma_prices_only", "DEFAULT_BATCH_SIZE",
                "DEFAULT_TIMEOUT_S", "annotations", "logger",
                "logging", "json", "httpx"}
    unexpected = set(public) - expected
    assert not unexpected, f"unexpected public names: {unexpected}"
