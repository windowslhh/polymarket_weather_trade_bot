"""FIX-2P-9: pin the post-rollout fee callout in CLAUDE.md.

CLAUDE.md is the canonical hand-off document for Claude Code sessions.
The 5%-no-double-multiply story is load-bearing for any future Claude
that touches the EV math; pin a couple of unambiguous markers so the
invariant survives doc reshuffles.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MD = ROOT / "CLAUDE.md"


def test_claude_md_records_5pct_fee_post_rollout() -> None:
    body = CLAUDE_MD.read_text()
    assert "Polymarket Weather taker fee = 5%" in body, (
        "FIX-2P-9: CLAUDE.md must surface the post-2026-03-30 5% fee."
    )
    assert "2026-03-30" in body, (
        "FIX-2P-9: keep the rollout date so future readers know when "
        "the constant changed."
    )
    assert "FIX-2P-2" in body
    assert "No ×2 factor" in body or "no ×2" in body.lower(), (
        "FIX-2P-9: must call out that the formula has no ×2 factor "
        "(the pre-fix bug doubled it)."
    )
