from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class CityConfig:
    name: str
    icao: str
    lat: float
    lon: float


@dataclass
class StrategyConfig:
    no_distance_threshold_f: int = 8
    min_no_ev: float = 0.03
    yes_confirmation_threshold: float = 0.85
    max_position_per_slot_usd: float = 5.0
    max_exposure_per_city_usd: float = 50.0
    max_total_exposure_usd: float = 1000.0
    daily_loss_limit_usd: float = 50.0
    kelly_fraction: float = 0.5
    min_market_volume: float = 500.0
    max_slot_spread: float = 0.15
    min_trim_ev: float = 0.02
    ladder_width: int = 3
    ladder_min_ev: float = 0.03
    ladder_min_distance_f: float = 2.0
    max_no_price: float = 0.85
    day_ahead_ev_discount: float = 0.7
    max_days_ahead: int = 2
    max_positions_per_event: int = 4


def get_strategy_variants() -> dict[str, dict]:
    """Six strategy variants running in parallel for A/B testing.

    NEW strategies (optimized from Apr 5-9 data):
    A = Conservative: NO only on distant slots, no ladder, strict price cap
    B = Moderate: Ladder with 4°F min distance, balanced EV thresholds
    C = Aggressive: Wider ladder, more trades but capped price

    OLD strategies (control group, original params from Apr 5-6):
    D = Old Conservative: original A params
    E = Old Moderate: original B params
    F = Old Aggressive: original C params

    All strategies now benefit from EXIT bug fix (no false exits on future markets).
    """
    return {
        # --- NEW strategies (optimized) ---
        "A": {
            "no_distance_threshold_f": 8,
            "min_no_ev": 0.05,
            "ladder_width": 0,  # disabled — conservative, distant-only NO
            "ladder_min_ev": 1.0,  # effectively disabled
            "ladder_min_distance_f": 99.0,
            "max_no_price": 0.70,
            "max_position_per_slot_usd": 5.0,
            "max_exposure_per_city_usd": 30.0,
            "day_ahead_ev_discount": 0.6,
            "min_trim_ev": 0.02,
            "max_positions_per_event": 3,
        },
        "B": {
            "no_distance_threshold_f": 8,
            "min_no_ev": 0.05,
            "ladder_width": 3,
            "ladder_min_ev": 0.05,
            "ladder_min_distance_f": 4.0,
            "max_no_price": 0.75,
            "max_position_per_slot_usd": 3.0,
            "max_exposure_per_city_usd": 30.0,
            "day_ahead_ev_discount": 0.7,
            "min_trim_ev": 0.02,
            "max_positions_per_event": 4,
        },
        "C": {
            "no_distance_threshold_f": 8,
            "min_no_ev": 0.03,
            "ladder_width": 4,
            "ladder_min_ev": 0.03,
            "ladder_min_distance_f": 3.0,
            "max_no_price": 0.80,
            "max_position_per_slot_usd": 2.0,
            "max_exposure_per_city_usd": 25.0,
            "day_ahead_ev_discount": 0.8,
            "min_trim_ev": 0.02,
            "max_positions_per_event": 4,
        },
        # --- OLD strategies (control group) ---
        "D": {  # Old A: Conservative original
            "no_distance_threshold_f": 8,
            "min_no_ev": 0.05,
            "ladder_width": 0,
            "ladder_min_ev": 1.0,
            "ladder_min_distance_f": 99.0,
            "max_no_price": 0.80,  # original higher cap
            "max_position_per_slot_usd": 3.0,  # original smaller size
            "max_exposure_per_city_usd": 30.0,
            "day_ahead_ev_discount": 0.6,
            "min_trim_ev": 0.005,  # original low trim threshold
            "max_positions_per_event": 8,  # original no real cap
        },
        "E": {  # Old B: Moderate original
            "no_distance_threshold_f": 8,
            "min_no_ev": 0.03,  # original lower threshold
            "ladder_width": 3,
            "ladder_min_ev": 0.03,
            "ladder_min_distance_f": 4.0,
            "max_no_price": 0.85,  # original higher cap
            "max_position_per_slot_usd": 3.0,
            "max_exposure_per_city_usd": 30.0,
            "day_ahead_ev_discount": 0.7,
            "min_trim_ev": 0.005,
            "max_positions_per_event": 8,
        },
        "F": {  # Old C: Aggressive original
            "no_distance_threshold_f": 6,  # original tighter
            "min_no_ev": 0.02,
            "ladder_width": 4,
            "ladder_min_ev": 0.02,
            "ladder_min_distance_f": 2.0,  # original closer
            "max_no_price": 0.90,  # original very high cap
            "max_position_per_slot_usd": 1.5,
            "max_exposure_per_city_usd": 20.0,
            "day_ahead_ev_discount": 0.8,
            "min_trim_ev": 0.005,
            "max_positions_per_event": 8,
        },
    }


@dataclass
class SchedulingConfig:
    discovery_interval_minutes: int = 15
    rebalance_interval_minutes: int = 60
    pnl_snapshot_interval_hours: int = 24


@dataclass
class AppConfig:
    # Polymarket credentials
    polymarket_api_key: str = ""
    polymarket_secret: str = ""
    polymarket_passphrase: str = ""
    eth_private_key: str = ""

    # Optional weather API key
    openweathermap_api_key: str = ""

    # Alert webhook URL (Telegram/Discord/Slack)
    alert_webhook_url: str = ""

    # Sub-configs
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    cities: list[CityConfig] = field(default_factory=list)

    # Runtime flags: dry_run = print only; paper = simulate fills + track positions
    dry_run: bool = False
    paper: bool = False
    db_path: Path = field(default_factory=lambda: _ROOT / "data" / "bot.db")


def load_config(config_path: str | Path | None = None, env_path: str | Path | None = None) -> AppConfig:
    """Load configuration from .env and config.yaml."""
    load_dotenv(env_path or _ROOT / ".env")

    cfg_file = Path(config_path) if config_path else _ROOT / "config.yaml"
    with open(cfg_file) as f:
        raw = yaml.safe_load(f)

    strategy_raw = raw.get("strategy", {})
    scheduling_raw = raw.get("scheduling", {})
    cities_raw = raw.get("cities", [])

    return AppConfig(
        polymarket_api_key=os.getenv("POLYMARKET_API_KEY", ""),
        polymarket_secret=os.getenv("POLYMARKET_SECRET", ""),
        polymarket_passphrase=os.getenv("POLYMARKET_PASSPHRASE", ""),
        eth_private_key=os.getenv("ETH_PRIVATE_KEY", ""),
        openweathermap_api_key=os.getenv("OPENWEATHERMAP_API_KEY", ""),
        alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL", ""),
        strategy=StrategyConfig(**strategy_raw),
        scheduling=SchedulingConfig(**scheduling_raw),
        cities=[CityConfig(**c) for c in cities_raw],
    )
