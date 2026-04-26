"""C-7: confirm _admin_auth_check is consistently named and the
ADMIN_NOAUTH paper/dry-run-only semantic is documented in source.

The existing test_kill_switch.py already exercises the four authorization
states in practice; this test pins the SOURCE-LEVEL invariants the
docstring promises so future edits can't silently drop them.
"""
from __future__ import annotations

import inspect
from pathlib import Path


_APP_PY = Path(__file__).resolve().parents[1] / "src" / "web" / "app.py"


def test_admin_auth_check_function_name_used_consistently() -> None:
    """C-7: every admin endpoint must call `_admin_auth_check()`.  A
    rename / typo would silently bypass auth on the affected endpoint."""
    body = _APP_PY.read_text()
    # The function definition exists.
    assert "def _admin_auth_check():" in body, (
        "C-7: _admin_auth_check function must be defined in src/web/app.py"
    )
    # Every admin route invokes it.  Pre-fix the routes used a mix of
    # _admin_auth_check / _check_admin / different variants.
    admin_endpoints = ["api_admin_pause", "api_admin_unpause", "api_trigger"]
    for ep in admin_endpoints:
        # Find the function body of each endpoint
        marker = f"def {ep}("
        pos = body.find(marker)
        assert pos >= 0, f"missing endpoint definition: {ep}"
        # Look at the next ~20 lines for the auth call
        snippet = body[pos:pos + 800]
        assert "_admin_auth_check()" in snippet, (
            f"C-7: endpoint {ep} must call _admin_auth_check() — pre-fix "
            f"some endpoints used different (or no) auth functions"
        )


def test_admin_auth_docstring_documents_paper_only_admin_noauth() -> None:
    """C-7: the docstring must explicitly call out that ADMIN_NOAUTH=1
    is honoured ONLY in paper/dry-run mode.  An operator inheriting
    the codebase shouldn't have to grep the implementation to learn
    that the env var is silently ignored in live."""
    body = _APP_PY.read_text()
    assert "ADMIN_NOAUTH" in body
    # Three load-bearing phrases in the docstring matrix:
    assert "honoured when the bot is" in body, (
        "C-7: docstring must explicitly state ADMIN_NOAUTH is paper-only"
    )
    assert "live mode" in body and "ignored" in body, (
        "C-7: docstring must say the env var is ignored in live mode"
    )


def test_admin_auth_implementation_enforces_paper_dry_run_gate() -> None:
    """C-7: the implementation must read both `paper` and `dry_run` from
    config and require either to be true before honouring ADMIN_NOAUTH."""
    body = _APP_PY.read_text()
    # Look for the gate condition
    assert "ADMIN_NOAUTH" in body
    assert 'getattr(cfg, "paper"' in body
    assert 'getattr(cfg, "dry_run"' in body
    assert "is_non_prod" in body or "is_paper_or_dry" in body, (
        "C-7: implementation must combine paper + dry_run into a single "
        "is_non_prod check before honouring ADMIN_NOAUTH"
    )


def test_admin_auth_uses_constant_time_compare() -> None:
    """C-7 sanity: secret comparison must use hmac.compare_digest to
    defeat timing oracles.  Plain `==` would leak via response-time."""
    body = _APP_PY.read_text()
    assert "hmac.compare_digest" in body, (
        "C-7: admin auth must use hmac.compare_digest for the secret check"
    )
