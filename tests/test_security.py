"""Tests for src.security — Keychain → env → raise loader."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from src.security import (
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE,
    _check_format,
    load_eth_private_key,
    load_key_from_keychain,
)

VALID_KEY = "0x" + "a" * 64
VALID_KEY_NO_PREFIX = "a" * 64
INVALID_SHORT = "0xabc"
INVALID_HEX = "0x" + "z" * 64


class TestFormatCheck:

    def test_valid_with_0x_prefix(self):
        assert _check_format(VALID_KEY) is True

    def test_valid_without_prefix(self):
        assert _check_format(VALID_KEY_NO_PREFIX) is True

    def test_too_short_rejected(self):
        assert _check_format(INVALID_SHORT) is False

    def test_non_hex_rejected(self):
        assert _check_format(INVALID_HEX) is False

    def test_empty_rejected(self):
        assert _check_format("") is False

    def test_uppercase_accepted(self):
        assert _check_format("0x" + "A" * 64) is True

    def test_whitespace_stripped(self):
        assert _check_format(f"  {VALID_KEY}\n") is True


class TestLoadKeyFromKeychain:

    def test_non_macos_returns_none(self):
        with patch("src.security.sys.platform", "linux"):
            assert load_key_from_keychain() is None

    def test_keychain_hit_returns_key(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=VALID_KEY + "\n", stderr="",
        )
        with patch("src.security.sys.platform", "darwin"), \
             patch("src.security.subprocess.run", return_value=result) as run:
            key = load_key_from_keychain()
        assert key == VALID_KEY
        assert run.call_count == 1
        cmd = run.call_args[0][0]
        assert "find-generic-password" in cmd
        assert KEYCHAIN_SERVICE in cmd
        assert KEYCHAIN_ACCOUNT in cmd

    def test_keychain_miss_returns_none(self):
        err = subprocess.CalledProcessError(
            returncode=44, cmd=["security", "find-generic-password"],
            stderr="The specified item could not be found",
        )
        with patch("src.security.sys.platform", "darwin"), \
             patch("src.security.subprocess.run", side_effect=err):
            assert load_key_from_keychain() is None

    def test_keychain_returns_garbage_returns_none(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not-a-real-key\n", stderr="",
        )
        with patch("src.security.sys.platform", "darwin"), \
             patch("src.security.subprocess.run", return_value=result):
            assert load_key_from_keychain() is None


class TestLoadEthPrivateKey:

    def test_keychain_first(self, monkeypatch):
        monkeypatch.setenv("ETH_PRIVATE_KEY", "0x" + "b" * 64)
        with patch("src.security.load_key_from_keychain", return_value=VALID_KEY):
            assert load_eth_private_key() == VALID_KEY

    def test_env_fallback_when_keychain_miss(self, monkeypatch):
        monkeypatch.setenv("ETH_PRIVATE_KEY", VALID_KEY)
        with patch("src.security.load_key_from_keychain", return_value=None):
            assert load_eth_private_key() == VALID_KEY

    def test_env_fallback_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("ETH_PRIVATE_KEY", f"  {VALID_KEY}\n")
        with patch("src.security.load_key_from_keychain", return_value=None):
            assert load_eth_private_key() == VALID_KEY

    def test_env_with_invalid_format_falls_through(self, monkeypatch):
        monkeypatch.setenv("ETH_PRIVATE_KEY", "garbage")
        with patch("src.security.load_key_from_keychain", return_value=None):
            with pytest.raises(RuntimeError) as exc:
                load_eth_private_key()
        assert "Keychain" in str(exc.value)
        assert KEYCHAIN_SERVICE in str(exc.value)
        assert KEYCHAIN_ACCOUNT in str(exc.value)

    def test_both_missing_raises_with_helpful_hint(self, monkeypatch):
        monkeypatch.delenv("ETH_PRIVATE_KEY", raising=False)
        with patch("src.security.load_key_from_keychain", return_value=None):
            with pytest.raises(RuntimeError) as exc:
                load_eth_private_key()
        msg = str(exc.value)
        assert "security add-generic-password" in msg
        assert KEYCHAIN_SERVICE in msg
        assert "--paper" in msg or "--dry-run" in msg

    def test_empty_env_raises(self, monkeypatch):
        monkeypatch.setenv("ETH_PRIVATE_KEY", "")
        with patch("src.security.load_key_from_keychain", return_value=None):
            with pytest.raises(RuntimeError):
                load_eth_private_key()


class TestMainLiveModeKeychainOverride:
    """Regression test for P-A8: in live mode, src.main must call
    load_eth_private_key() *unconditionally* and assign the result to
    config.eth_private_key — even when .env already populated it.

    Source-level checks are brittle but cheap; the alternative is
    spinning up the full async run() with mocks for Store / Alerter /
    scheduler / preflight / etc., which is far more code for the same
    invariant: the Keychain entry is the source of truth on a Mac
    with it set up.
    """

    def test_live_branch_does_not_guard_on_existing_key(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "src" / "main.py").read_text()

        # Locate the live branch.
        marker = "if not config.dry_run and not config.paper:"
        assert marker in src, "live-mode branch in src/main.py is missing"

        live_block = src.split(marker, 1)[1].split("\n\n", 1)[0]

        # The pre-P-A8 code wrapped the call in:
        #   if not config.eth_private_key:
        #       config.eth_private_key = load_eth_private_key()
        # which let an .env value short-circuit Keychain.  P-A8 removes
        # that guard, so the call must be unconditional.
        assert "if not config.eth_private_key" not in live_block, (
            "live-mode branch must call load_eth_private_key() unconditionally "
            "(P-A8 regression — .env should not short-circuit Keychain)"
        )
        assert "load_eth_private_key()" in live_block, (
            "live-mode branch must call load_eth_private_key()"
        )
        assert "config.eth_private_key = load_eth_private_key()" in live_block, (
            "result of load_eth_private_key() must be assigned to config.eth_private_key"
        )
