"""FIX-2P-11: REJECT reason codes must be UPPERCASE.

GATE_MATRIX rejection codes (PRICE_TOO_LOW, EV_BELOW_GATE, …) all use
SCREAMING_SNAKE.  The whitelist branch in rebalancer.py drifted to a
lowercase 'city_not_in_whitelist' string, breaking decision_log
filtering / grouping queries that key off the convention.  Pin the
casing here so a future edit doesn't silently regress.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_whitelist_reject_code_uppercase() -> None:
    body = (ROOT / "src" / "strategy" / "rebalancer.py").read_text()
    assert "CITY_NOT_IN_WHITELIST" in body, (
        "FIX-2P-11: rebalancer must emit CITY_NOT_IN_WHITELIST as the "
        "REJECT code so it groups with the other GATE_MATRIX codes."
    )
    assert "city_not_in_whitelist" not in body, (
        "FIX-2P-11: lowercase 'city_not_in_whitelist' string survived "
        "in rebalancer.py — convert to SCREAMING_SNAKE."
    )
