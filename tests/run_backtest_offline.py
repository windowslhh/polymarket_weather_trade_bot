"""Offline backtest: 30 cities, realistic profiles, Polymarket fees included.

Run: python -m tests.run_backtest_offline
"""
from __future__ import annotations

import math
import random
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from src.backtest.engine import _run_day, BacktestResult, print_backtest_report
from src.config import StrategyConfig
from src.weather.historical import ForecastErrorDistribution

# 30 US cities with realistic climate profiles
# (city, base_high_f, seasonal_amplitude_f, forecast_bias_f, forecast_std_f)
# base_high: annual average daily high
# seasonal_amplitude: summer-winter swing / 2
# forecast_bias: positive = forecast runs warm
# forecast_std: typical day-ahead forecast error std dev
CITY_PROFILES = [
    # Northeast — large seasonal swings, moderate forecast accuracy
    ("New York",       60, 25, 0.5,  3.5),
    ("Boston",         57, 27, 0.4,  3.8),
    ("Pittsburgh",     57, 25, 0.3,  3.6),
    ("Cleveland",      56, 26, 0.6,  4.0),
    # Southeast — warm, less seasonal, more stable forecasts
    ("Miami",          83, 8,  0.2,  2.0),
    ("Atlanta",        70, 18, 0.3,  3.0),
    ("Orlando",        82, 10, 0.1,  2.2),
    ("Tampa",          82, 10, 0.2,  2.1),
    ("Charlotte",      67, 20, 0.4,  3.2),
    ("Nashville",      67, 22, 0.5,  3.4),
    # Midwest — extreme seasons, volatile weather, hardest to forecast
    ("Chicago",        55, 30, 0.8,  4.5),
    ("Detroit",        55, 28, 0.7,  4.2),
    ("Minneapolis",    50, 35, 0.9,  5.0),
    ("Indianapolis",   58, 26, 0.6,  3.8),
    ("St. Louis",      62, 25, 0.5,  3.7),
    ("Kansas City",    62, 26, 0.4,  3.9),
    ("Cincinnati",     58, 25, 0.5,  3.6),
    ("Louisville",     62, 24, 0.4,  3.5),
    ("Memphis",        68, 21, 0.3,  3.2),
    # Southwest — hot, dry, predictable
    ("Phoenix",        87, 20, 0.0,  2.5),
    ("Las Vegas",      80, 25, 0.1,  2.8),
    ("San Antonio",    78, 18, 0.2,  2.6),
    ("Dallas",         75, 22, -0.3, 3.0),
    ("Houston",        78, 16, 0.0,  2.8),
    # West Coast — mild, marine influence, moderate forecast accuracy
    ("Los Angeles",    75, 10, 0.3,  2.5),
    ("San Francisco",  64, 8,  0.5,  3.0),
    ("Seattle",        55, 15, 1.0,  4.0),
    ("Portland",       58, 18, 0.8,  3.8),
    # Mountain — altitude effects, variable
    ("Denver",         62, 25, 0.2,  4.5),
    ("Salt Lake City", 58, 28, 0.3,  4.0),
]

assert len(CITY_PROFILES) == 30


def generate_realistic_data(
    city: str,
    base_high_f: float,
    seasonal_amplitude_f: float,
    forecast_bias_f: float,
    forecast_std_f: float,
    num_days: int = 365,
) -> tuple[list[tuple[date, float, float]], ForecastErrorDistribution]:
    """Generate realistic forecast/actual pairs with fat tails."""
    random.seed(hash(city) + 42)
    start = date.today() - timedelta(days=num_days)

    pairs = []
    errors = []

    for i in range(num_days):
        day = start + timedelta(days=i)
        day_of_year = day.timetuple().tm_yday
        seasonal = base_high_f + seasonal_amplitude_f * math.sin(
            2 * math.pi * (day_of_year - 80) / 365
        )

        # Weather noise + fat tails (5% chance of extreme event)
        weather_noise = random.gauss(0, 3.0)
        if random.random() < 0.05:
            weather_noise += random.choice([-1, 1]) * random.uniform(8, 15)
        actual = seasonal + weather_noise

        forecast_error = random.gauss(forecast_bias_f, forecast_std_f)
        forecast = actual + forecast_error

        pairs.append((day, round(forecast, 1), round(actual, 1)))
        errors.append(round(forecast_error, 1))

    dist = ForecastErrorDistribution(city, errors)
    return pairs, dist


def run_single_strategy(
    label: str,
    config: StrategyConfig,
    profiles: list[tuple],
    num_days: int = 365,
) -> list[BacktestResult]:
    """Run backtest for one strategy across all cities."""
    results: list[BacktestResult] = []

    for city, base_high, seasonal_amp, bias, std in profiles:
        pairs, error_dist = generate_realistic_data(
            city, base_high, seasonal_amp, bias, std, num_days=num_days,
        )

        day_results = []
        for day, forecast_high, actual_high in pairs:
            result = _run_day(city, day, forecast_high, actual_high, config, error_dist)
            if result.slots_traded > 0:
                day_results.append(result)

        if not day_results:
            results.append(BacktestResult(
                city=city, days_tested=0, total_trades=0,
                total_wins=0, total_losses=0, win_rate=0,
                gross_pnl=0, total_risked=0, roi_pct=0,
                avg_daily_pnl=0, max_daily_loss=0, max_daily_profit=0,
                error_dist_summary=error_dist.summary(),
            ))
            continue

        total_trades = sum(r.slots_traded for r in day_results)
        total_wins = sum(r.no_wins for r in day_results)
        total_losses = sum(r.no_losses for r in day_results)
        gross_pnl = sum(r.gross_pnl for r in day_results)
        total_risked = sum(r.total_risked for r in day_results)
        total_fees = sum(r.fees_paid for r in day_results)
        net_pnl = sum(r.net_pnl for r in day_results)
        daily_net = [r.net_pnl for r in day_results]

        results.append(BacktestResult(
            city=city,
            days_tested=len(day_results),
            total_trades=total_trades,
            total_wins=total_wins,
            total_losses=total_losses,
            win_rate=round(total_wins / total_trades, 4) if total_trades > 0 else 0,
            gross_pnl=round(gross_pnl, 2),
            total_risked=round(total_risked, 2),
            roi_pct=round(gross_pnl / total_risked * 100, 2) if total_risked > 0 else 0,
            avg_daily_pnl=round(net_pnl / len(day_results), 2),
            max_daily_loss=round(min(daily_net), 2),
            max_daily_profit=round(max(daily_net), 2),
            total_fees=round(total_fees, 2),
            net_pnl=round(net_pnl, 2),
            net_roi_pct=round(net_pnl / total_risked * 100, 2) if total_risked > 0 else 0,
            error_dist_summary=error_dist.summary(),
        ))

    return results


def run_offline_backtest():
    """Full 30-city backtest with Polymarket fees."""
    strategy_configs = [
        ("Conservative (threshold=10, minEV=0.03)",
         StrategyConfig(no_distance_threshold_f=10, min_no_ev=0.03, max_position_per_slot_usd=5.0)),
        ("Moderate (threshold=8, minEV=0.02)",
         StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.02, max_position_per_slot_usd=5.0)),
        ("Aggressive (threshold=6, minEV=0.01)",
         StrategyConfig(no_distance_threshold_f=6, min_no_ev=0.01, max_position_per_slot_usd=5.0)),
    ]

    for config_label, config in strategy_configs:
        print(f"\n{'#' * 80}")
        print(f"# STRATEGY: {config_label}")
        print(f"# Fee model: Polymarket weather taker 1.25%, prob-weighted")
        print(f"# Cities: {len(CITY_PROFILES)}")
        print(f"{'#' * 80}")

        results = run_single_strategy(config_label, config, CITY_PROFILES)
        print_backtest_report(results)

        # Blowup analysis — group by region
        print("BLOWUP ANALYSIS BY REGION:")
        regions = {
            "Northeast": ["New York", "Boston", "Pittsburgh", "Cleveland"],
            "Southeast": ["Miami", "Atlanta", "Orlando", "Tampa", "Charlotte", "Nashville"],
            "Midwest": ["Chicago", "Detroit", "Minneapolis", "Indianapolis", "St. Louis",
                       "Kansas City", "Cincinnati", "Louisville", "Memphis"],
            "Southwest": ["Phoenix", "Las Vegas", "San Antonio", "Dallas", "Houston"],
            "West Coast": ["Los Angeles", "San Francisco", "Seattle", "Portland"],
            "Mountain": ["Denver", "Salt Lake City"],
        }

        for region, region_cities in regions.items():
            region_results = [r for r in results if r.city in region_cities]
            total_losses = sum(r.total_losses for r in region_results)
            total_trades = sum(r.total_trades for r in region_results)
            net = sum(r.net_pnl for r in region_results)
            risked = sum(r.total_risked for r in region_results)
            roi = net / risked * 100 if risked > 0 else 0
            print(f"  {region:12s}: {len(region_cities)} cities, "
                  f"net ${net:+8.2f}, ROI {roi:+.2f}%, "
                  f"losses {total_losses}/{total_trades}")

        # Correlation risk: how many cities lose on the same day?
        print("\nWORST DAYS (multi-city simultaneous losses):")
        # Regenerate all data and find worst days
        all_day_results: dict[date, list[DayResult]] = {}
        for city, base_high, seasonal_amp, bias, std in CITY_PROFILES:
            pairs, error_dist = generate_realistic_data(city, base_high, seasonal_amp, bias, std)
            for day, forecast, actual in pairs:
                r = _run_day(city, day, forecast, actual, config, error_dist)
                if r.no_losses > 0:
                    all_day_results.setdefault(r.day, []).append(r)

        # Sort by number of cities losing on same day
        multi_loss_days = [(d, rs) for d, rs in all_day_results.items() if len(rs) >= 3]
        multi_loss_days.sort(key=lambda x: -len(x[1]))

        if multi_loss_days:
            for d, rs in multi_loss_days[:5]:
                cities_hit = [r.city for r in rs]
                total_loss = sum(r.net_pnl for r in rs)
                print(f"  {d}: {len(rs)} cities lost (${total_loss:+.2f}) — {', '.join(cities_hit)}")
        else:
            print("  No days with 3+ cities losing simultaneously")
        print()


if __name__ == "__main__":
    from src.backtest.engine import DayResult  # noqa: needed for type
    run_offline_backtest()
