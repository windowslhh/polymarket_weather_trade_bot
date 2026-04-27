"""P-A10: ClobClient lazy-init derives API creds from the L1 key.

Covers four matrix cells of (FUNDER_ADDRESS set?) × (API creds in .env?):
- proxy + provisioned creds → signature_type=2, no derive call
- proxy + empty creds       → signature_type=2, derive called
- direct EOA + provisioned  → signature_type=0, no derive call
- direct EOA + empty        → signature_type=0, derive called

v2-2 (2026-04-27): mocks now target ``py_clob_client_v2`` since the
production code was migrated to the v2 SDK ahead of the 2026-04-28
exchange cutover.  Same test contract; only the import path /
method name (``create_or_derive_api_key``) changed.

We mock the SDK at the import sites in src.markets.clob_client._get_client
so the tests never touch the real network or filesystem-side signing material.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.markets.clob_client import ClobClient


def _stub_py_clob_client(monkeypatch):
    """Install a fake ``py_clob_client_v2`` package under sys.modules so
    ``from py_clob_client_v2 import ClobClient, ApiCreds`` inside
    ``ClobClient._get_client`` resolves to MagicMocks we control.

    Returns (clob_class_mock, instance_mock, api_creds_class) so the test
    can both inspect kwargs passed to the underlying ``ClobClient(...)``
    and assert which path (provisioned vs. derive) was taken.
    """
    instance = MagicMock(name="py_clob_client_v2_instance")
    # v2-2 (2026-04-27): SDK renamed ``create_or_derive_api_creds`` →
    # ``create_or_derive_api_key`` for the Polymarket exchange v2
    # cutover.  Stub the new name so production code resolves it.
    instance.create_or_derive_api_key.return_value = MagicMock(name="derived_creds")

    clob_class = MagicMock(name="ClobClientClass", return_value=instance)

    api_creds_class = MagicMock(name="ApiCredsClass")

    # v2-2: stub the v2 module path the production code now imports
    # from.  Top-level ``py_clob_client_v2`` package exposes ClobClient
    # and ApiCreds directly (per its __init__.py).
    pkg = types.ModuleType("py_clob_client_v2")
    pkg.__path__ = []
    pkg.ClobClient = clob_class
    pkg.ApiCreds = api_creds_class

    monkeypatch.setitem(sys.modules, "py_clob_client_v2", pkg)

    return clob_class, instance, api_creds_class


def _make_cfg(*, funder: str = "", api_key: str = "", api_secret: str = "",
              api_pass: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        dry_run=False, paper=False,
        polymarket_api_key=api_key,
        polymarket_secret=api_secret,
        polymarket_passphrase=api_pass,
        eth_private_key="0x" + "a" * 64,
        funder_address=funder,
    )


# ──────────────────────────────────────────────────────────────────────
# Signature mode (proxy vs direct EOA)
# ──────────────────────────────────────────────────────────────────────

class TestSignatureType:

    def test_funder_set_uses_signature_type_2(self, monkeypatch):
        clob_class, instance, _ = _stub_py_clob_client(monkeypatch)
        cfg = _make_cfg(funder="0x" + "1" * 40)

        ClobClient(cfg)._get_client()

        kwargs = clob_class.call_args.kwargs
        assert kwargs["signature_type"] == 2
        assert kwargs["funder"] == "0x" + "1" * 40

    def test_funder_empty_uses_signature_type_0_no_funder(self, monkeypatch):
        clob_class, _instance, _ = _stub_py_clob_client(monkeypatch)
        cfg = _make_cfg(funder="")

        ClobClient(cfg)._get_client()

        kwargs = clob_class.call_args.kwargs
        assert kwargs["signature_type"] == 0
        assert kwargs["funder"] is None

    def test_funder_whitespace_treated_as_empty(self, monkeypatch):
        """Operators copy-paste from .env — leading/trailing whitespace must
        not flip the sig mode silently."""
        clob_class, _instance, _ = _stub_py_clob_client(monkeypatch)
        cfg = _make_cfg(funder="   ")

        ClobClient(cfg)._get_client()

        kwargs = clob_class.call_args.kwargs
        assert kwargs["signature_type"] == 0
        assert kwargs["funder"] is None


# ──────────────────────────────────────────────────────────────────────
# API-creds resolution (provisioned vs. derived)
# ──────────────────────────────────────────────────────────────────────

class TestApiCredsResolution:

    def test_all_three_creds_present_uses_provisioned(self, monkeypatch):
        _clob_class, instance, api_creds_class = _stub_py_clob_client(monkeypatch)
        cfg = _make_cfg(api_key="K", api_secret="S", api_pass="P")

        ClobClient(cfg)._get_client()

        api_creds_class.assert_called_once_with(
            api_key="K", api_secret="S", api_passphrase="P",
        )
        instance.create_or_derive_api_key.assert_not_called()
        instance.set_api_creds.assert_called_once_with(api_creds_class.return_value)

    def test_all_three_creds_empty_derives(self, monkeypatch):
        _clob_class, instance, api_creds_class = _stub_py_clob_client(monkeypatch)
        cfg = _make_cfg(api_key="", api_secret="", api_pass="")

        ClobClient(cfg)._get_client()

        instance.create_or_derive_api_key.assert_called_once()
        api_creds_class.assert_not_called()
        # Whatever derive returned must be passed to set_api_creds
        instance.set_api_creds.assert_called_once_with(
            instance.create_or_derive_api_key.return_value,
        )

    @pytest.mark.parametrize("k,s,p", [
        ("K", "S", ""),
        ("K", "", "P"),
        ("", "S", "P"),
    ])
    def test_partial_creds_falls_back_to_derive(self, monkeypatch, k, s, p):
        """A half-filled .env triple is not a valid ApiCreds; either we use
        all three or we derive fresh.  The earlier code passed empty strings
        through and let py-clob-client auth fail at the first request — far
        worse than just deriving."""
        _clob_class, instance, _api_creds_class = _stub_py_clob_client(monkeypatch)
        cfg = _make_cfg(api_key=k, api_secret=s, api_pass=p)

        ClobClient(cfg)._get_client()

        instance.create_or_derive_api_key.assert_called_once()

    def test_creds_with_only_whitespace_treated_as_empty(self, monkeypatch):
        _clob_class, instance, _api_creds_class = _stub_py_clob_client(monkeypatch)
        cfg = _make_cfg(api_key="  ", api_secret=" ", api_pass="\t")

        ClobClient(cfg)._get_client()

        instance.create_or_derive_api_key.assert_called_once()


# ──────────────────────────────────────────────────────────────────────
# Derive failure modes (P-A10d)
# ──────────────────────────────────────────────────────────────────────

class TestDeriveFailureModes:
    """py-clob-client's create_or_derive_api_key has two known fail
    modes — a silent None return and a network exception.  Both must
    be handled before the bot caches a half-built client and fails
    cryptically at the first live BUY.
    """

    def test_derive_returning_none_raises_clear_error(self, monkeypatch):
        """SDK silent-fail mode: create_or_derive returns None instead of
        raising.  Without explicit None-check, we'd cache broken client
        (no creds set) and fail at first BUY with an opaque auth error.
        """
        _clob_class, instance, _ = _stub_py_clob_client(monkeypatch)
        instance.create_or_derive_api_key.return_value = None
        cfg = _make_cfg()

        client = ClobClient(cfg)
        with pytest.raises(RuntimeError, match="returned None"):
            client._get_client()

        # set_api_creds must NOT have been called with None — the guard
        # has to fire before that point or py-clob-client's internal
        # state ends up half-initialised.
        instance.set_api_creds.assert_not_called()
        # And self._client must NOT be cached on failure, so a retry
        # (e.g. preflight loop, restart) gets a fresh derive attempt.
        assert client._client is None

    def test_derive_network_error_propagates(self, monkeypatch):
        """Network failure during derive should propagate to caller —
        preflight (check_clob_reachable) catches it and refuses to start
        the scheduler.  Swallowing here would cache a half-built client
        the same way the None case does.
        """
        _clob_class, instance, _ = _stub_py_clob_client(monkeypatch)
        instance.create_or_derive_api_key.side_effect = ConnectionError(
            "CLOB unreachable",
        )
        cfg = _make_cfg()

        client = ClobClient(cfg)
        with pytest.raises(ConnectionError, match="CLOB unreachable"):
            client._get_client()

        instance.set_api_creds.assert_not_called()
        assert client._client is None


# ──────────────────────────────────────────────────────────────────────
# Lazy-init invariant
# ──────────────────────────────────────────────────────────────────────

class TestLazyInit:

    def test_init_does_not_call_py_clob_client(self, monkeypatch):
        """Constructing ClobClient must NOT touch py-clob-client.  Paper /
        dry-run paths short-circuit before _get_client(), and a constructor
        that imports py-clob-client at __init__ time would defeat that."""
        clob_class, _instance, _ = _stub_py_clob_client(monkeypatch)
        cfg = _make_cfg(funder="0x" + "1" * 40)

        ClobClient(cfg)

        clob_class.assert_not_called()

    def test_get_client_is_idempotent(self, monkeypatch):
        clob_class, instance, _ = _stub_py_clob_client(monkeypatch)
        cfg = _make_cfg()

        client = ClobClient(cfg)
        first = client._get_client()
        second = client._get_client()

        assert first is second
        # Underlying ClobClient(...) ctor + creds derive each ran exactly once.
        assert clob_class.call_count == 1
        assert instance.create_or_derive_api_key.call_count == 1


# ──────────────────────────────────────────────────────────────────────
# Combined matrix smoke
# ──────────────────────────────────────────────────────────────────────

class TestMatrixSmoke:

    @pytest.mark.parametrize("funder,creds,expected_sig,expected_derive", [
        ("0x" + "1" * 40, ("K", "S", "P"), 2, False),  # proxy + provisioned
        ("0x" + "1" * 40, ("",  "",  ""),  2, True),   # proxy + derive
        ("",              ("K", "S", "P"), 0, False),  # EOA   + provisioned
        ("",              ("",  "",  ""),  0, True),   # EOA   + derive
    ])
    def test_matrix(self, monkeypatch, funder, creds, expected_sig, expected_derive):
        clob_class, instance, _ = _stub_py_clob_client(monkeypatch)
        cfg = _make_cfg(
            funder=funder, api_key=creds[0], api_secret=creds[1], api_pass=creds[2],
        )

        ClobClient(cfg)._get_client()

        kwargs = clob_class.call_args.kwargs
        assert kwargs["signature_type"] == expected_sig
        if expected_derive:
            instance.create_or_derive_api_key.assert_called_once()
        else:
            instance.create_or_derive_api_key.assert_not_called()
