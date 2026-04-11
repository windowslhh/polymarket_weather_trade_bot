#!/usr/bin/env python3
"""Monte Carlo backtest using local forecast error distributions.

Simulates 730 days of trading per city using empirical forecast errors
from data/history/*_errors.json. Compares all 4 strategy variants (A-D)
side-by-side. No network access needed.

Usage:
    .venv/bin/python scripts/monte_carlo_backtest.py
    .venv/bin/python scripts/monte_carlo_backtest.py --days 365 --runs 5
    .venv/bin/python scripts/monte_carlo_backtest.py --cities "Seattle,Miami"
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import CityConfig, StrategyConfig, get_strategy_variants, load_config
from src.strategy.calibrator import calibrate_distance_threshold
from src.weather.historical import ForecastErrorDistribution


# ── Data Structures ─────────────────────────────────────────────────

@dataclass
class DayTrade:
    slot_lower: float
    slot_upper: float
    price_no: float
    size_usd: float
    win: bool  # NO wins = actual NOT in slot
    pnl: float
    fee: float


@dataclass
class DayResult:
    city: str
    strategy: str
    forecast_high: float
    actual_high: float
    error: float
    trades: list[DayTrade] = field(default_factory=list)

    @property
    def gross_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def fees(self) -> float:
        return sum(t.fee for t in self.trades)

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.win)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if not t.win)

    @property
    def risked(self) -> float:
        return sum(t.size_usd for t in self.trades)


@dataclass
class StrategyResult:
    strategy: str
    city: str
    days_tested: int
    total_trades: int
    total_wins: int
    total_losses: int
    gross_pnl: float
    net_pnl: float
    total_fees: float
    total_risked: float
    max_daily_loss: float
    max_daily_profit: float
    max_drawdown: float
    sharpe_ratio: float


# ── Pricing Model ───────────────────────────────────────────────────

def simulate_market_prices(
    forecast_high: float,
    slots: list[tuple[float, float]],
    forecast_std: float = 4.0,
) -> list[tuple[float, float, float]]:
    """Simulate NO prices for slots. Returns [(lower, upper, price_no), ...].

    Calibrated against real Polymarket weather market data:
    - Real entry prices: mean $0.677, 70% in 0.50-0.70 range
    - Market makers use much wider uncertainty than NWS forecast error
    - Low-liquidity weather markets are significantly less efficient
    - Near-forecast (0-3°F): NO 0.40-0.60 (contested)
    - Medium distance (4-8°F): NO 0.55-0.75 (bot's sweet spot)
    - Far (9-15°F): NO 0.70-0.85
    - Very far (>15°F): NO 0.80-0.96
    """
    # Market makers price with ~5x wider sigma than actual forecast error.
    # This reflects: low liquidity, non-expert participants, wide spreads,
    # and desire for two-sided order flow.  Calibrated so that at 4-6°F
    # distance, NO prices land in the 0.55-0.70 range (matching real data).
    market_sigma = forecast_std * 5.0
    result = []
    for lower, upper in slots:
        mid = (lower + upper) / 2
        distance = abs(mid - forecast_high)
        z = distance / max(market_sigma, 1.0)

        # Fair P(NO) via normal CDF
        fair_no = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))

        # Tiny house edge — real weather markets vary, no consistent edge
        market_no = fair_no * 0.99 + 0.01

        # Price ceiling: even very safe bets don't trade above ~0.96
        market_no = min(market_no, 0.96)

        # Significant noise (real markets have wide bid-ask, random flow)
        noise = random.gauss(0, 0.04)
        market_no = max(0.30, min(0.96, market_no + noise))

        result.append((lower, upper, round(market_no, 4)))
    return result


def compute_taker_fee(price: float, size_usd: float, fee_rate: float = 0.0125) -> float:
    """Polymarket probability-weighted taker fee."""
    prob_weight = 2.0 * price * (1.0 - price)
    shares = size_usd / price if price > 0 else 0
    return fee_rate * prob_weight * shares * price


# ── Kelly Sizing ────────────────────────────────────────────────────

def kelly_size(
    win_prob: float,
    price_no: float,
    config: StrategyConfig,
    city_exposure: float,
    is_locked: bool = False,
) -> float:
    """Half-Kelly (or full for locked wins) position sizing."""
    net_odds = (1.0 - price_no) / price_no
    kelly_full = (win_prob * net_odds - (1.0 - win_prob)) / net_odds
    if kelly_full <= 0:
        return 0.0

    frac = config.locked_win_kelly_fraction if is_locked else config.kelly_fraction
    slot_cap = config.max_locked_win_per_slot_usd if is_locked else config.max_position_per_slot_usd
    size = kelly_full * frac * slot_cap

    # Cap by city exposure
    remaining = config.max_exposure_per_city_usd - city_exposure
    size = min(size, remaining)

    return max(size, 0.0) if size >= 0.10 else 0.0


# ── Day Simulation ──────────────────────────────────────────────────

def simulate_day(
    forecast_high: float,
    actual_high: float,
    config: StrategyConfig,
    error_dist: ForecastErrorDistribution,
    city: str,
    strategy: str,
    slot_width: float = 2.0,
    slot_range: float = 30.0,
) -> DayResult:
    """Simulate one day of NO trading for a single strategy variant."""
    result = DayResult(
        city=city,
        strategy=strategy,
        forecast_high=forecast_high,
        actual_high=actual_high,
        error=forecast_high - actual_high,
    )

    # Auto-calibrate distance threshold
    cal_dist = calibrate_distance_threshold(error_dist, config.calibration_confidence)
    effective_config = StrategyConfig(
        no_distance_threshold_f=round(cal_dist),
        min_no_ev=config.min_no_ev,
        max_no_price=config.max_no_price,
        kelly_fraction=config.kelly_fraction,
        max_position_per_slot_usd=config.max_position_per_slot_usd,
        max_exposure_per_city_usd=config.max_exposure_per_city_usd,
        locked_win_kelly_fraction=config.locked_win_kelly_fraction,
        max_locked_win_per_slot_usd=config.max_locked_win_per_slot_usd,
        max_positions_per_event=config.max_positions_per_event,
    )

    # Generate slots
    base = int(forecast_high - slot_range)
    slots = [(base + i * slot_width, base + (i + 1) * slot_width)
             for i in range(int(2 * slot_range / slot_width))]

    # Simulate market prices
    priced_slots = simulate_market_prices(
        forecast_high, slots, forecast_std=error_dist.std,
    )

    city_exposure = 0.0
    positions_taken = 0

    for lower, upper, price_no in priced_slots:
        if positions_taken >= effective_config.max_positions_per_event:
            break
        if price_no > effective_config.max_no_price:
            continue

        # Distance from forecast to slot
        if lower <= forecast_high <= upper:
            distance = 0.0
        else:
            distance = min(abs(forecast_high - lower), abs(forecast_high - upper))

        if distance < effective_config.no_distance_threshold_f:
            continue

        # Win probability from empirical distribution
        win_prob = error_dist.prob_no_wins(lower, upper, forecast_high)

        # EV check (include fee estimate)
        entry_fee_est = compute_taker_fee(price_no, effective_config.max_position_per_slot_usd)
        fee_pct = entry_fee_est / effective_config.max_position_per_slot_usd
        ev = win_prob * (1.0 - price_no) - (1.0 - win_prob) * price_no - fee_pct

        if ev < effective_config.min_no_ev:
            continue

        # Size with Kelly
        size = kelly_size(win_prob, price_no, effective_config, city_exposure)
        if size <= 0:
            continue

        # Execute trade
        fee = compute_taker_fee(price_no, size)
        actual_in_slot = lower <= actual_high <= upper
        if actual_in_slot:
            pnl = -size  # NO loses
        else:
            pnl = size * (1.0 - price_no) / price_no  # NO wins

        result.trades.append(DayTrade(
            slot_lower=lower,
            slot_upper=upper,
            price_no=price_no,
            size_usd=round(size, 2),
            win=not actual_in_slot,
            pnl=round(pnl, 2),
            fee=round(fee, 4),
        ))

        city_exposure += size
        positions_taken += 1

    return result


# ── Main Backtest ───────────────────────────────────────────────────

def load_error_distributions(cities: list[CityConfig]) -> dict[str, ForecastErrorDistribution]:
    """Load cached error distributions from data/history/."""
    dists = {}
    for city in cities:
        path = ROOT / "data" / "history" / f"{city.icao}_errors.json"
        if path.exists():
            data = json.loads(path.read_text())
            dists[city.name] = ForecastErrorDistribution(city.name, data["errors"])
    return dists


def run_backtest(
    cities: list[CityConfig],
    n_days: int = 730,
    n_runs: int = 3,
    seed: int = 42,
) -> dict[str, list[StrategyResult]]:
    """Run Monte Carlo backtest across all cities and strategies.

    For each city:
    - Sample n_days forecast/actual pairs from the error distribution
    - Run all 4 strategy variants on the same data
    - Repeat n_runs times with different random seeds for market noise
    """
    random.seed(seed)
    dists = load_error_distributions(cities)
    variants = get_strategy_variants()
    base_config = StrategyConfig()

    all_results: dict[str, list[StrategyResult]] = {s: [] for s in variants}

    for city in cities:
        dist = dists.get(city.name)
        if not dist or dist._count < 30:
            print(f"  Skip {city.name}: insufficient error data")
            continue

        # Typical forecast high for this city (estimate from errors)
        # Use mean of error distribution shifted around a base of 70°F
        # In practice we sample both forecast and actual from the distribution
        base_temp = 70.0  # representative baseline

        for run_i in range(n_runs):
            run_seed = seed + run_i * 1000 + hash(city.name) % 10000
            random.seed(run_seed)

            # Generate n_days of (forecast, actual) pairs from error distribution
            day_pairs: list[tuple[float, float]] = []
            for _ in range(n_days):
                # Sample a random error from the empirical distribution
                error = random.choice(dist._errors)
                # Randomize the base forecast (seasonal variation)
                seasonal_var = random.gauss(0, 10)  # ±10°F seasonal swing
                forecast_high = base_temp + seasonal_var
                actual_high = forecast_high - error  # error = forecast - actual
                day_pairs.append((forecast_high, actual_high))

            # Run each strategy variant on same data
            for strat_name, overrides in variants.items():
                strat_cfg = StrategyConfig(**{**vars(base_config), **overrides})
                day_results: list[DayResult] = []

                for forecast_high, actual_high in day_pairs:
                    dr = simulate_day(
                        forecast_high, actual_high,
                        strat_cfg, dist, city.name, strat_name,
                    )
                    if dr.trades:
                        day_results.append(dr)

                # Aggregate
                if not day_results:
                    continue

                daily_pnls = [r.net_pnl for r in day_results]
                cumulative = []
                running = 0.0
                peak = 0.0
                max_dd = 0.0
                for pnl in daily_pnls:
                    running += pnl
                    cumulative.append(running)
                    peak = max(peak, running)
                    dd = peak - running
                    max_dd = max(max_dd, dd)

                avg_pnl = sum(daily_pnls) / len(daily_pnls) if daily_pnls else 0
                std_pnl = (sum((p - avg_pnl) ** 2 for p in daily_pnls) / len(daily_pnls)) ** 0.5 if daily_pnls else 1
                sharpe = (avg_pnl / std_pnl * (252 ** 0.5)) if std_pnl > 0 else 0

                sr = StrategyResult(
                    strategy=strat_name,
                    city=city.name,
                    days_tested=len(day_results),
                    total_trades=sum(len(r.trades) for r in day_results),
                    total_wins=sum(r.wins for r in day_results),
                    total_losses=sum(r.losses for r in day_results),
                    gross_pnl=round(sum(r.gross_pnl for r in day_results), 2),
                    net_pnl=round(sum(r.net_pnl for r in day_results), 2),
                    total_fees=round(sum(r.fees for r in day_results), 2),
                    total_risked=round(sum(r.risked for r in day_results), 2),
                    max_daily_loss=round(min(daily_pnls), 2),
                    max_daily_profit=round(max(daily_pnls), 2),
                    max_drawdown=round(max_dd, 2),
                    sharpe_ratio=round(sharpe, 2),
                )
                all_results[strat_name].append(sr)

    return all_results


# ── Report ──────────────────────────────────────────────────────────

def print_report(results: dict[str, list[StrategyResult]]) -> None:
    print("\n" + "=" * 90)
    print("  MONTE CARLO BACKTEST — STRATEGY COMPARISON")
    print("  Using 730-day empirical forecast error distributions")
    print("=" * 90)

    # Aggregate per strategy
    summary = {}
    for strat, city_results in sorted(results.items()):
        if not city_results:
            continue
        total_trades = sum(r.total_trades for r in city_results)
        total_wins = sum(r.total_wins for r in city_results)
        total_losses = sum(r.total_losses for r in city_results)
        gross = sum(r.gross_pnl for r in city_results)
        net = sum(r.net_pnl for r in city_results)
        fees = sum(r.total_fees for r in city_results)
        risked = sum(r.total_risked for r in city_results)
        days = sum(r.days_tested for r in city_results)
        max_dd = max(r.max_drawdown for r in city_results)
        sharpes = [r.sharpe_ratio for r in city_results if r.sharpe_ratio != 0]
        avg_sharpe = sum(sharpes) / len(sharpes) if sharpes else 0

        summary[strat] = {
            "trades": total_trades,
            "wins": total_wins,
            "losses": total_losses,
            "win_rate": total_wins / total_trades * 100 if total_trades else 0,
            "gross": gross,
            "net": net,
            "fees": fees,
            "risked": risked,
            "roi": net / risked * 100 if risked else 0,
            "days": days,
            "max_dd": max_dd,
            "sharpe": avg_sharpe,
            "cities": len(city_results),
        }

    # Side-by-side comparison
    print(f"\n{'Metric':<22} {'A':>12} {'B':>12} {'C':>12} {'D':>12}")
    print("-" * 70)

    metrics = [
        ("Total trades", "trades", "d"),
        ("Win rate", "win_rate", ".1f", "%"),
        ("Gross P&L", "gross", "+.2f", "$"),
        ("Fees paid", "fees", ".2f", "$"),
        ("Net P&L", "net", "+.2f", "$"),
        ("Total risked", "risked", ".0f", "$"),
        ("Net ROI", "roi", "+.2f", "%"),
        ("Max drawdown", "max_dd", ".2f", "$"),
        ("Avg Sharpe", "sharpe", ".2f", ""),
        ("Days w/ trades", "days", "d"),
    ]

    for m in metrics:
        label, key, fmt = m[0], m[1], m[2]
        suffix = m[3] if len(m) > 3 else ""
        prefix = "$" if suffix == "$" else ""
        suffix_str = "%" if suffix == "%" else ""

        vals = []
        for s in ["A", "B", "C", "D"]:
            v = summary.get(s, {}).get(key, 0)
            if prefix == "$":
                vals.append(f"${v:{fmt}}")
            elif suffix_str == "%":
                vals.append(f"{v:{fmt}}%")
            else:
                vals.append(f"{v:{fmt}}")

        print(f"  {label:<20} {vals[0]:>12} {vals[1]:>12} {vals[2]:>12} {vals[3]:>12}")

    if not summary:
        print("\n  No trades generated. Check strategy thresholds vs market pricing.")
        print("=" * 90 + "\n")
        return

    # Per-city breakdown for best strategy
    best_strat = max(summary.keys(), key=lambda s: summary[s]["net"])
    print(f"\n{'─' * 70}")
    print(f"  Best strategy: {best_strat} (Net P&L: ${summary[best_strat]['net']:+.2f})")
    print(f"{'─' * 70}")
    print(f"\n  {'City':<18} {'Trades':>7} {'Win%':>7} {'Net P&L':>10} {'ROI':>8} {'Sharpe':>8}")
    print(f"  {'-'*60}")
    for r in sorted(results[best_strat], key=lambda x: x.net_pnl, reverse=True):
        wr = r.total_wins / r.total_trades * 100 if r.total_trades else 0
        roi = r.net_pnl / r.total_risked * 100 if r.total_risked else 0
        print(f"  {r.city:<18} {r.total_trades:>7} {wr:>6.1f}% ${r.net_pnl:>+8.2f} {roi:>+7.2f}% {r.sharpe_ratio:>7.2f}")

    # Verdict
    print(f"\n{'=' * 90}")
    profitable = [s for s, d in summary.items() if d["net"] > 0]
    if profitable:
        print(f"  PROFITABLE strategies: {', '.join(profitable)}")
    else:
        print("  WARNING: No strategy is profitable after fees")
    print(f"{'=' * 90}\n")


def save_results(results: dict[str, list[StrategyResult]], path: Path) -> None:
    """Save results to JSON."""
    data = {}
    for strat, city_results in results.items():
        data[strat] = [
            {
                "city": r.city, "days": r.days_tested,
                "trades": r.total_trades, "wins": r.total_wins,
                "losses": r.total_losses,
                "gross_pnl": r.gross_pnl, "net_pnl": r.net_pnl,
                "fees": r.total_fees, "risked": r.total_risked,
                "max_drawdown": r.max_drawdown, "sharpe": r.sharpe_ratio,
            }
            for r in city_results
        ]
    path.write_text(json.dumps(data, indent=2))
    print(f"Raw results saved to {path}")


# ── CLI ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Monte Carlo backtest with local error distributions")
    parser.add_argument("--days", type=int, default=730, help="Simulated trading days per city (default: 730)")
    parser.add_argument("--runs", type=int, default=3, help="Monte Carlo runs per city (default: 3)")
    parser.add_argument("--cities", type=str, default="", help="Comma-separated city names (default: all with data)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    args = parser.parse_args()

    config = load_config()

    if args.cities:
        city_names = [c.strip() for c in args.cities.split(",")]
        cities = [c for c in config.cities if c.name in city_names]
    else:
        # Use all cities that have error distribution files
        cities = [c for c in config.cities
                  if (ROOT / "data" / "history" / f"{c.icao}_errors.json").exists()]

    print(f"Monte Carlo Backtest: {len(cities)} cities, {args.days} days/city, {args.runs} runs")
    print(f"Strategies: A (conservative), B (locked aggressor), C (close range), D (quick exit)")
    print(f"Seed: {args.seed}")

    results = run_backtest(cities, n_days=args.days, n_runs=args.runs, seed=args.seed)
    print_report(results)
    save_results(results, ROOT / "data" / "mc_backtest_results.json")


if __name__ == "__main__":
    main()
