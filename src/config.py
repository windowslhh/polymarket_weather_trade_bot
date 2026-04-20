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
    tz: str = "America/New_York"  # IANA timezone for local date grouping


@dataclass
class StrategyConfig:
    no_distance_threshold_f: int = 8
    min_no_ev: float = 0.03
    max_position_per_slot_usd: float = 5.0
    max_exposure_per_city_usd: float = 50.0
    max_total_exposure_usd: float = 1000.0
    daily_loss_limit_usd: float = 50.0
    kelly_fraction: float = 0.5
    min_market_volume: float = 500.0
    max_slot_spread: float = 0.15
    # Absolute EV floor for TRIM (legacy; retained for back-compat).
    # Fix 4 reframes TRIM in terms of two gates:
    #   1. Relative: current EV < entry_ev * (1 - trim_ev_decay_ratio)
    #   2. Absolute: current EV < -min_trim_ev_absolute
    # A slot is trimmed when EITHER gate fires.  See
    # docs/fixes/2026-04-16-strategy-p0-fixes.md#fix-4.
    min_trim_ev: float = 0.02
    trim_ev_decay_ratio: float = 0.75
    min_trim_ev_absolute: float = 0.03
    # Bug #3 fix (2026-04-18): price-based stop that fires independent of EV.
    # When the NO price drops by trim_price_stop_ratio relative to entry
    # (default 25% — e.g. bought at 0.40, price now <= 0.30), trim regardless
    # of EV sign.  Catches the pathology where the market is moving hard
    # against us but EV still looks ~0 because of stale forecast inputs,
    # letting the position bleed to near-zero before the EV gates finally
    # fire.  Chicago 80-81 TRIMs at 95% loss on 2026-04-15 were this pattern.
    # Set to a value > 1.0 to disable.
    trim_price_stop_ratio: float = 0.25
    # Bug #1 fix (2026-04-18): reject entries where the model's win_prob
    # disagrees with the market-implied NO price by more than this many
    # points.  Applies to both standard-NO and locked-win branches via
    # the shared PriceDivergenceGate in src/strategy/gates.py.  Promoted
    # from module constant to config field on the 2026-04-18 PR#5 review
    # so future tuning doesn't need a code change + redeploy — analogous
    # treatment as locked_win_max_price.
    price_divergence_threshold: float = 0.50
    max_no_price: float = 0.85
    min_no_price: float = 0.20
    day_ahead_ev_discount: float = 0.7
    max_days_ahead: int = 2
    max_positions_per_event: int = 4
    # Auto-calibrate distance threshold from historical forecast error data
    auto_calibrate_distance: bool = True
    calibration_confidence: float = 0.90
    # Locked-win signals: buy NO on slots where daily max already exceeded upper bound
    enable_locked_wins: bool = True
    locked_win_kelly_fraction: float = 1.0
    max_locked_win_per_slot_usd: float = 10.0
    # Safety margin: wu_round(daily_max) must differ from slot boundary by at least
    # this many integer degrees to trigger locked-win (avoids X.5 rounding ambiguity)
    locked_win_margin_f: int = 2
    # Hard ceiling on NO price for locked-win entries.  Above this, the implied
    # margin (1 - price) is so thin that one Polymarket tick of paper→live
    # slippage (0.001) can flip the entry into negative EV.  Acts as a hard
    # gate alongside the `ev > 0` safety net inside evaluate_locked_win_signals.
    # Default 0.95 chosen empirically (production data 2026-04-17 — see
    # docs/fixes/2026-04-17-lockedwin-price-cap-rollback.md).  Tuneable via
    # config without redeploy if future market microstructure shifts.
    locked_win_max_price: float = 0.95
    # Hour (local) after which peak temperature window is considered over
    post_peak_hour: int = 17
    # Minutes without a new high (after post_peak_hour) to confirm daily max is final
    stability_window_minutes: int = 60
    # Hybrid exit: force-sell within N hours of settlement when distance is close
    force_exit_hours: float = 1.0
    # Cooldown after exiting a slot to prevent BUY→EXIT→BUY churn
    exit_cooldown_hours: float = 4.0
    # Dynamic threshold: scale distance by real-time ensemble spread ratio
    enable_spread_adjustment: bool = True
    # Fix 5: thin-liquidity cities get a reduced per-city exposure cap.
    # Rationale: Gamma-volume median for Miami / SF / Tampa / Orlando sits around
    # $800-1500 vs $3000+ elsewhere, yet they share the same exposure cap —
    # amplifying MTM losses when the market moves against us.  See
    # docs/fixes/2026-04-16-strategy-p0-fixes.md#fix-5.
    thin_liquidity_cities: frozenset[str] = field(default_factory=lambda: frozenset({
        "Miami", "San Francisco", "Tampa", "Orlando",
    }))
    thin_liquidity_exposure_ratio: float = 0.5


def get_strategy_variants() -> dict[str, dict]:
    """Four strategy variants testing different dimensions.

    All strategies share:
    - Auto-calibrated distance threshold (per-city)
    - Locked-win signals enabled
    - Hybrid exit mode (EV + distance + pre-settlement force)
    - Exit cooldown to prevent BUY→EXIT→BUY churn
    - NO-only signals (no YES, no LADDER)

    A = Conservative Far:  only distant NO, strict price cap, half Kelly
    B = Locked Aggressor:  same entry as A, but full Kelly on locked wins
    C = Close Range:       tighter distance (75% confidence), higher EV bar
    D = Quick Exit:        aggressive risk management, earlier force exit
    """
    return {
        "A": {
            # Conservative far-distance: fewest trades, highest safety margin
            # Uses half-Kelly even on locked wins (vs B's full Kelly)
            "max_no_price": 0.70,
            "kelly_fraction": 0.5,
            "locked_win_kelly_fraction": 0.5,
            "max_locked_win_per_slot_usd": 5.0,
            "max_positions_per_event": 3,
            "calibration_confidence": 0.90,
            "min_no_ev": 0.05,
            "max_position_per_slot_usd": 5.0,
            "max_exposure_per_city_usd": 30.0,
        },
        "B": {
            # Locked aggressor: same entry slots as A (same max_no_price / min_no_ev),
            # but 20% larger forecast-based sizing (kelly 0.6 vs A's 0.5) AND
            # full-Kelly on locked wins.  This ensures B ≠ A even when zero
            # locked-win signals fire in a window.  See docs/fixes/2026-04-16-strategy-p0-fixes.md#fix-1.
            "max_no_price": 0.70,
            "kelly_fraction": 0.6,
            "max_positions_per_event": 6,
            "calibration_confidence": 0.90,
            "min_no_ev": 0.05,
            "max_position_per_slot_usd": 5.0,
            "max_exposure_per_city_usd": 30.0,
            "locked_win_kelly_fraction": 1.0,
            "max_locked_win_per_slot_usd": 10.0,
        },
        "C": {
            # Close range: enters closer slots (75% confidence), demands higher EV
            "max_no_price": 0.75,
            "kelly_fraction": 0.3,
            "max_positions_per_event": 4,
            "calibration_confidence": 0.75,
            "min_no_ev": 0.06,
            "max_position_per_slot_usd": 3.0,
            "max_exposure_per_city_usd": 25.0,
        },
        "D": {
            # Quick exit: most aggressive risk management
            "max_no_price": 0.65,
            "kelly_fraction": 0.5,
            "max_positions_per_event": 4,
            "calibration_confidence": 0.90,
            "min_no_ev": 0.05,
            "max_position_per_slot_usd": 5.0,
            "max_exposure_per_city_usd": 30.0,
            "force_exit_hours": 2.0,
            "exit_cooldown_hours": 2.0,
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

    # Optional secret token protecting the /api/trigger endpoint.
    # Set TRIGGER_SECRET in .env; if empty the endpoint is unprotected (dev only).
    trigger_secret: str = ""

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
        trigger_secret=os.getenv("TRIGGER_SECRET", ""),
        strategy=StrategyConfig(**strategy_raw),
        scheduling=SchedulingConfig(**scheduling_raw),
        cities=[CityConfig(**c) for c in cities_raw],
    )
