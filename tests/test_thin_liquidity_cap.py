"""Fix 5: thin-liquidity per-city cap tests.

See docs/fixes/2026-04-16-strategy-p0-fixes.md#fix-5.
"""
from __future__ import annotations

from src.config import StrategyConfig
from src.strategy.rebalancer import _effective_city_config


def test_thin_city_gets_reduced_cap():
    cfg = StrategyConfig(max_exposure_per_city_usd=30.0, thin_liquidity_exposure_ratio=0.5)
    effective = _effective_city_config(cfg, "Miami")
    assert effective.max_exposure_per_city_usd == 15.0
    # Other fields preserved
    assert effective.max_total_exposure_usd == cfg.max_total_exposure_usd
    assert effective.kelly_fraction == cfg.kelly_fraction


def test_non_thin_city_unchanged():
    cfg = StrategyConfig(max_exposure_per_city_usd=30.0, thin_liquidity_exposure_ratio=0.5)
    effective = _effective_city_config(cfg, "New York")
    # Same object (identity check) — no allocation for non-thin cities
    assert effective is cfg


def test_san_francisco_tampa_orlando_also_thin():
    cfg = StrategyConfig(max_exposure_per_city_usd=40.0, thin_liquidity_exposure_ratio=0.5)
    for city in ("San Francisco", "Tampa", "Orlando"):
        effective = _effective_city_config(cfg, city)
        assert effective.max_exposure_per_city_usd == 20.0, f"{city} should be reduced"


def test_default_ratio_half():
    """Default thin_liquidity_exposure_ratio is 0.5."""
    cfg = StrategyConfig()
    assert cfg.thin_liquidity_exposure_ratio == 0.5


def test_default_cities_list():
    cfg = StrategyConfig()
    assert "Miami" in cfg.thin_liquidity_cities
    assert "San Francisco" in cfg.thin_liquidity_cities
    assert "Tampa" in cfg.thin_liquidity_cities
    assert "Orlando" in cfg.thin_liquidity_cities
    assert "New York" not in cfg.thin_liquidity_cities


def test_custom_ratio_honored():
    cfg = StrategyConfig(
        max_exposure_per_city_usd=20.0,
        thin_liquidity_exposure_ratio=0.25,
    )
    effective = _effective_city_config(cfg, "Miami")
    assert effective.max_exposure_per_city_usd == 5.0
