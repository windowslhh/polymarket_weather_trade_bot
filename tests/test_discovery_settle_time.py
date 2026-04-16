"""Unit tests for ``src.markets.discovery._compute_settle_timestamp``.

Why this matters: ``hours_to_settle`` derived from this helper drives the
/markets UI tag, the force-exit gate (``evaluator.py:680``), the new-entry
block (``evaluator.py:165``) and the ``SETTLING`` trend classifier
(``trend.py:50``).  Mis-computing it shifts every strategy trigger by hours
and silently kills entries.

The helper has two date-source paths (``gameStartTime`` → fallback to
``market_date``) sharing one timezone-anchored construction.  The shared
construction is what keeps DST transition days correct — a literal ``+24h``
on a UTC-shifted ``gameStartTime`` is wrong by ±1h on those two days per
year (see ``TestDSTTransitions``).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.markets.discovery import _compute_settle_timestamp


NYC = "America/New_York"
LA = "America/Los_Angeles"
CHI = "America/Chicago"


# ── (a) gameStartTime in canonical ISO-Z form ────────────────────────────────

def test_gst_iso_z_format_atlanta():
    """ATL Apr 16: gameStartTime = 04:00Z (= 00:00 ET) → settle = 2026-04-17 04:00Z."""
    end_dt = _compute_settle_timestamp(
        markets=[{"gameStartTime": "2026-04-16T04:00:00Z"}],
        market_date=date(2026, 4, 16),
        city_tz=NYC,
    )
    assert end_dt == datetime(2026, 4, 17, 4, 0, tzinfo=timezone.utc)


# ── (b) gameStartTime with space + "+00" no colon (production format) ────────

def test_gst_space_plus00_format():
    """Real Gamma format observed in production payloads."""
    end_dt = _compute_settle_timestamp(
        markets=[{"gameStartTime": "2026-04-16 04:00:00+00"}],
        market_date=date(2026, 4, 16),
        city_tz=NYC,
    )
    assert end_dt == datetime(2026, 4, 17, 4, 0, tzinfo=timezone.utc)


# ── (c) gameStartTime with explicit "+00:00" offset ──────────────────────────

def test_gst_explicit_offset_format():
    end_dt = _compute_settle_timestamp(
        markets=[{"gameStartTime": "2026-04-16T04:00:00+00:00"}],
        market_date=date(2026, 4, 16),
        city_tz=NYC,
    )
    assert end_dt == datetime(2026, 4, 17, 4, 0, tzinfo=timezone.utc)


# ── (d) gameStartTime missing/None/empty → falls back to market_date ─────────

@pytest.mark.parametrize("missing_value", [None, ""])
def test_fallback_when_gst_missing_or_empty(missing_value):
    end_dt = _compute_settle_timestamp(
        markets=[{"gameStartTime": missing_value}],
        market_date=date(2026, 4, 16),
        city_tz=NYC,
    )
    # Same answer as the gameStartTime path
    assert end_dt == datetime(2026, 4, 17, 4, 0, tzinfo=timezone.utc)


def test_fallback_when_gst_key_absent():
    end_dt = _compute_settle_timestamp(
        markets=[{}],
        market_date=date(2026, 4, 16),
        city_tz=NYC,
    )
    assert end_dt == datetime(2026, 4, 17, 4, 0, tzinfo=timezone.utc)


# ── (e) markets list empty → falls back to market_date ───────────────────────

def test_empty_markets_list_uses_market_date():
    """LA in April = PDT (UTC-7); 00:00 PDT Apr 17 = 07:00 UTC."""
    end_dt = _compute_settle_timestamp(
        markets=[],
        market_date=date(2026, 4, 16),
        city_tz=LA,
    )
    assert end_dt == datetime(2026, 4, 17, 7, 0, tzinfo=timezone.utc)


# ── (f) city_tz missing or unparseable → None ────────────────────────────────

@pytest.mark.parametrize("bad_tz", [None, "", "Not/A_Real_Zone"])
def test_no_or_invalid_tz_returns_none(bad_tz):
    """Without a valid tz we cannot construct a meaningful settle moment."""
    end_dt = _compute_settle_timestamp(
        markets=[{"gameStartTime": "2026-04-16T04:00:00Z"}],
        market_date=date(2026, 4, 16),
        city_tz=bad_tz,
    )
    assert end_dt is None


# ── Garbage gameStartTime falls through to market_date path ──────────────────

def test_unparseable_gst_falls_back_to_market_date():
    """Logs at debug level (no crash, no silent wrong answer)."""
    end_dt = _compute_settle_timestamp(
        markets=[{"gameStartTime": "not-a-timestamp"}],
        market_date=date(2026, 4, 16),
        city_tz=NYC,
    )
    assert end_dt == datetime(2026, 4, 17, 4, 0, tzinfo=timezone.utc)


# ── (h) DST transitions: both paths agree (DST-safety guarantee) ─────────────

class TestDSTTransitions:
    """Anchor the DST-safety guarantee added in this fix.

    Spring forward 2026-03-08 in NYC: clocks jump 02:00 EST → 03:00 EDT,
    so the calendar day Mar 8 is only 23h long.  A literal
    ``gameStartTime + 24h`` would put the settle at 2026-03-09 05:00 UTC
    (1h late).  Correct: 2026-03-09 04:00 UTC (00:00 EDT Mar 9).

    Fall back 2026-11-01 in NYC: clocks jump 02:00 EDT → 01:00 EST,
    so day Nov 1 is 25h long.  A literal +24h would put the settle at
    2026-11-02 04:00 UTC (1h early).  Correct: 2026-11-02 05:00 UTC
    (00:00 EST Nov 2).
    """

    def test_spring_forward_primary_and_fallback_agree(self):
        # 00:00 EST Mar 8 = 05:00 UTC (still EST)
        primary = _compute_settle_timestamp(
            markets=[{"gameStartTime": "2026-03-08T05:00:00Z"}],
            market_date=date(2026, 3, 8),
            city_tz=NYC,
        )
        fallback = _compute_settle_timestamp(
            markets=[],
            market_date=date(2026, 3, 8),
            city_tz=NYC,
        )
        # Correct: end of Mar 8 NYC = 00:00 EDT Mar 9 = 04:00 UTC
        expected = datetime(2026, 3, 9, 4, 0, tzinfo=timezone.utc)
        assert primary == expected
        assert fallback == expected
        assert primary == fallback  # DST-safe: both paths converge

    def test_fall_back_primary_and_fallback_agree(self):
        # 00:00 EDT Nov 1 = 04:00 UTC (still EDT)
        primary = _compute_settle_timestamp(
            markets=[{"gameStartTime": "2026-11-01T04:00:00Z"}],
            market_date=date(2026, 11, 1),
            city_tz=NYC,
        )
        fallback = _compute_settle_timestamp(
            markets=[],
            market_date=date(2026, 11, 1),
            city_tz=NYC,
        )
        # Correct: end of Nov 1 NYC = 00:00 EST Nov 2 = 05:00 UTC
        expected = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
        assert primary == expected
        assert fallback == expected
        assert primary == fallback


# ── Production data anchors (lock measured values from 2026-04-16) ───────────

class TestProductionAnchors:
    """Lock the exact UTC moments measured against live Gamma API on 2026-04-16.

    These are the same values reported in
    ``docs/fixes/2026-04-16-settlement-time.md``.
    """

    @pytest.mark.parametrize(
        "label, city_tz, gst, market_date, expected",
        [
            ("ATL/Miami Apr 16", NYC, "2026-04-16 04:00:00+00",
             date(2026, 4, 16), datetime(2026, 4, 17, 4, 0, tzinfo=timezone.utc)),
            ("LA/SF/Sea Apr 16", LA, "2026-04-16 07:00:00+00",
             date(2026, 4, 16), datetime(2026, 4, 17, 7, 0, tzinfo=timezone.utc)),
            ("CHI/HOU Apr 17", CHI, "2026-04-17 05:00:00+00",
             date(2026, 4, 17), datetime(2026, 4, 18, 5, 0, tzinfo=timezone.utc)),
        ],
    )
    def test_production_values(self, label, city_tz, gst, market_date, expected):
        end_dt = _compute_settle_timestamp(
            markets=[{"gameStartTime": gst}],
            market_date=market_date,
            city_tz=city_tz,
        )
        assert end_dt == expected, label
