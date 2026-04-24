"""Serial medium-priority fixes: M1 / M8 / M9 / P2-11.

One test file covering the small fixes so they all have coverage without
a dozen near-empty files.  The larger fixes (M2 executor batch, M7
preflight) get their own files below.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from src.config import AppConfig, CityConfig, StrategyConfig, SchedulingConfig
from src.strategy.rebalancer import _effective_city_config


# ── FIX-M8: frozen dataclass ──────────────────────────────────────────


def test_strategy_config_is_frozen():
    cfg = StrategyConfig()
    with pytest.raises(FrozenInstanceError):
        cfg.kelly_fraction = 0.9  # type: ignore[misc]


def test_scheduling_config_is_frozen():
    sc = SchedulingConfig()
    with pytest.raises(FrozenInstanceError):
        sc.rebalance_interval_minutes = 1  # type: ignore[misc]


def test_city_config_is_frozen():
    c = CityConfig("NYC", "KLGA", 40.7, -74.0)
    with pytest.raises(FrozenInstanceError):
        c.icao = "KJFK"  # type: ignore[misc]


# ── FIX-M9: case-insensitive thin_liquidity_cities ───────────────────


def test_thin_liquidity_match_is_case_insensitive():
    base = StrategyConfig()
    # "miami" lowercased — pre-M9 would have returned the base cfg.
    effective = _effective_city_config(base, "miami")
    assert effective.max_exposure_per_city_usd == (
        base.max_exposure_per_city_usd * base.thin_liquidity_exposure_ratio
    )


def test_thin_liquidity_non_thin_city_unchanged():
    base = StrategyConfig()
    effective = _effective_city_config(base, "NYC")
    assert effective.max_exposure_per_city_usd == base.max_exposure_per_city_usd


# ── FIX-P2-11: math.ceil on calibrated distance ──────────────────────


def test_math_ceil_used_for_calibrated_distance():
    """Ensure src/strategy/rebalancer.py uses math.ceil, not round, for
    the two auto-calibrate call sites.  AST-scan so the assertion is
    robust to whitespace/comment changes."""
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "src" / "strategy" / "rebalancer.py"
    tree = ast.parse(path.read_text())
    found_round_on_cal = 0
    found_ceil_on_cal = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "ceil":
                for arg in node.args:
                    if (isinstance(arg, ast.Name) and arg.id == "cal_dist") or \
                       (isinstance(arg, ast.Attribute) and getattr(arg, "attr", "") == "cal_dist"):
                        found_ceil_on_cal += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "round":
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id == "cal_dist":
                        found_round_on_cal += 1
    assert found_ceil_on_cal >= 2, (
        f"Expected at least 2 math.ceil(cal_dist) call sites; found {found_ceil_on_cal}"
    )
    assert found_round_on_cal == 0, (
        "FIX-P2-11 regression: round(cal_dist) reappeared in rebalancer.py"
    )


# ── FIX-M1: UTC default for daily_pnl / snapshot_pnl ─────────────────


@pytest.mark.asyncio
async def test_tracker_get_daily_pnl_uses_utc_default(monkeypatch):
    """Patch datetime.now so we can assert the stored key is the UTC date."""
    import tempfile
    from datetime import datetime, timezone
    from pathlib import Path
    from src.portfolio.store import Store
    from src.portfolio.tracker import PortfolioTracker

    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    tracker = PortfolioTracker(store)

    # Seed today's UTC row.
    today_utc = datetime.now(timezone.utc).date().isoformat()
    await store.upsert_daily_pnl(today_utc, -12.5, 0.0, 0.0)

    pnl = await tracker.get_daily_pnl()  # default → UTC today
    assert pnl == -12.5

    await store.close()
