"""FIX-11: kill switch — `/api/admin/pause` / `/api/admin/unpause`
flip a persistent flag that the rebalancer honours by suppressing BUYs.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.portfolio.store import Store
from src.web.app import create_app


async def _mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    s = Store(tmp)
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_default_paused_is_false():
    s = await _mk_store()
    assert await s.get_bot_paused() is False
    await s.close()


@pytest.mark.asyncio
async def test_set_and_get_paused():
    s = await _mk_store()
    await s.set_bot_paused(True)
    assert await s.get_bot_paused() is True
    await s.set_bot_paused(False)
    assert await s.get_bot_paused() is False
    await s.close()


@pytest.mark.asyncio
async def test_flag_survives_restart():
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    s1 = Store(tmp)
    await s1.initialize()
    await s1.set_bot_paused(True)
    await s1.close()

    s2 = Store(tmp)
    await s2.initialize()
    assert await s2.get_bot_paused() is True
    await s2.close()


def _make_flask_app(store, secret: str = "") -> "flask.Flask":
    cfg = SimpleNamespace(
        trigger_secret=secret,
        scheduling=SimpleNamespace(rebalance_interval_minutes=60),
        cities=[],
    )
    # Rebalancer isn't exercised here; any truthy MagicMock works.
    return create_app(store=store, rebalancer=MagicMock(), config=cfg)


@pytest.mark.asyncio
async def test_pause_endpoint_requires_secret_when_set():
    """With TRIGGER_SECRET configured, unauthenticated POST is rejected."""
    s = await _mk_store()
    app = _make_flask_app(s, secret="S3CRET")
    client = app.test_client()
    r = client.post("/api/admin/pause")
    assert r.status_code == 401
    # Flag untouched.
    assert await s.get_bot_paused() is False
    await s.close()


@pytest.mark.asyncio
async def test_pause_endpoint_accepts_bearer_header():
    s = await _mk_store()
    app = _make_flask_app(s, secret="S3CRET")
    client = app.test_client()
    r = client.post(
        "/api/admin/pause", headers={"Authorization": "Bearer S3CRET"},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body == {"ok": True, "paused": True}
    assert await s.get_bot_paused() is True
    await s.close()


@pytest.mark.asyncio
async def test_pause_endpoint_accepts_x_trigger_secret_header():
    s = await _mk_store()
    app = _make_flask_app(s, secret="S3CRET")
    client = app.test_client()
    r = client.post(
        "/api/admin/pause", headers={"X-Trigger-Secret": "S3CRET"},
    )
    assert r.status_code == 200
    assert await s.get_bot_paused() is True
    await s.close()


@pytest.mark.asyncio
async def test_unpause_endpoint():
    s = await _mk_store()
    await s.set_bot_paused(True)
    # Blocker 5 (review): empty secret no longer fail-opens.  Set a
    # secret + send the header so this test exercises the unpause
    # response shape, not auth bypass.
    app = _make_flask_app(s, secret="UNPAUSE_SECRET")
    client = app.test_client()
    r = client.post(
        "/api/admin/unpause",
        headers={"Authorization": "Bearer UNPAUSE_SECRET"},
    )
    assert r.status_code == 200
    assert (await s.get_bot_paused()) is False
    await s.close()


@pytest.mark.asyncio
async def test_wrong_secret_rejected():
    s = await _mk_store()
    app = _make_flask_app(s, secret="correct")
    client = app.test_client()
    r = client.post(
        "/api/admin/pause", headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401
    await s.close()


# ── Blocker 5: empty TRIGGER_SECRET semantics ────────────────────────


@pytest.mark.asyncio
async def test_empty_secret_returns_503_by_default(monkeypatch):
    """No TRIGGER_SECRET + no ADMIN_NOAUTH opt-in + paper=False (live) →
    admin endpoint must refuse with 503, not fail-open."""
    monkeypatch.delenv("ADMIN_NOAUTH", raising=False)
    s = await _mk_store()
    cfg = SimpleNamespace(
        trigger_secret="",
        scheduling=SimpleNamespace(rebalance_interval_minutes=60),
        cities=[], paper=False, dry_run=False,
    )
    from src.web.app import create_app as _create
    app = _create(store=s, rebalancer=MagicMock(), config=cfg)
    client = app.test_client()
    r = client.post("/api/admin/pause")
    assert r.status_code == 503
    body = r.get_json()
    assert "TRIGGER_SECRET not configured" in body["error"]
    # Pause flag is untouched.
    assert (await s.get_bot_paused()) is False
    await s.close()


@pytest.mark.asyncio
async def test_empty_secret_admin_noauth_only_in_dev_mode(monkeypatch):
    """With ADMIN_NOAUTH=1 + paper=True, no-secret allow goes through;
    with ADMIN_NOAUTH=1 + paper=False, it still 503's."""
    s = await _mk_store()

    # Dev mode: paper=True, opt-in flag set → allowed.
    monkeypatch.setenv("ADMIN_NOAUTH", "1")
    cfg = SimpleNamespace(
        trigger_secret="",
        scheduling=SimpleNamespace(rebalance_interval_minutes=60),
        cities=[], paper=True, dry_run=False,
    )
    from src.web.app import create_app as _create
    app = _create(store=s, rebalancer=MagicMock(), config=cfg)
    client = app.test_client()
    r = client.post("/api/admin/pause")
    assert r.status_code == 200
    assert (await s.get_bot_paused()) is True

    # Live mode: paper=False, opt-in flag still set → STILL 503.
    cfg2 = SimpleNamespace(
        trigger_secret="",
        scheduling=SimpleNamespace(rebalance_interval_minutes=60),
        cities=[], paper=False, dry_run=False,
    )
    app2 = _create(store=s, rebalancer=MagicMock(), config=cfg2)
    client2 = app2.test_client()
    r2 = client2.post("/api/admin/unpause")
    assert r2.status_code == 503, "Live mode must never honour ADMIN_NOAUTH"
    # Pause flag from the dev-mode call survives because unpause was rejected.
    assert (await s.get_bot_paused()) is True
    await s.close()
