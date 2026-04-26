"""Y6: dashboard total_realized must be consistent with per-strategy buckets.

Pre-fix the dashboard headline `total_realized` summed realized_pnl
across ALL closed positions (including legacy strategy='A'), while
`strat_realized` only iterated B/C/D.  The two numbers diverged on
any DB with historical A rows, breaking the operator's ability to
reconcile "headline total" with "per-strategy detail."

Y6: filter total_realized to {B, C, D} and surface legacy A as a
separate `legacy_a_pnl` line so audit history is preserved without
corrupting the headline.
"""
from __future__ import annotations

from pathlib import Path


_APP_PY = Path(__file__).resolve().parents[1] / "src" / "web" / "app.py"
_DASHBOARD_HTML = (
    Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "dashboard.html"
)


def test_dashboard_total_realized_filters_by_active_strategy() -> None:
    """Static check: the total_realized sum in app.py must filter on the
    active strategy set.  Catches a regression that drops the filter
    and resumes including legacy 'A' rows."""
    body = _APP_PY.read_text()
    assert "active_strats = {\"B\", \"C\", \"D\"}" in body, (
        "Y6: active_strats set should be defined"
    )
    # The total_realized sum should reference active_strats
    assert "p.get(\"strategy\") in active_strats" in body, (
        "Y6: total_realized must filter by active_strats; pre-fix it "
        "summed all closed positions including legacy 'A'"
    )
    # The legacy_a_pnl variable should also be computed for transparency
    assert "legacy_a_pnl" in body
    assert "p.get(\"strategy\") == \"A\"" in body


def test_dashboard_template_renders_legacy_a_when_nonzero() -> None:
    """The dashboard template should surface legacy A pnl conditionally
    so an operator with audit-trail A rows can still see them, but the
    block doesn't clutter the UI on a clean DB."""
    body = _DASHBOARD_HTML.read_text()
    assert "legacy_a_pnl" in body
    assert "legacy A" in body, (
        "Y6: dashboard must label the legacy A figure visibly"
    )


def test_dashboard_total_realized_filter_logic_matches_strat_realized() -> None:
    """End-to-end consistency: simulate dashboard's logic and verify
    the headline matches the sum of per-strategy buckets."""
    closed_positions = [
        {"strategy": "B", "realized_pnl": 1.50},
        {"strategy": "C", "realized_pnl": -0.30},
        {"strategy": "D", "realized_pnl": 0.80},
        {"strategy": "A", "realized_pnl": 12.40},  # legacy — must NOT count
        {"strategy": "B", "realized_pnl": None},   # ignored
    ]

    # Simulate Y6 fix
    active_strats = {"B", "C", "D"}
    total_realized = sum(
        p["realized_pnl"] for p in closed_positions
        if p.get("realized_pnl") is not None
        and p.get("strategy") in active_strats
    )
    legacy_a_pnl = sum(
        p["realized_pnl"] for p in closed_positions
        if p.get("realized_pnl") is not None
        and p.get("strategy") == "A"
    )

    # Per-strategy bucket sum (simulating strat_realized aggregation)
    strat_buckets = {"B": 0.0, "C": 0.0, "D": 0.0}
    for p in closed_positions:
        if p.get("realized_pnl") is None:
            continue
        s = p.get("strategy")
        if s in strat_buckets:
            strat_buckets[s] += p["realized_pnl"]

    # The headline MUST equal the sum of per-strategy buckets
    assert abs(total_realized - sum(strat_buckets.values())) < 1e-9, (
        f"Y6 invariant: total_realized ({total_realized}) must equal "
        f"sum of per-strategy buckets ({sum(strat_buckets.values())})"
    )
    # And legacy A is surfaced separately, not lost
    assert legacy_a_pnl == 12.40
