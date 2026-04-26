"""FIX-2P-10: business-logic date anchors must be UTC, not server-local.

`date.today()` reads the *server's* local clock, so a dev box in
non-UTC tz silently disagrees with the container (UTC) and the DB
(UTC).  M1 fixed most call sites; the four covered here were missed
and only surfaced via the audit.
"""
from __future__ import annotations

import inspect

from src.markets import discovery as discovery_mod
from src.weather import metar as metar_mod


def test_discovery_parse_date_uses_utc_year_fallback() -> None:
    src = inspect.getsource(discovery_mod._parse_date)
    # Allowed: the docstring narrates `date.today()` as the prior bug.
    code_lines = [
        ln for ln in src.splitlines()
        if "date.today()" in ln and not ln.lstrip().startswith(("#", '"', "FIX"))
    ]
    assert all('"' in ln or "'" in ln or "docstring" in ln.lower() for ln in code_lines) or not code_lines, (
        f"FIX-2P-10: discovery._parse_date must not call date.today(); "
        f"offending lines: {code_lines}"
    )
    # Y9: the year fallback should now compute via `datetime.now(tz).year`
    # where tz is either the city's tz (when supplied) or UTC.
    assert "datetime.now(tz).year" in src, (
        "FIX-2P-10 + Y9: discovery._parse_date year fallback must anchor "
        "on the resolved tz (city-local when supplied, UTC otherwise)."
    )


def test_metar_local_today_falls_back_to_utc() -> None:
    src = inspect.getsource(metar_mod.DailyMaxTracker._local_today)
    assert "datetime.now(timezone.utc).date()" in src, (
        "FIX-2P-10: DailyMaxTracker._local_today fallback must use UTC."
    )


def test_metar_cleanup_old_default_is_utc() -> None:
    src = inspect.getsource(metar_mod.DailyMaxTracker.cleanup_old)
    assert "datetime.now(timezone.utc).date()" in src, (
        "FIX-2P-10: DailyMaxTracker.cleanup_old default keep_date must use UTC."
    )
