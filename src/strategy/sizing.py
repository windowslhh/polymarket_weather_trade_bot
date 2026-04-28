"""Position sizing using half-Kelly criterion with exposure caps."""
from __future__ import annotations

import logging

from src.config import StrategyConfig
from src.markets.models import TradeSignal

logger = logging.getLogger(__name__)


def compute_size(
    signal: TradeSignal,
    city_exposure_usd: float,
    total_exposure_usd: float,
    config: StrategyConfig,
) -> float:
    """Compute the USD size for a trade signal.

    Uses half-Kelly criterion capped by per-slot, per-city, and global limits.

    Args:
        signal: The trade signal to size.
        city_exposure_usd: Current total exposure for this city.
        total_exposure_usd: Current total exposure across all cities.
        config: Strategy configuration.

    Returns:
        Recommended position size in USD. Returns 0 if no trade should be made.
    """
    price = signal.price
    if price <= 0 or price >= 1:
        return 0.0

    win_prob = signal.estimated_win_prob
    if win_prob <= 0 or win_prob >= 1:
        return 0.0

    # Kelly criterion: f* = (p * net_odds - q) / net_odds
    # where p = win probability, q = 1-p, net_odds = (1-price)/price
    # Result is a fraction in [0, 1] representing signal strength.
    net_odds = (1.0 - price) / price
    q = 1.0 - win_prob
    kelly_full = (win_prob * net_odds - q) / net_odds if net_odds > 0 else 0.0

    if kelly_full <= 0:
        return 0.0

    # Use higher Kelly fraction and cap for locked wins (near-certain bets)
    frac = config.locked_win_kelly_fraction if signal.is_locked_win else config.kelly_fraction
    slot_cap = config.max_locked_win_per_slot_usd if signal.is_locked_win else config.max_position_per_slot_usd

    # Signal-proportional sizing (intentional design choice):
    # size = kelly_full × frac × slot_cap
    #
    # This is NOT the traditional "fraction of bankroll" Kelly formula.
    # Instead, kelly_full (0→1) acts as a signal-strength scalar that scales
    # the maximum per-slot bet down based on how confident the strategy is:
    #   - High-EV signal (kelly_full ≈ 0.5): invest ~25% of slot cap (× 0.5 frac)
    #   - Weak signal (kelly_full ≈ 0.1): invest ~5% of slot cap
    #   - Maximum bet (kelly_full = 1.0, locked win): up to full slot cap
    #
    # Exposure caps in apply_caps() enforce the hard risk limits regardless.
    kelly_fraction = kelly_full * frac
    size_usd = kelly_fraction * slot_cap

    # Apply caps
    # 1. Per-slot cap
    size_usd = min(size_usd, slot_cap)

    # 2. Per-city remaining capacity
    city_remaining = config.max_exposure_per_city_usd - city_exposure_usd
    if city_remaining <= 0:
        return 0.0
    size_usd = min(size_usd, city_remaining)

    # 3. Global remaining capacity
    global_remaining = config.max_total_exposure_usd - total_exposure_usd
    if global_remaining <= 0:
        return 0.0
    size_usd = min(size_usd, global_remaining)

    # Minimum viable order size (avoid dust orders)
    if size_usd < 0.10:
        return 0.0

    # Polymarket CLOB rejects orders below 5 shares OR $1 notional (2026-04-28).
    # Pre-compute the would-be share count from the *rounded* USD size so the
    # gate matches what the executor will actually submit — otherwise a 4.99
    # share signal could squeak past sizing and be 400'd by Polymarket.
    rounded = round(size_usd, 2)
    shares = rounded / price if price > 0 else 0.0
    if shares < config.min_order_size_shares:
        logger.info(
            "Sizing skip [SIZE_BELOW_MIN_SHARES]: %.4f shares < min %.2f (size=$%.2f, price=%.4f)",
            shares, config.min_order_size_shares, rounded, price,
        )
        return 0.0
    if rounded < config.min_order_amount_usd:
        logger.info(
            "Sizing skip [AMOUNT_BELOW_MIN_USD]: $%.4f < min $%.2f (shares=%.2f, price=%.4f)",
            rounded, config.min_order_amount_usd, shares, price,
        )
        return 0.0

    return rounded
