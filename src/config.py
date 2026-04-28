from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent


# FIX-M8: freeze leaf configs so no code path can mutate them at runtime.
# AppConfig stays mutable because main.py flips dry_run / paper on it
# right after load based on CLI args; migrating that to `replace()` is
# a heavier refactor deferred post go-live.
@dataclass(frozen=True)
class CityConfig:
    name: str
    icao: str
    lat: float
    lon: float
    tz: str = "America/New_York"  # IANA timezone for local date grouping


@dataclass(frozen=True)
class StrategyConfig:
    no_distance_threshold_f: int = 8
    min_no_ev: float = 0.03
    max_position_per_slot_usd: float = 5.0
    max_exposure_per_city_usd: float = 50.0
    # 2026-04-28: dropped 1000 → 200 to match the live wallet's actual
    # capital pool.  Kelly sizing already keeps per-position USD small
    # ($1-10), so the practical effect is letting the global ceiling
    # bind on real aggregate exposure instead of a 5× notional capacity
    # that never existed.
    max_total_exposure_usd: float = 200.0
    # 2026-04-28: tightened 75 → 50 to align with the $200 live wallet
    # (max_total_exposure dropped 1000 → 200 in the same change).  $50
    # is a 25% drawdown — still wide enough to absorb an adverse
    # weather streak across 30 cities of kelly=0.5 forecast entries +
    # full-Kelly locked wins, but tight enough that hitting it actually
    # means something is wrong.  Prior 75 was sized against a notional
    # $200 cap that wasn't enforced.  See
    # docs/fixes/2026-04-17-lockedwin-price-cap-rollback.md for
    # fee-floor reasoning.
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
    # FIX-02 (2026-04-24): tightened from 0.25 → 0.20 now that TRIM runs
    # every 15 min (not just 60).  Entry=0.645→exit=0.180 (Chicago 04-15,
    # 72% loss) could have been caught an hour earlier with 0.20 at a
    # 15-min cadence; keeping 0.25 wastes the latency gain.
    trim_price_stop_ratio: float = 0.20
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
    # FIX-17 (2026-04-24): dropped 0.95 → 0.90.  Post-rollback production
    # data still showed EV ≈ 0 at 0.93+; the 0.90 cap gives ~3¢ of slippage
    # slack before a 1-tick adverse fill turns the entry negative.
    locked_win_max_price: float = 0.90
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
    # cycle-frequency-fix (2026-04-27): when True, the 15-min position
    # check runs evaluate_no_signals on the cached event list with
    # freshly-refreshed Gamma prices.  Closes the 60-min sampling gap
    # that caused local paper to miss the Miami 88-89°F entry window
    # (price was at 0.70 for ~15 min, fell between local cycles).
    # Cheap by design: the entry scan does NOT re-discover markets,
    # does NOT refresh forecasts, and does NOT pull METAR — those are
    # already kept fresh by refresh_forecasts() / METAR sync.  Only
    # Gamma outcomePrices are re-fetched.  Default ON because this is
    # a bug fix, not a feature.
    enable_position_check_entry_scan: bool = True
    # Fix 5: thin-liquidity cities get a reduced per-city exposure cap.
    # Rationale: Gamma-volume median for Miami / SF / Tampa / Orlando sits around
    # $800-1500 vs $3000+ elsewhere, yet they share the same exposure cap —
    # amplifying MTM losses when the market moves against us.  See
    # docs/fixes/2026-04-16-strategy-p0-fixes.md#fix-5.
    thin_liquidity_cities: frozenset[str] = field(default_factory=lambda: frozenset({
        "Miami", "San Francisco", "Tampa", "Orlando",
    }))
    thin_liquidity_exposure_ratio: float = 0.5
    # 2026-04-28: Polymarket CLOB rejects orders below 5 shares OR $1 of
    # notional.  Pre-Phase 5 the bot would happily produce sub-minimum
    # signals (sizing rounding, full-Kelly on small slots) and Polymarket
    # would 400-out every one of them — wasted retries, dirty alert
    # channel.  Both gates run on BUY (in sizing) and SELL (in the
    # executor) paths so the rejection happens once, in DB+log, instead
    # of once per network round-trip.
    min_order_size_shares: float = 5.0
    min_order_amount_usd: float = 1.0

    # FIX-17 (2026-04-24): per-variant city filter.  Empty = all cities
    # allowed (default for B/C).  D' uses this to restrict its narrow
    # high-EV profile to cities whose historical forecast error is small
    # enough for the 0.08 EV threshold to actually fire.
    city_whitelist: frozenset[str] = field(default_factory=frozenset)


def get_strategy_variants() -> dict[str, dict]:
    """Active strategy variants — single source of truth for both runtime
    behaviour and dashboard display.

    Each value is a flat dict that mixes two concerns:

    - **StrategyConfig overrides**: top-level keys whose names match
      ``StrategyConfig`` field names (``max_no_price``, ``kelly_fraction``,
      ...).  These are spread into ``replace(StrategyConfig(), **overrides)``
      to build the per-variant config.

    - **Display metadata**: a single ``_meta`` key holding the human-facing
      label / description / colour / Jinja CSS class that the web layer
      and templates read.  The underscore prefix marks it as not-a-config
      field; ``strategy_params()`` strips it before splatting into
      ``replace`` so the dataclass doesn't trip on an unknown field.

    Adding a new variant should be a single edit here — the rebalancer,
    web app, and templates all pull metadata off whatever this returns.

    Shared across all variants (handled by ``StrategyConfig`` defaults):
    - Auto-calibrated distance threshold (per-city)
    - Locked-win signals enabled
    - Hybrid exit mode (EV + distance + pre-settlement force)
    - Exit cooldown to prevent BUY→EXIT→BUY churn
    - NO-only signals (no YES, no LADDER)

    DB schema retains the ``strategy`` column with values A/B/C/D allowed
    (Y6 trigger preserved) so historical rows from earlier variant
    line-ups remain queryable for audit even when not active here.
    """
    # ──────────────────────────────────────────────────────────────────
    # B and C disabled for live cutover (user request 2026-04-27).
    # Going live with D-only.  Restore by un-commenting the two blocks
    # inside the dict below verbatim — schema, _meta colours, and
    # DB-allowed strategy column values (A/B/C/D, Y6 trigger) are all
    # still in place.
    # ──────────────────────────────────────────────────────────────────
    return {
        # "B": {
        #     "max_no_price": 0.70,
        #     "kelly_fraction": 0.5,
        #     "max_positions_per_event": 4,
        #     "min_no_ev": 0.05,
        #     "max_position_per_slot_usd": 5.0,
        #     # Per-city cap dropped 20 → 10 so the three live variants
        #     # together stay within the same per-city ceiling B alone
        #     # used (B@20 ≡ B+C+D@10 each, summing to $30/city).  Without
        #     # the rebalance the combined exposure would 1.5× silently.
        #     "max_exposure_per_city_usd": 10.0,
        #     "locked_win_kelly_fraction": 1.0,
        #     "max_locked_win_per_slot_usd": 10.0,
        #     "_meta": {
        #         "label": "B (Conservative)",
        #         "description": "max_no_price=0.70 — baseline production variant",
        #         "color": "#3b82f6",     # blue-500
        #         "tag_class": "tag-info",
        #     },
        # },
        # # Control groups C and D widen max_no_price beyond B to test
        # # whether the EV gate alone is enough to filter out fee-eaten
        # # entries, OR whether the price cap is actually doing work.
        # # Both share the same kelly / EV / Safe-cap profile as B so
        # # comparisons isolate the price-cap dimension cleanly.
        # "C": {
        #     "max_no_price": 0.75,
        #     "kelly_fraction": 0.5,
        #     "max_positions_per_event": 4,
        #     "min_no_ev": 0.05,
        #     "max_position_per_slot_usd": 5.0,
        #     "max_exposure_per_city_usd": 10.0,
        #     "locked_win_kelly_fraction": 1.0,
        #     "max_locked_win_per_slot_usd": 10.0,
        #     "_meta": {
        #         "label": "C (Moderate)",
        #         "description": "max_no_price=0.75 — control group, +5pt cap",
        #         "color": "#f59e0b",     # amber-500
        #         "tag_class": "tag-warning",
        #     },
        # },
        "D": {
            "max_no_price": 0.80,
            "kelly_fraction": 0.5,
            "max_positions_per_event": 4,
            "min_no_ev": 0.05,
            # 2026-04-28 micro-tune: bumped 5 → 10 so half-Kelly sizing
            # at typical NO prices (0.5–0.8) clears Polymarket's hard
            # 5-share minimum (min_order_size_shares=5.0).  At $5/slot,
            # Kelly was producing 4.0–4.3 shares — every signal tripped
            # SIZE_BELOW_MIN_SHARES and the bot went ~10h without an
            # entry.  Cumulative exposure still bounded by
            # max_total_exposure_usd=200; full-Kelly locked wins remain
            # capped at max_locked_win_per_slot_usd=10.
            "max_position_per_slot_usd": 10.0,
            # 2026-04-28: D running solo (B/C disabled); $50/city so the
            # variant has room to compound across the day instead of being
            # capped at 2 entries.  Global $200 exposure ceiling and the
            # $50 daily-loss breaker remain in force.
            "max_exposure_per_city_usd": 50.0,
            "locked_win_kelly_fraction": 1.0,
            "max_locked_win_per_slot_usd": 10.0,
            "_meta": {
                "label": "D (Aggressive)",
                "description": "max_no_price=0.80, slot $10 — clears 5-share min",
                "color": "#ef4444",     # red-500
                "tag_class": "tag-danger",
            },
        },
    }


def strategy_params(variant: dict) -> dict:
    """Drop ``_meta`` (and any other underscore-prefixed) keys from a
    variant dict so the remainder can be splatted into
    ``dataclasses.replace(StrategyConfig(), **)`` without a TypeError.

    Centralised here rather than inlined at every call site so a future
    addition (``_origin``, ``_deprecated_at``, etc.) propagates without
    hunting down every consumer.
    """
    return {k: v for k, v in variant.items() if not k.startswith("_")}


@dataclass(frozen=True)
class SchedulingConfig:
    discovery_interval_minutes: int = 15
    rebalance_interval_minutes: int = 60
    pnl_snapshot_interval_hours: int = 24


@dataclass
class AppConfig:
    # Polymarket L2 API credentials.  Optional: when None, ClobClient
    # calls ``create_or_derive_api_creds()`` on the underlying py-clob-client
    # to derive them from the L1 private key.  Pre-provisioned creds in
    # .env still work (back-compat) and short-circuit the derive call.
    polymarket_api_key: str | None = None
    polymarket_secret: str | None = None
    polymarket_passphrase: str | None = None
    # L1 private key (signing key).  Loaded from macOS Keychain in live
    # mode by src.security.load_eth_private_key; .env fallback is honored
    # for non-macOS environments (VPS paper today).  Stays as ``str = ""``
    # rather than Optional because the rest of the codebase reads it
    # without None-handling — the empty default mirrors paper-mode where
    # no key is needed.
    eth_private_key: str = ""
    # Polymarket proxy / Gnosis Safe address that holds the actual USDC
    # on-chain.  Polymarket.com web users have one of these by default;
    # direct EOA setups do not.  When set, ClobClient signs orders with
    # signature_type=2 (POLY_GNOSIS_SAFE).  When None, signature_type=0
    # (direct EOA on-chain wallet).  ``load_config`` normalises the env
    # placeholder ``0x`` / ``0x0`` to None so an unfilled .env stub
    # doesn't accidentally trip proxy mode against a non-existent Safe.
    funder_address: str | None = None

    # Optional weather API key
    openweathermap_api_key: str = ""

    # Alert webhook URL (Telegram/Discord/Slack)
    alert_webhook_url: str = ""

    # Optional secret token protecting the /api/trigger endpoint.
    # Set TRIGGER_SECRET in .env; if empty the endpoint is unprotected (dev only).
    trigger_secret: str = ""

    # Optional Polygon RPC URL override.  Currently unused — reserved for
    # future on-chain helpers (settlement redemption, balance queries).
    polygon_rpc_url: str = ""

    # Sub-configs
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    cities: list[CityConfig] = field(default_factory=list)

    # Runtime flags: dry_run = print only; paper = simulate fills + track positions
    dry_run: bool = False
    paper: bool = False
    db_path: Path = field(default_factory=lambda: _ROOT / "data" / "bot.db")


def _env_or_none(key: str, *placeholders: str) -> str | None:
    """Read an env var; collapse empty / whitespace / placeholder to None.

    Treating ``""`` and the literal placeholder ``0x`` (used in .env.example
    so the file passes a syntax sanity check while flagging the field as
    unset) as None means downstream code can use ``if not value`` without
    extra normalisation, and a half-edited .env can't accidentally drive
    the bot into proxy-wallet mode against a non-existent address.
    """
    raw = os.getenv(key, "").strip()
    if not raw or raw in placeholders:
        return None
    return raw


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
        polymarket_api_key=_env_or_none("POLYMARKET_API_KEY"),
        polymarket_secret=_env_or_none("POLYMARKET_SECRET"),
        polymarket_passphrase=_env_or_none("POLYMARKET_PASSPHRASE"),
        eth_private_key=os.getenv("ETH_PRIVATE_KEY", ""),
        # 0x / 0x0 are .env.example placeholders — reject so an unfilled
        # FUNDER_ADDRESS line doesn't flip the bot into proxy-wallet mode.
        funder_address=_env_or_none("FUNDER_ADDRESS", "0x", "0x0"),
        polygon_rpc_url=os.getenv("POLYGON_RPC_URL", ""),
        openweathermap_api_key=os.getenv("OPENWEATHERMAP_API_KEY", ""),
        alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL", ""),
        trigger_secret=os.getenv("TRIGGER_SECRET", ""),
        strategy=StrategyConfig(**strategy_raw),
        scheduling=SchedulingConfig(**scheduling_raw),
        cities=[CityConfig(**c) for c in cities_raw],
    )
