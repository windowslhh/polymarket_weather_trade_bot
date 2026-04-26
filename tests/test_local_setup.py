"""Smoke tests for the local-native deployment artifacts.

Static checks only — no shell execution, no network, no Keychain access.
Verifies the scripts are wired up correctly so a missing chmod or a typo
in the launchd plist is caught at PR time, not at deploy time.
"""
from __future__ import annotations

import os
import plistlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_setup_local_script_exists_and_executable():
    p = ROOT / "scripts" / "setup_local.sh"
    assert p.is_file(), "scripts/setup_local.sh is missing"
    assert os.access(p, os.X_OK), "scripts/setup_local.sh is not executable"


def test_run_local_script_exists_and_executable():
    p = ROOT / "scripts" / "run_local.sh"
    assert p.is_file(), "scripts/run_local.sh is missing"
    assert os.access(p, os.X_OK), "scripts/run_local.sh is not executable"


def test_setup_local_calls_load_eth_private_key():
    """setup_local.sh must prove the Keychain entry resolves before
    handing control over to the user — otherwise launchd starts a job
    that immediately exits on first cycle."""
    text = (ROOT / "scripts" / "setup_local.sh").read_text()
    assert "load_eth_private_key" in text


def test_setup_local_does_not_print_key_suffix():
    """The setup script must NOT print any portion of the raw private
    key — even the last 6 hex chars are enough material to start
    brute-forcing in adversarial scenarios.  Use the SHA-256 fingerprint
    from src.security._fingerprint instead."""
    text = (ROOT / "scripts" / "setup_local.sh").read_text()
    # No raw key slicing in the keychain-probe block
    assert "key[-6:]" not in text
    assert "key[-4:]" not in text
    assert "key[:6]" not in text
    assert "key[:8]" not in text
    # Use the audited helper that emits sha256:xxxxxxxx...xxxx
    assert "_fingerprint" in text


def test_setup_local_creates_runtime_dirs():
    text = (ROOT / "scripts" / "setup_local.sh").read_text()
    for d in ("data", "data/backups", "data/history", "logs"):
        assert d in text, f"setup_local.sh does not mkdir {d}"


def test_setup_local_chmod_env():
    text = (ROOT / "scripts" / "setup_local.sh").read_text()
    assert "chmod 600 .env" in text, ".env permissions must be locked to 600"


def test_run_local_forwards_args():
    text = (ROOT / "scripts" / "run_local.sh").read_text()
    assert "src.main" in text
    assert '"$@"' in text, "run_local.sh must forward CLI args to src.main"
    assert "tee -a logs/bot.log" in text


def test_launchd_template_exists():
    p = ROOT / "launchd" / "com.user.weather-bot.plist.template"
    assert p.is_file(), "launchd plist template missing"


def test_launchd_template_parses_after_substitution(tmp_path):
    """The template must be valid plist XML once __BOT_DIR__ is substituted."""
    src = (ROOT / "launchd" / "com.user.weather-bot.plist.template").read_text()
    # Force a substitution that yields a real path so plistlib accepts the
    # ProgramArguments as a non-empty string.
    populated = src.replace("__BOT_DIR__", str(tmp_path))
    parsed = plistlib.loads(populated.encode("utf-8"))
    assert parsed["Label"] == "com.user.weather-bot"
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    # The program is our run_local.sh
    args = parsed["ProgramArguments"]
    assert len(args) == 1
    assert args[0].endswith("scripts/run_local.sh")
    assert parsed["WorkingDirectory"] == str(tmp_path)
    assert parsed["StandardOutPath"].startswith(str(tmp_path))
    assert parsed["StandardErrorPath"].startswith(str(tmp_path))


def test_launchd_template_logs_under_bot_dir():
    """Both stdout/stderr paths must live under the bot dir so log
    rotation, backups, and chmod 600 are co-located with everything else."""
    src = (ROOT / "launchd" / "com.user.weather-bot.plist.template").read_text()
    assert "__BOT_DIR__/logs/launchd.out" in src
    assert "__BOT_DIR__/logs/launchd.err" in src


def test_runbook_exists():
    p = ROOT / "docs" / "runbook" / "local_deploy.md"
    assert p.is_file(), "docs/runbook/local_deploy.md is missing"
    text = p.read_text()
    # Must mention all three run modes so an operator can pick one.
    assert "--paper" in text
    assert "--dry-run" in text
    # Must mention the Keychain shared entry.
    assert "polymarket-bot" in text
    # Must mention the fee sanity-check (a known trip wire on first live BUY).
    assert "fee" in text.lower()


@pytest.mark.parametrize("path", [
    "scripts/setup_local.sh",
    "scripts/run_local.sh",
])
def test_scripts_use_strict_mode(path):
    """Both shell scripts opt into strict mode — silent failures during
    setup are how operators end up with a half-installed venv that
    the bot then crashes on at first cycle."""
    text = (ROOT / path).read_text()
    assert "set -e" in text or "set -euo pipefail" in text
