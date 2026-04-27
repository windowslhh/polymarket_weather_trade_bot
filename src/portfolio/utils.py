"""Cost-basis helpers shared across all P&L call sites.

Background (Fix B1, 2026-04-28): the bot writes ``positions.entry_price``
as the limit price submitted to CLOB, but the actual fill may cross at a
better price (e.g. limit 0.69 → fill 0.685 when the maker side is thin).
``positions.match_price`` (added in Fix B) records the effective per-share
fill from ``/data/trades``; legacy and paper-mode rows have it NULL and
must fall back to ``entry_price``.

Every place that computes "cost basis" — display P&L on the dashboard,
realized P&L on close/SELL, settlement payout vs cost, the reconciler's
post-restart P&L computation — MUST go through this helper so the
backfill of historical match_price values lights up consistently.

Accepts both dict rows (``aiosqlite.Row`` /  plain dict) and dataclass-
shaped objects (``getattr`` fallback) so existing call sites don't have
to normalise.
"""
from __future__ import annotations

from typing import Any


def effective_entry_price(p: Any) -> float:
    """Return ``match_price`` (actual fill) when present, else ``entry_price``.

    Use this everywhere cost basis matters: realized P&L computation,
    unrealized P&L display, settlement payout vs cost, reconciler P&L.
    """
    if isinstance(p, dict):
        mp = p.get("match_price")
        ep = p["entry_price"]
    else:
        mp = getattr(p, "match_price", None)
        ep = getattr(p, "entry_price")
    if mp is not None:
        return float(mp)
    return float(ep)
