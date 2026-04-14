"""Backtesting engine for the weather temperature trading strategy.

Uses historical weather data to simulate what would have happened if we
had traded the strategy over past dates. Validates that EV is actually
positive using real forecast errors, not theoretical assumptions.

Usage:
    python -m src.backtest.engine --days 365 --cities "New York,Dallas,Seattle"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from src.config import CityConfig, StrategyConfig, load_config
from src.strategy.temperature import wu_round
from src.weather.historical import (
    ForecastErrorDistribution,
    build_error_distribution,
    fetch_historical_actuals,
    fetch_historical_forecasts,
)

logger = logging.getLogger(__name__)


@dataclass
class SimulatedSlot:
    """A simulated temperature slot for backtesting."""
    lower_f: float
    upper_f: float
    # Simulated NO price: based on a simple pricing model
    # In reality, we'd need historical Polymarket prices, but those aren't available.
    # We use the "fair" price + house edge as a proxy.
    price_no: float = 0.0


@dataclass
class DayResult:
    """Result of trading one day for one city."""
    city: str
    day: date
    forecast_high_f: float
    actual_high_f: float
    forecast_error_f: float  # forecast - actual
    slots_traded: int
    no_wins: int
    no_losses: int
    gross_pnl: float  # before fees
    total_risked: float
    fees_paid: float = 0.0  # Polymarket taker fees
    net_pnl: float = 0.0  # after fees


@dataclass
class BacktestResult:
    """Aggregate backtest results."""
    city: str
    days_tested: int
    total_trades: int
    total_wins: int
    total_losses: int
    win_rate: float
    gross_pnl: float
    total_risked: float
    roi_pct: float  # gross_pnl / total_risked
    avg_daily_pnl: float
    max_daily_loss: float
    max_daily_profit: float
    # Fee tracking
    total_fees: float = 0.0
    net_pnl: float = 0.0
    net_roi_pct: float = 0.0
    # Per-distance breakdown
    distance_stats: dict[str, dict] = field(default_factory=dict)
    # Error distribution summary
    error_dist_summary: dict = field(default_factory=dict)


def _generate_slot_prices(
    actual_high_f: float,
    slot_lower_f: float,
    slot_upper_f: float,
    house_edge: float = 0.03,
) -> float:
    """Simulate a NO price for a slot based on what the "fair" market would price it.

    This is a simplified pricing model since we don't have historical Polymarket prices.
    The fair NO price = 1 - P(actual in slot). We add house edge on top.
    Uses a rough heuristic based on slot distance from actual.
    """
    # For backtesting purposes, we use a simple model:
    # The market doesn't know the actual, so we simulate what the market
    # would have priced based on typical forecast uncertainty.
    # A slot 10°F from the "expected" outcome would typically price NO at ~0.92-0.95.
    # This is conservative — real markets may have more or less edge.
    mid = (slot_lower_f + slot_upper_f) / 2
    # We don't use actual here (market doesn't know it), instead we work with the
    # forecast-based pricing model. This is set externally.
    return 0.0  # Will be computed by the caller


def _simulate_market_prices(
    forecast_high_f: float,
    slots: list[tuple[float, float]],
    forecast_std: float = 4.0,
    house_edge: float = 0.03,
    market_noise_std: float = 0.03,
) -> list[SimulatedSlot]:
    """Simulate market NO prices based on forecast.

    Models what a typical Polymarket weather market actually looks like:
    - Base pricing uses a WIDER uncertainty than the true forecast error
      (market participants don't have perfect models)
    - House edge: the sum of all NO prices > $1.00
    - Per-slot noise: prices aren't perfectly calibrated
    - Near-forecast slots are often mispriced (too cheap or expensive)

    This is intentionally adversarial to avoid overfitting the backtest.
    """
    import math
    import random

    # Market uses a wider sigma than the true forecast std
    # (participants are uncertain about their own uncertainty)
    market_sigma = forecast_std * 1.3

    simulated = []
    for lower, upper in slots:
        mid = (lower + upper) / 2
        distance = abs(mid - forecast_high_f)
        z = distance / max(market_sigma, 1.0)

        # Fair P(NO) based on market's (noisier) model
        fair_no = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))

        # Apply house edge (scales with how "safe" the bet looks)
        # Distant slots: higher edge because they look "free money"
        edge = house_edge * (1.0 + 0.5 * min(z, 3.0))
        market_no = fair_no + edge * (1.0 - fair_no)

        # Add per-slot noise (market inefficiency, but sometimes in our favor)
        noise = random.gauss(0, market_noise_std)
        market_no = max(0.50, min(0.98, market_no + noise))

        simulated.append(SimulatedSlot(
            lower_f=lower,
            upper_f=upper,
            price_no=round(market_no, 4),
        ))

    return simulated


def _compute_taker_fee(price: float, size_usd: float, fee_rate: float = 0.0125) -> float:
    """Compute Polymarket taker fee for weather markets.

    Fee structure (as of 2026):
    - Weather category: 1.25% base rate
    - Fee is probability-weighted: peaks at 50% price, decreases toward extremes
    - Fee = fee_rate * price * (1 - price) * 2 * size_in_shares
    - For takers only; makers pay 0%

    We assume all our orders are taker (market/aggressive limit orders).
    """
    # Probability-weighted fee: higher at 50/50, lower at extremes
    # This matches Polymarket's actual fee formula
    prob_weight = 2.0 * price * (1.0 - price)  # peaks at 0.5
    shares = size_usd / price if price > 0 else 0
    fee = fee_rate * prob_weight * shares * price
    return fee


def _run_day(
    city: str,
    day: date,
    forecast_high_f: float,
    actual_high_f: float,
    config: StrategyConfig,
    error_dist: ForecastErrorDistribution | None,
    slot_width: float = 2.0,
    slot_range: float = 30.0,
    taker_fee_rate: float = 0.0125,
) -> DayResult:
    """Simulate one day of trading for one city.

    1. Generate temperature slots around the forecast
    2. Simulate market prices (what the market would have priced)
    3. Apply our strategy: buy NO on distant slots
    4. Settle: check if actual temp landed in each slot
    5. Deduct Polymarket taker fees from P&L
    """
    # Generate slots spanning forecast ± slot_range
    base = int(forecast_high_f - slot_range)
    slots_range = []
    for i in range(int(2 * slot_range / slot_width)):
        lower = base + i * slot_width
        upper = lower + slot_width
        slots_range.append((lower, upper))

    # Simulate market prices
    simulated = _simulate_market_prices(
        forecast_high_f, slots_range,
        forecast_std=error_dist.std if error_dist else 4.0,
    )

    # Apply strategy: evaluate each slot
    slots_traded = 0
    no_wins = 0
    no_losses = 0
    gross_pnl = 0.0
    total_risked = 0.0
    total_fees = 0.0
    position_size = config.max_position_per_slot_usd

    for slot in simulated:
        distance = min(abs(forecast_high_f - slot.lower_f), abs(forecast_high_f - slot.upper_f))
        if slot.lower_f <= forecast_high_f <= slot.upper_f:
            distance = 0

        if distance < config.no_distance_threshold_f:
            continue

        # Estimate win probability
        if error_dist and error_dist._count >= 30:
            win_prob = error_dist.prob_no_wins(
                slot.lower_f, slot.upper_f, forecast_high_f,
            )
        else:
            import math
            sigma = 4.0
            z = distance / sigma
            win_prob = min(0.5 * (1.0 + math.erf(z / math.sqrt(2))), 0.99)

        # EV check (include fee estimate in EV calculation)
        entry_fee = _compute_taker_fee(slot.price_no, position_size, taker_fee_rate)
        fee_per_dollar = entry_fee / position_size if position_size > 0 else 0
        ev = win_prob * (1.0 - slot.price_no) - (1.0 - win_prob) * slot.price_no - fee_per_dollar
        if ev < config.min_no_ev:
            continue

        # Trade it
        slots_traded += 1
        total_risked += position_size

        # Entry fee (buying NO)
        total_fees += entry_fee

        # Settlement: did actual land in this slot?
        # Use wu_round (half-up) to match Weather Underground's whole-degree rounding.
        rounded_actual = wu_round(actual_high_f)
        actual_in_slot = int(slot.lower_f) <= rounded_actual <= int(slot.upper_f)
        if actual_in_slot:
            # NO loses — we lose our stake
            no_losses += 1
            gross_pnl -= position_size
        else:
            # NO wins — we gain (1 - price_no) * shares
            profit = position_size * (1.0 - slot.price_no) / slot.price_no
            no_wins += 1
            gross_pnl += profit

    net_pnl = gross_pnl - total_fees

    return DayResult(
        city=city,
        day=day,
        forecast_high_f=forecast_high_f,
        actual_high_f=actual_high_f,
        forecast_error_f=forecast_high_f - actual_high_f,
        slots_traded=slots_traded,
        no_wins=no_wins,
        no_losses=no_losses,
        gross_pnl=round(gross_pnl, 2),
        total_risked=round(total_risked, 2),
        fees_paid=round(total_fees, 2),
        net_pnl=round(net_pnl, 2),
    )


async def run_backtest(
    city: CityConfig,
    config: StrategyConfig,
    lookback_days: int = 365,
    error_dist: ForecastErrorDistribution | None = None,
) -> BacktestResult:
    """Run a full backtest for one city over the specified period."""
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=lookback_days)

    async with httpx.AsyncClient(timeout=60) as client:
        # Fetch historical data
        actuals = await fetch_historical_actuals(city, start, end, client)
        forecasts = await fetch_historical_forecasts(city, start, end, client)

        # Build error distribution if not provided
        if error_dist is None:
            error_dist = await build_error_distribution(
                city, lookback_days=lookback_days * 2, client=client,
            )

    # Create lookup maps
    actual_map = {d: h for d, h in actuals}
    forecast_map = {d: h for d, h in forecasts}

    # Run simulation for each day
    day_results: list[DayResult] = []
    for d in sorted(set(actual_map.keys()) & set(forecast_map.keys())):
        result = _run_day(
            city=city.name,
            day=d,
            forecast_high_f=forecast_map[d],
            actual_high_f=actual_map[d],
            config=config,
            error_dist=error_dist,
        )
        if result.slots_traded > 0:
            day_results.append(result)

    if not day_results:
        return BacktestResult(
            city=city.name, days_tested=0, total_trades=0,
            total_wins=0, total_losses=0, win_rate=0,
            gross_pnl=0, total_risked=0, roi_pct=0,
            avg_daily_pnl=0, max_daily_loss=0, max_daily_profit=0,
            error_dist_summary=error_dist.summary() if error_dist else {},
        )

    total_trades = sum(r.slots_traded for r in day_results)
    total_wins = sum(r.no_wins for r in day_results)
    total_losses = sum(r.no_losses for r in day_results)
    gross_pnl = sum(r.gross_pnl for r in day_results)
    total_risked = sum(r.total_risked for r in day_results)
    daily_pnls = [r.gross_pnl for r in day_results]

    # Per-distance breakdown
    distance_buckets: dict[str, list[DayResult]] = {}
    for r in day_results:
        bucket = f"{int(abs(r.forecast_error_f))}°F"
        distance_buckets.setdefault(bucket, []).append(r)

    distance_stats = {}
    for bucket, results in sorted(distance_buckets.items()):
        w = sum(r.no_wins for r in results)
        l = sum(r.no_losses for r in results)
        distance_stats[bucket] = {
            "days": len(results),
            "wins": w,
            "losses": l,
            "win_rate": round(w / (w + l), 4) if (w + l) > 0 else 0,
        }

    return BacktestResult(
        city=city.name,
        days_tested=len(day_results),
        total_trades=total_trades,
        total_wins=total_wins,
        total_losses=total_losses,
        win_rate=round(total_wins / total_trades, 4) if total_trades > 0 else 0,
        gross_pnl=round(gross_pnl, 2),
        total_risked=round(total_risked, 2),
        roi_pct=round(gross_pnl / total_risked * 100, 2) if total_risked > 0 else 0,
        avg_daily_pnl=round(gross_pnl / len(day_results), 2),
        max_daily_loss=round(min(daily_pnls), 2),
        max_daily_profit=round(max(daily_pnls), 2),
        distance_stats=distance_stats,
        error_dist_summary=error_dist.summary() if error_dist else {},
    )


def print_backtest_report(results: list[BacktestResult]) -> None:
    """Print a formatted backtest report."""
    print("\n" + "=" * 80)
    print("POLYMARKET WEATHER TRADING STRATEGY — BACKTEST REPORT")
    print("=" * 80)

    total_gross = 0.0
    total_fees = 0.0
    total_net = 0.0
    total_risked = 0.0
    total_trades = 0
    total_wins = 0
    total_losses = 0

    for r in results:
        total_gross += r.gross_pnl
        total_fees += r.total_fees
        total_net += r.net_pnl
        total_risked += r.total_risked
        total_trades += r.total_trades
        total_wins += r.total_wins
        total_losses += r.total_losses

        print(f"\n{'─' * 60}")
        print(f"  {r.city}")
        print(f"{'─' * 60}")
        print(f"  Days tested:     {r.days_tested}")
        print(f"  Total trades:    {r.total_trades}")
        print(f"  Wins / Losses:   {r.total_wins} / {r.total_losses}")
        print(f"  Win rate:        {r.win_rate * 100:.1f}%")
        print(f"  Gross P&L:       ${r.gross_pnl:+.2f}")
        print(f"  Fees paid:       ${r.total_fees:.2f}")
        print(f"  Net P&L:         ${r.net_pnl:+.2f}")
        print(f"  Total risked:    ${r.total_risked:.2f}")
        print(f"  Gross ROI:       {r.roi_pct:+.2f}%")
        print(f"  Net ROI:         {r.net_roi_pct:+.2f}%")
        print(f"  Max daily loss:  ${r.max_daily_loss:.2f}")

        if r.error_dist_summary:
            s = r.error_dist_summary
            print(f"  Forecast err:    mean={s.get('mean_error', 0):+.1f}  std={s.get('std_error', 0):.1f}")

    print(f"\n{'=' * 80}")
    print("AGGREGATE RESULTS")
    print(f"{'=' * 80}")
    overall_win_rate = total_wins / total_trades * 100 if total_trades > 0 else 0
    gross_roi = total_gross / total_risked * 100 if total_risked > 0 else 0
    net_roi = total_net / total_risked * 100 if total_risked > 0 else 0
    print(f"  Cities:          {len(results)}")
    print(f"  Total trades:    {total_trades}")
    print(f"  Win rate:        {overall_win_rate:.1f}%")
    print(f"  Gross P&L:       ${total_gross:+.2f}")
    print(f"  Fees paid:       ${total_fees:.2f}  ({total_fees/total_gross*100:.1f}% of gross)" if total_gross > 0 else f"  Fees paid:       ${total_fees:.2f}")
    print(f"  Net P&L:         ${total_net:+.2f}")
    print(f"  Total risked:    ${total_risked:.2f}")
    print(f"  Gross ROI:       {gross_roi:+.2f}%")
    print(f"  Net ROI:         {net_roi:+.2f}%")

    verdict = "PROFITABLE" if total_net > 0 else "NOT PROFITABLE"
    symbol = "[OK]" if total_net > 0 else "[!!]"
    print(f"\n  Verdict: {symbol} Strategy is {verdict} after fees")
    print(f"{'=' * 80}\n")


async def main(args: argparse.Namespace) -> None:
    config = load_config()

    # Filter cities if specified
    if args.cities:
        city_names = [c.strip() for c in args.cities.split(",")]
        cities = [c for c in config.cities if c.name in city_names]
        if not cities:
            print(f"No matching cities found. Available: {[c.name for c in config.cities]}")
            sys.exit(1)
    else:
        cities = config.cities[:args.max_cities]

    print(f"Running backtest: {len(cities)} cities, {args.days} days lookback")
    print(f"Strategy: NO threshold={config.strategy.no_distance_threshold_f}°F, min EV={config.strategy.min_no_ev}")

    results = []
    for city in cities:
        print(f"  Backtesting {city.name}...", end=" ", flush=True)
        result = await run_backtest(city, config.strategy, lookback_days=args.days)
        results.append(result)
        print(f"done ({result.total_trades} trades, ${result.gross_pnl:+.2f})")

    print_backtest_report(results)

    # Save raw results
    output_path = Path("data") / "backtest_results.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(
        [{"city": r.city, "days": r.days_tested, "trades": r.total_trades,
          "wins": r.total_wins, "losses": r.total_losses, "win_rate": r.win_rate,
          "pnl": r.gross_pnl, "risked": r.total_risked, "roi_pct": r.roi_pct,
          "error_dist": r.error_dist_summary}
         for r in results],
        indent=2,
    ))
    print(f"Raw results saved to {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest weather trading strategy")
    parser.add_argument("--days", type=int, default=365, help="Lookback period in days")
    parser.add_argument("--cities", type=str, default="", help="Comma-separated city names (default: all)")
    parser.add_argument("--max-cities", type=int, default=5, help="Max cities if not specified")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    asyncio.run(main(args))
