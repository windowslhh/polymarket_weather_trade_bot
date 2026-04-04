"""Offline backtest with synthetic but realistic historical data.

Generates 2 years of simulated forecast/actual temperature pairs using
realistic statistical properties, then runs the full backtest to validate
whether the strategy EV is genuinely positive.

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


def generate_realistic_data(
    city: str,
    base_high_f: float,
    seasonal_amplitude_f: float,
    forecast_bias_f: float,
    forecast_std_f: float,
    num_days: int = 730,
) -> tuple[list[tuple[date, float, float]], ForecastErrorDistribution]:
    """Generate realistic forecast/actual pairs.

    Models:
    - Seasonal cycle (sine wave over 365 days)
    - Day-to-day weather noise
    - Forecast bias (systematic over/under prediction)
    - Occasional extreme deviations (fat tails)

    Returns (list of (date, forecast_high, actual_high), error_distribution).
    """
    random.seed(hash(city) + 42)
    start = date.today() - timedelta(days=num_days)

    pairs = []
    errors = []

    for i in range(num_days):
        day = start + timedelta(days=i)
        # Seasonal component
        day_of_year = day.timetuple().tm_yday
        seasonal = base_high_f + seasonal_amplitude_f * math.sin(
            2 * math.pi * (day_of_year - 80) / 365  # peak around day 172 (June 21)
        )

        # Actual temperature: seasonal + random weather noise
        weather_noise = random.gauss(0, 3.0)
        # Fat tail: ~5% chance of extreme deviation
        if random.random() < 0.05:
            weather_noise += random.choice([-1, 1]) * random.uniform(8, 15)
        actual = seasonal + weather_noise

        # Forecast: actual + bias + forecast error
        forecast_error = random.gauss(forecast_bias_f, forecast_std_f)
        forecast = actual + forecast_error

        pairs.append((day, round(forecast, 1), round(actual, 1)))
        errors.append(round(forecast_error, 1))

    dist = ForecastErrorDistribution(city, errors)
    return pairs, dist


def run_offline_backtest():
    """Run backtest with synthetic data for multiple city profiles."""
    cities_profiles = [
        # (city, base_high, seasonal_amp, forecast_bias, forecast_std)
        ("New York",      60.0, 25.0, 0.5,  3.5),   # Slight warm bias
        ("Dallas",        75.0, 20.0, -0.3, 3.0),    # Slight cool bias
        ("Seattle",       55.0, 15.0, 1.0,  4.0),    # Warm bias, less certain
        ("Phoenix",       85.0, 20.0, 0.0,  2.5),    # No bias, more precise
        ("Chicago",       55.0, 30.0, 0.8,  4.5),    # Warm bias, volatile
    ]

    strategy_configs = [
        ("Conservative (threshold=10°F, minEV=0.03)",
         StrategyConfig(no_distance_threshold_f=10, min_no_ev=0.03, max_position_per_slot_usd=5.0)),
        ("Moderate (threshold=8°F, minEV=0.02)",
         StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.02, max_position_per_slot_usd=5.0)),
        ("Aggressive (threshold=6°F, minEV=0.01)",
         StrategyConfig(no_distance_threshold_f=6, min_no_ev=0.01, max_position_per_slot_usd=5.0)),
    ]

    for config_label, config in strategy_configs:
        print(f"\n{'#' * 80}")
        print(f"# STRATEGY: {config_label}")
        print(f"{'#' * 80}")

        results: list[BacktestResult] = []

        for city, base_high, seasonal_amp, bias, std in cities_profiles:
            pairs, error_dist = generate_realistic_data(
                city, base_high, seasonal_amp, bias, std, num_days=365,
            )

            # Run day-by-day simulation
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
            daily_pnls = [r.gross_pnl for r in day_results]

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
                avg_daily_pnl=round(gross_pnl / len(day_results), 2),
                max_daily_loss=round(min(daily_pnls), 2),
                max_daily_profit=round(max(daily_pnls), 2),
                error_dist_summary=error_dist.summary(),
            ))

        print_backtest_report(results)

        # Additional analysis: blowup frequency
        print("BLOWUP ANALYSIS (days with any NO loss):")
        for city, base_high, seasonal_amp, bias, std in cities_profiles:
            pairs, error_dist = generate_realistic_data(
                city, base_high, seasonal_amp, bias, std, num_days=365,
            )
            loss_days = 0
            total_days = 0
            for day, forecast, actual in pairs:
                r = _run_day(city, day, forecast, actual, config, error_dist)
                if r.slots_traded > 0:
                    total_days += 1
                    if r.no_losses > 0:
                        loss_days += 1

            pct = loss_days / total_days * 100 if total_days > 0 else 0
            print(f"  {city}: {loss_days}/{total_days} days with losses ({pct:.1f}%)")
        print()


if __name__ == "__main__":
    run_offline_backtest()
