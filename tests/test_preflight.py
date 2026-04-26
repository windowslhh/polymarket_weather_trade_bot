"""FIX-M7: preflight startup checks.

DB failures and live-mode CLOB failures must `sys.exit(2)`; webhook
failures log critical but do NOT exit.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.alerts import Alerter
from src.portfolio.store import Store
from src.preflight import (
    FEE_RATE_TOLERANCE_BPS,
    check_db_writable, check_fee_rate, check_webhook_reachable, run_preflight,
)


async def _mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    return store


@pytest.mark.asyncio
async def test_db_check_happy_path():
    store = await _mk_store()
    ok, msg = await check_db_writable(store)
    assert ok
    assert "db_writable" in msg
    await store.close()


@pytest.mark.asyncio
async def test_webhook_empty_url_skips():
    ok, msg = await check_webhook_reachable("")
    assert ok and "skipped" in msg


@pytest.mark.asyncio
async def test_webhook_non_200_reports_failure(monkeypatch):
    """A 500 response surfaces as hook_ok=False."""
    class _Resp:
        status_code = 500
    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    ok, msg = await check_webhook_reachable("http://hook.example/abc")
    assert not ok
    assert "500" in msg


@pytest.mark.asyncio
async def test_preflight_paper_skips_clob(monkeypatch):
    store = await _mk_store()
    alerter = Alerter(webhook_url="")
    alerter.send = AsyncMock()  # type: ignore[method-assign]

    clob = MagicMock()  # should never have _get_client called
    await run_preflight(
        store=store, clob_client=clob, alerter=alerter,
        webhook_url="", is_paper=True, is_dry_run=False,
    )
    # No critical alert should fire; paper mode has nothing to sanity-check
    # on the CLOB side.
    assert not any(
        call.args[0] == "critical" for call in alerter.send.call_args_list
    )
    clob._get_client.assert_not_called()
    await store.close()


@pytest.mark.asyncio
async def test_preflight_live_clob_failure_exits(monkeypatch):
    store = await _mk_store()
    alerter = Alerter(webhook_url="")
    alerter.send = AsyncMock()  # type: ignore[method-assign]

    clob = MagicMock()
    clob._get_client = MagicMock(
        return_value=SimpleNamespace(get_address=lambda: (_ for _ in ()).throw(RuntimeError("auth"))),
    )

    with pytest.raises(SystemExit) as excinfo:
        await run_preflight(
            store=store, clob_client=clob, alerter=alerter,
            webhook_url="", is_paper=False, is_dry_run=False,
        )
    assert excinfo.value.code == 2
    # Critical alert fired.
    critical_calls = [c for c in alerter.send.call_args_list if c.args[0] == "critical"]
    assert critical_calls
    await store.close()


# ──────────────────────────────────────────────────────────────────────
# FIX-2P-6 fee_rate sanity check
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_fee_rate_skipped_in_paper_mode():
    clob = MagicMock()
    ok, msg = await check_fee_rate(
        clob, "any-token", expected_rate=0.05,
        is_paper=True, is_dry_run=False,
    )
    assert ok and "skipped_non_live" in msg
    clob._get_client.assert_not_called()


@pytest.mark.asyncio
async def test_check_fee_rate_skipped_when_no_token_available():
    clob = MagicMock()
    ok, msg = await check_fee_rate(
        clob, None, expected_rate=0.05,
        is_paper=False, is_dry_run=False,
    )
    assert ok and "skipped_no_token" in msg


@pytest.mark.asyncio
async def test_check_fee_rate_passes_when_broker_matches_constant():
    clob = MagicMock()
    clob._get_client = MagicMock(
        return_value=SimpleNamespace(get_fee_rate_bps=lambda tid: 500),
    )
    ok, msg = await check_fee_rate(
        clob, "tok", expected_rate=0.05,
        is_paper=False, is_dry_run=False,
    )
    assert ok, f"expected pass, got: {msg}"
    assert "500bps" in msg


@pytest.mark.asyncio
async def test_check_fee_rate_passes_within_tolerance():
    """A few-bp drift (e.g. broker reports 505 vs constant 500) should not fire."""
    clob = MagicMock()
    clob._get_client = MagicMock(
        return_value=SimpleNamespace(
            get_fee_rate_bps=lambda tid: 500 + FEE_RATE_TOLERANCE_BPS - 1,
        ),
    )
    ok, msg = await check_fee_rate(
        clob, "tok", expected_rate=0.05,
        is_paper=False, is_dry_run=False,
    )
    assert ok, f"within-tolerance drift should not fail; got {msg}"


@pytest.mark.asyncio
async def test_check_fee_rate_fails_on_material_drift():
    """Broker reports 1000 bps (10%) vs our constant 500 bps (5%) → flag."""
    clob = MagicMock()
    clob._get_client = MagicMock(
        return_value=SimpleNamespace(get_fee_rate_bps=lambda tid: 1000),
    )
    ok, msg = await check_fee_rate(
        clob, "tok", expected_rate=0.05,
        is_paper=False, is_dry_run=False,
    )
    assert not ok
    assert "fee_rate_drift" in msg
    assert "1000bps" in msg


@pytest.mark.asyncio
async def test_check_fee_rate_swallows_clob_error():
    """A transient CLOB error must not cascade into a startup failure —
    log + alert, but caller treats it as ok=True (non-blocking)."""
    clob = MagicMock()
    clob._get_client = MagicMock(
        return_value=SimpleNamespace(
            get_fee_rate_bps=lambda tid: (_ for _ in ()).throw(RuntimeError("rpc dead")),
        ),
    )
    ok, msg = await check_fee_rate(
        clob, "tok", expected_rate=0.05,
        is_paper=False, is_dry_run=False,
    )
    assert ok, "CLOB hiccup must not block startup"
    assert "skipped_clob_error" in msg


@pytest.mark.asyncio
async def test_run_preflight_alerts_on_fee_drift_but_does_not_exit():
    store = await _mk_store()
    alerter = Alerter(webhook_url="")
    alerter.send = AsyncMock()  # type: ignore[method-assign]

    clob = MagicMock()
    clob._get_client = MagicMock(
        return_value=SimpleNamespace(
            get_address=lambda: "0xfeed",
            get_fee_rate_bps=lambda tid: 1000,  # 10% — far over 5% tolerance
        ),
    )

    async def _provider() -> str | None:
        return "weather-tok-1"

    # Must not raise SystemExit; fee drift is non-fatal by design.
    await run_preflight(
        store=store, clob_client=clob, alerter=alerter,
        webhook_url="", is_paper=False, is_dry_run=False,
        sample_token_provider=_provider,
        expected_fee_rate=0.05,
    )
    # Critical alert fired with the drift message.
    critical_calls = [
        c for c in alerter.send.call_args_list if c.args[0] == "critical"
    ]
    assert any("fee_rate_drift" in c.args[1] for c in critical_calls)
    await store.close()
