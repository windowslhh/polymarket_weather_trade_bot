"""G-4 (2026-04-26): wallet balance + nonce monitor.

Two checks:
  - USDC balance via SDK get_balance_allowance — critical alert below floor
  - Nonce via Polygon RPC eth_getTransactionCount — info log only (history)

Skipped in paper / dry-run.  Wired as 60-min APScheduler job + once 10s
after startup so the first reading lands in operator logs immediately.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.monitoring.wallet import (
    check_signer_nonce,
    check_wallet_balance,
    run_wallet_monitor,
)


# ──────────────────────────────────────────────────────────────────────
# check_wallet_balance
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_wallet_balance_returns_usd():
    """SDK returns balance as integer base units (6 decimals); divide by 1e6."""
    clob = MagicMock()
    clob._get_client = MagicMock(
        return_value=SimpleNamespace(
            get_balance_allowance=lambda params: {
                "balance": "12345678", "allowance": "9999",
            },
        ),
    )
    ok, balance, msg = await check_wallet_balance(clob)
    assert ok
    assert abs(balance - 12.345678) < 1e-6
    assert "12.35 USDC" in msg


@pytest.mark.asyncio
async def test_check_wallet_balance_handles_sdk_error():
    """SDK raises → ok=False, balance=None, error message included."""
    clob = MagicMock()
    clob._get_client = MagicMock(
        return_value=SimpleNamespace(
            get_balance_allowance=lambda params: (_ for _ in ()).throw(
                RuntimeError("api 503"),
            ),
        ),
    )
    ok, balance, msg = await check_wallet_balance(clob)
    assert not ok
    assert balance is None
    assert "api 503" in msg


@pytest.mark.asyncio
async def test_check_wallet_balance_handles_unexpected_response_shape():
    clob = MagicMock()
    clob._get_client = MagicMock(
        return_value=SimpleNamespace(
            get_balance_allowance=lambda params: {"unexpected": "shape"},
        ),
    )
    ok, balance, msg = await check_wallet_balance(clob)
    assert not ok
    assert balance is None
    assert "unexpected" in msg.lower() or "balance" in msg.lower()


# ──────────────────────────────────────────────────────────────────────
# check_signer_nonce
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_signer_nonce_returns_int(monkeypatch):
    """Polygon RPC returns hex-encoded nonce; we decode to int."""
    class _Resp:
        def raise_for_status(self): pass
        def json(self_): return {"jsonrpc": "2.0", "id": 1, "result": "0x2a"}

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    ok, nonce, msg = await check_signer_nonce(
        "https://example.com", "0xfeedface",
    )
    assert ok
    assert nonce == 42  # 0x2a
    assert "42" in msg


@pytest.mark.asyncio
async def test_check_signer_nonce_handles_rpc_error(monkeypatch):
    class _Resp:
        def raise_for_status(self): pass
        def json(self_): return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}}

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    ok, nonce, msg = await check_signer_nonce(
        "https://example.com", "0xfeedface",
    )
    assert not ok
    assert nonce is None


@pytest.mark.asyncio
async def test_check_signer_nonce_handles_network_error(monkeypatch):
    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw):
            raise httpx.ConnectError("dns dead")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    ok, nonce, msg = await check_signer_nonce(
        "https://example.com", "0xfeedface",
    )
    assert not ok
    assert nonce is None
    assert "polygon RPC unreachable" in msg


@pytest.mark.asyncio
async def test_check_signer_nonce_empty_address():
    ok, nonce, msg = await check_signer_nonce("https://example.com", "")
    assert not ok
    assert "no signer address" in msg


# ──────────────────────────────────────────────────────────────────────
# run_wallet_monitor (combined, with alerter)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_wallet_monitor_paper_skipped():
    """G-4: paper / dry-run mode is a no-op."""
    clob = MagicMock()
    alerter = AsyncMock()
    await run_wallet_monitor(
        clob, alerter,
        rpc_url="https://example.com", min_balance_usd=50.0,
        is_paper=True, is_dry_run=False,
    )
    alerter.send.assert_not_called()
    clob._get_client.assert_not_called()


@pytest.mark.asyncio
async def test_run_wallet_monitor_dry_run_skipped():
    clob = MagicMock()
    alerter = AsyncMock()
    await run_wallet_monitor(
        clob, alerter,
        rpc_url="https://example.com", min_balance_usd=50.0,
        is_paper=False, is_dry_run=True,
    )
    alerter.send.assert_not_called()


@pytest.mark.asyncio
async def test_run_wallet_monitor_alerts_on_low_balance(monkeypatch):
    """G-4: balance below floor → critical alert."""
    clob = MagicMock()
    clob._get_client = MagicMock(
        return_value=SimpleNamespace(
            get_balance_allowance=lambda params: {
                "balance": "20000000",  # $20 USDC
                "allowance": "0",
            },
            get_address=lambda: "0xfeed",
        ),
    )
    alerter = AsyncMock()

    # Stub Polygon RPC so the nonce check doesn't try real network
    class _Resp:
        def raise_for_status(self): pass
        def json(self_): return {"result": "0x1"}
    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw): return _Resp()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())

    await run_wallet_monitor(
        clob, alerter,
        rpc_url="https://example.com", min_balance_usd=50.0,
        is_paper=False, is_dry_run=False,
    )

    # Critical alert fired with the low-balance message
    critical_calls = [
        c for c in alerter.send.call_args_list
        if c.args and c.args[0] == "critical"
    ]
    assert critical_calls
    assert any("below floor" in c.args[1] for c in critical_calls)


@pytest.mark.asyncio
async def test_run_wallet_monitor_alerts_on_balance_fetch_failure(monkeypatch):
    """G-4: SDK error on balance fetch → critical alert."""
    clob = MagicMock()
    clob._get_client = MagicMock(
        return_value=SimpleNamespace(
            get_balance_allowance=lambda params: (_ for _ in ()).throw(
                RuntimeError("clob 502"),
            ),
            get_address=lambda: "0xfeed",
        ),
    )
    alerter = AsyncMock()

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw):
            class R:
                def raise_for_status(self): pass
                def json(self_): return {"result": "0x1"}
            return R()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())

    await run_wallet_monitor(
        clob, alerter,
        rpc_url="https://example.com", min_balance_usd=50.0,
        is_paper=False, is_dry_run=False,
    )

    critical_calls = [
        c for c in alerter.send.call_args_list
        if c.args and c.args[0] == "critical"
    ]
    assert any("balance check FAILED" in c.args[1] for c in critical_calls)


@pytest.mark.asyncio
async def test_run_wallet_monitor_no_alert_on_healthy_balance(monkeypatch):
    """Sanity: balance above floor + nonce fetch ok → no critical alerts."""
    clob = MagicMock()
    clob._get_client = MagicMock(
        return_value=SimpleNamespace(
            get_balance_allowance=lambda params: {
                "balance": "200000000", "allowance": "0",  # $200
            },
            get_address=lambda: "0xfeed",
        ),
    )
    alerter = AsyncMock()

    class _Resp:
        def raise_for_status(self): pass
        def json(self_): return {"result": "0x42"}
    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw): return _Resp()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())

    await run_wallet_monitor(
        clob, alerter,
        rpc_url="https://example.com", min_balance_usd=50.0,
        is_paper=False, is_dry_run=False,
    )

    critical_calls = [
        c for c in alerter.send.call_args_list
        if c.args and c.args[0] == "critical"
    ]
    assert not critical_calls
