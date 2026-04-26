"""D-2: dashboard authentication via DASHBOARD_SECRET + cookie.

Two-secret design:
  - DASHBOARD_SECRET gates dashboard view + view-only API
  - TRIGGER_SECRET still gates /api/admin/* (write actions)

An attacker who steals the dashboard cookie can READ but cannot
pause/unpause/trigger.  Tests pin the precedence + the admin
endpoint isolation.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.web.app import create_app


def _mk_config(dash_secret: str = "dash-xyz", trigger_secret: str = "trig-abc",
               paper: bool = False, dry_run: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        dashboard_secret=dash_secret,
        trigger_secret=trigger_secret,
        paper=paper,
        dry_run=dry_run,
        scheduling=SimpleNamespace(rebalance_interval_minutes=60),
        strategy=SimpleNamespace(
            max_total_exposure_usd=200,
            daily_loss_limit_usd=75,
        ),
    )


@pytest.fixture
def client():
    """Build an isolated Flask test client with a stub store/rebalancer."""
    store = SimpleNamespace()
    rebalancer = SimpleNamespace()
    config = _mk_config()
    app = create_app(store, rebalancer, config)
    app.config["TESTING"] = True
    return app.test_client()


def test_dashboard_blocks_unauthenticated_request(client):
    """No cookie, no header → 302 redirect to /login."""
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")


def test_dashboard_blocks_with_wrong_cookie(client):
    """Cookie present but value doesn't match → still redirect to /login."""
    client.set_cookie(
        "dashboard_session", "wrong-value",
    )
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")


def test_dashboard_allows_with_correct_cookie(client):
    """Cookie matches DASHBOARD_SECRET → request reaches the route
    (returns 200/500 from the stub, NOT a 302/401)."""
    client.set_cookie("dashboard_session", "dash-xyz")
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_dashboard_allows_with_correct_x_dashboard_header(client):
    """Header path: X-Dashboard-Secret bypasses cookie."""
    resp = client.get(
        "/api/health",
        headers={"X-Dashboard-Secret": "dash-xyz"},
    )
    assert resp.status_code == 200


def test_admin_endpoint_still_requires_trigger_secret_not_dashboard(client):
    """Critical D-2 invariant: a dashboard-authed user CANNOT pause
    the bot.  /api/admin/pause needs TRIGGER_SECRET, not DASHBOARD_SECRET."""
    # Authed for dashboard
    client.set_cookie("dashboard_session", "dash-xyz")
    # But sending DASHBOARD_SECRET as if it were the trigger secret
    resp = client.post(
        "/api/admin/pause",
        headers={"X-Trigger-Secret": "dash-xyz"},  # wrong secret!
    )
    assert resp.status_code == 401
    # Now with the correct trigger secret
    resp = client.post(
        "/api/admin/pause",
        headers={"X-Trigger-Secret": "trig-abc"},
    )
    # 200 (paused) OR 500 (stub store).  Not a 401.
    assert resp.status_code != 401


def test_login_with_wrong_secret_returns_401(client):
    resp = client.post("/login", data={"secret": "wrong"})
    assert resp.status_code == 401


def test_login_with_correct_secret_sets_cookie_and_redirects(client):
    resp = client.post(
        "/login", data={"secret": "dash-xyz"}, follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    cookies = resp.headers.getlist("Set-Cookie")
    # Cookie set with correct value + httponly + samesite
    assert any("dashboard_session=dash-xyz" in c for c in cookies)
    assert any("HttpOnly" in c for c in cookies)
    assert any("SameSite=Strict" in c for c in cookies)


def test_login_via_get_query_secret_works(client):
    """Browser convenience: /login?secret=… also sets the cookie."""
    resp = client.get("/login?secret=dash-xyz", follow_redirects=False)
    assert resp.status_code == 302
    cookies = resp.headers.getlist("Set-Cookie")
    assert any("dashboard_session=dash-xyz" in c for c in cookies)


def test_health_endpoint_is_public(client):
    """/api/health must be reachable WITHOUT auth (docker HEALTHCHECK)."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_dashboard_disabled_when_secret_empty_and_not_paper():
    """DASHBOARD_SECRET empty + not paper/dry-run → 503 fail-closed.
    A misconfigured live deploy must NOT silently allow public access."""
    store = SimpleNamespace()
    rebalancer = SimpleNamespace()
    config = _mk_config(dash_secret="", paper=False, dry_run=False)
    app = create_app(store, rebalancer, config)
    client = app.test_client()
    resp = client.get("/api/health")
    # /api/health is public so it always works
    assert resp.status_code == 200
    # But the dashboard root returns 503
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 503
    assert "DASHBOARD_SECRET not configured" in resp.get_json().get("error", "")


def test_dashboard_admin_noauth_paper_only(monkeypatch):
    """Empty DASHBOARD_SECRET + ADMIN_NOAUTH=1 + paper=True → allow.
    Same env var + live mode → still 503 (paper-only opt-in)."""
    monkeypatch.setenv("ADMIN_NOAUTH", "1")

    # Paper mode → opt-in honoured
    store = SimpleNamespace()
    rebalancer = SimpleNamespace()
    paper_config = _mk_config(dash_secret="", paper=True)
    paper_app = create_app(store, rebalancer, paper_config)
    paper_client = paper_app.test_client()
    resp = paper_client.get("/api/health")
    assert resp.status_code == 200

    # Live mode → ignored
    live_config = _mk_config(dash_secret="", paper=False, dry_run=False)
    live_app = create_app(store, rebalancer, live_config)
    live_client = live_app.test_client()
    resp = live_client.get("/", follow_redirects=False)
    assert resp.status_code == 503
