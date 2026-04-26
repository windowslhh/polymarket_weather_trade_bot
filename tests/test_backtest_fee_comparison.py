"""FIX-2P-4: smoke test that the fee-comparison script runs end-to-end
and produces a report file with the expected anchors.

The full Monte Carlo backtest is too heavy to run on every pytest pass,
but we exercise a 30-day window over two cities to keep the test under
a second.  The structural assertions catch common rot — missing
sections, accidentally identical pre/post numbers, format drift.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest


def test_fee_comparison_script_emits_expected_sections(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "_btfc",
        Path(__file__).resolve().parents[1] / "scripts" / "backtest_fee_comparison.py",
    )
    module = importlib.util.module_from_spec(spec)
    # Register so dataclasses can resolve __module__ during introspection.
    sys.modules[spec.name] = module

    # Trim cohort + window so the test is fast.
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "CITY_PROFILES", module.CITY_PROFILES[:2])
    monkeypatch.setattr(module, "NUM_DAYS", 30)
    # Redirect output dir to tmp so we don't clutter docs/backtests/.
    monkeypatch.setattr(module, "ROOT", tmp_path)

    assert module.main() == 0
    out_files = list((tmp_path / "docs" / "backtests").glob("*-new-fee.md"))
    assert len(out_files) == 1
    body = out_files[0].read_text()

    # Required structural anchors.
    for anchor in [
        "## Per-variant summary — pre-fix fee",
        "## Per-variant summary — post-fix fee",
        "## Delta (post − pre)",
        "## LOCKED_WIN — analytical fee impact (per share)",
        "## Reality check vs 25h paper (Y5, 2026-04-26)",
        "## What to do next",
        "FIX-2P-12",
        # Y5 explicit warning against tuning from this report
        "Do NOT use these",
        # Y5 reality-check rows (sample one each)
        "Miami",
        "Chicago",
        "Denver",
    ]:
        assert anchor in body, f"missing anchor: {anchor}"

    # Sanity: pre and post tables are not literally identical (would mean
    # the fee_rate kwarg never reached the engine).
    pre = re.search(
        r"## Per-variant summary — pre-fix fee.*?(?=##)", body, flags=re.S,
    )
    post = re.search(
        r"## Per-variant summary — post-fix fee.*?(?=##)", body, flags=re.S,
    )
    assert pre and post
    assert pre.group(0).split("\n", 1)[1:] != post.group(0).split("\n", 1)[1:], (
        "Pre and post fee tables are identical — fee_rate kwarg likely "
        "didn't propagate to _run_day."
    )
