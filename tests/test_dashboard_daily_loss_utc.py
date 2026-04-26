"""G-7 (2026-04-26): the dashboard's `daily_loss_remaining` gauge must
sum ONLY today's UTC-bucketed realized PnL, not the full closed_pos
history.

Pre-fix the formula was `daily_loss_limit_usd - abs(total_realized)`,
where `total_realized` summed every closed position in the recent-200
result set (multiple days of history).  A losing week made the gauge
show a negative remainder even on a winning today; a winning week
made it look like infinite headroom on a losing today.

The settler writes `daily_pnl` bucketed in UTC (FIX-M1 / R-02).  G-7
brings the dashboard's loss gauge into the same UTC anchor by
filtering closed_pos on `closed_at[:10] == utc_today_iso`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def test_g7_filter_logic_pin():
    """Static check: the dashboard source uses `_is_today` filtering
    on closed_at when computing `total_realized_today`.  Catches a
    revert that resumes summing the full history into the loss gauge."""
    body = (Path(__file__).resolve().parents[1] / "src" / "web" / "app.py").read_text()
    assert "total_realized_today" in body, (
        "G-7: dashboard must compute a today-only realized total"
    )
    assert "utc_today_iso" in body, (
        "G-7: today filter must anchor on UTC date, not server-local"
    )
    assert "ca[:10] == utc_today_iso" in body, (
        "G-7: closed_at must be matched on its first 10 chars (YYYY-MM-DD)"
    )
    # daily_loss_remaining must reference TODAY's value, not the all-history one
    assert 'd.get("total_realized_today"' in body
    assert "daily_loss_remaining=cfg.strategy.daily_loss_limit_usd - abs(\n                d.get(\"total_realized_today\"" in body, (
        "G-7: daily_loss_remaining must subtract today's realized only"
    )


def _is_today_filter(closed_pos: list[dict], utc_today_iso: str) -> list[dict]:
    """Re-implement G-7's filter so we can drive it from a unit test
    without spinning up Flask."""
    return [
        p for p in closed_pos
        if isinstance(p.get("closed_at"), str)
        and p["closed_at"][:10] == utc_today_iso
    ]


def test_g7_excludes_yesterday_closed_positions():
    """A position closed yesterday MUST NOT contribute to today's
    daily-loss calculation."""
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    rows = [
        {"closed_at": f"{today.isoformat()} 12:00:00",
         "realized_pnl": -5.0, "strategy": "B"},
        {"closed_at": f"{yesterday.isoformat()} 23:55:00",
         "realized_pnl": -50.0, "strategy": "B"},  # huge loss yesterday
    ]
    todays = _is_today_filter(rows, today.isoformat())
    assert len(todays) == 1
    assert todays[0]["realized_pnl"] == -5.0


def test_g7_handles_missing_closed_at_safely():
    """A row with closed_at=None (shouldn't happen for a closed row,
    but defense in depth) is excluded — never crashes."""
    today = datetime.now(timezone.utc).date()
    rows = [
        {"closed_at": None, "realized_pnl": -5.0, "strategy": "B"},
        {"closed_at": "", "realized_pnl": -3.0, "strategy": "B"},
        {"closed_at": f"{today.isoformat()} 10:00:00",
         "realized_pnl": -2.0, "strategy": "B"},
    ]
    todays = _is_today_filter(rows, today.isoformat())
    assert len(todays) == 1
    assert todays[0]["realized_pnl"] == -2.0


def test_g7_utc_midnight_boundary():
    """At UTC 00:01, a position closed at UTC 23:55 yesterday must NOT
    contribute to "today" — the boundary is a strict date prefix match."""
    # Simulate a UTC clock that just rolled over.
    now_utc = datetime(2026, 4, 27, 0, 1, 0, tzinfo=timezone.utc)
    today_iso = now_utc.date().isoformat()  # 2026-04-27
    rows = [
        {"closed_at": "2026-04-26 23:55:00",
         "realized_pnl": -100.0, "strategy": "B"},
        {"closed_at": "2026-04-27 00:00:30",
         "realized_pnl": -1.0, "strategy": "B"},
    ]
    todays = _is_today_filter(rows, today_iso)
    assert len(todays) == 1
    assert todays[0]["closed_at"].startswith("2026-04-27")


def test_g7_filter_works_across_strategies():
    """The filter is independent of the strategy field — it's a
    timestamp comparison only.  Strategy filtering is layered on
    separately in the dashboard code."""
    today_iso = datetime.now(timezone.utc).date().isoformat()
    rows = [
        {"closed_at": f"{today_iso} 10:00:00",
         "realized_pnl": -1.0, "strategy": "B"},
        {"closed_at": f"{today_iso} 11:00:00",
         "realized_pnl": -2.0, "strategy": "C"},
        {"closed_at": f"{today_iso} 12:00:00",
         "realized_pnl": -3.0, "strategy": "D"},
        {"closed_at": "2026-01-01 00:00:00",
         "realized_pnl": -100.0, "strategy": "B"},
    ]
    todays = _is_today_filter(rows, today_iso)
    assert len(todays) == 3
    assert sum(p["realized_pnl"] for p in todays) == -6.0
