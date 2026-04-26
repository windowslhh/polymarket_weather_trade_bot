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
    check_db_writable, check_webhook_reachable, run_preflight,
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
