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


# ──────────────────────────────────────────────────────────────────────
# C-3: residual date.today() sweep (business-logic paths only)
# ──────────────────────────────────────────────────────────────────────


def test_forecast_get_ensemble_target_date_default_is_utc() -> None:
    """C-3: every async forecast fetcher's `target_date` fallback must
    use UTC, not server-local.  Production callers pass an explicit
    target via `city_local_date`, but the default is reachable via
    legacy / ad-hoc callers and must not silently disagree."""
    from src.weather import forecast as forecast_mod

    for fn in (
        forecast_mod.get_ensemble_forecast,
        forecast_mod.get_single_forecast,
        forecast_mod.get_forecast,
    ):
        body = inspect.getsource(fn)
        assert "datetime.now(timezone.utc).date()" in body, (
            f"C-3: {fn.__name__} must use UTC for the target_date fallback"
        )
        assert "or date.today()" not in body, (
            f"C-3: {fn.__name__} still has `target_date or date.today()`"
        )


def test_nws_get_forecast_target_date_default_is_utc() -> None:
    from src.weather import nws as nws_mod

    body = inspect.getsource(nws_mod.get_nws_forecast)
    assert "datetime.now(timezone.utc).date()" in body, (
        "C-3: NWS fallback target_date must be UTC"
    )
    assert "or date.today()" not in body


def test_backtest_engine_run_backtest_uses_utc_end() -> None:
    from src.backtest import engine as engine_mod

    body = inspect.getsource(engine_mod.run_backtest)
    assert "datetime.now(timezone.utc).date()" in body, (
        "C-3: run_backtest's `end` date must be UTC-anchored"
    )


def test_no_business_logic_date_today_remains_in_src() -> None:
    """Final sweep: scan all *.py under src/ and assert the only
    remaining `date.today()` calls are in:
      - comments / docstrings (allowed: documentation reference)
      - src/weather/historical.py (exempt: offline cache metadata)
    Any other site is a regression."""
    from pathlib import Path
    import re
    src_root = Path(__file__).resolve().parents[1] / "src"
    pat = re.compile(r"\bdate\.today\(\)")
    offenders: list[tuple[str, int, str]] = []
    for py in src_root.rglob("*.py"):
        for i, line in enumerate(py.read_text().splitlines(), start=1):
            if not pat.search(line):
                continue
            stripped = line.strip()
            # Skip pure comment/docstring lines (heuristic: leading # or
            # the line lives inside `""" ... """` block — we accept any
            # line whose first non-whitespace token is # or " or `).
            if stripped.startswith(("#", '"', "'", "`")):
                continue
            # Skip docstrings that quote the bug as text — e.g.
            # 'no longer reads ``date.today()``' (markdown double-backticks)
            # or '`date.today()`' (single backticks).
            if "`date.today()`" in line or "``date.today()``" in line:
                continue
            # Skip prose continuation lines from multi-line docstrings:
            # "...date.today()).  All timestamps in this tracker..." —
            # the call appears as text in the middle of a sentence with
            # no `=` and no method-call follow-on.
            if "date.today()." not in line and "= date.today()" not in line \
                    and "(date.today()" not in line \
                    and "date.today() -" not in line:
                continue
            offenders.append((str(py.relative_to(src_root)), i, line.rstrip()))

    # Allowed exempt file: historical.py (cache metadata).  Drop those.
    offenders = [
        (f, i, line) for f, i, line in offenders
        if not f.endswith("weather/historical.py")
    ]
    assert not offenders, (
        "C-3: business-logic paths must use UTC anchors. Remaining "
        f"date.today() call sites:\n" + "\n".join(
            f"  {f}:{i}  {line}" for f, i, line in offenders
        )
    )
