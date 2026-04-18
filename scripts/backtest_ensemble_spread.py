"""Backtest: Ensemble spread vs forecast error correlation analysis.

This script fetches historical ensemble spread data from Open-Meteo and
correlates it with the existing forecast error data to determine whether
ensemble spread is a useful predictor of forecast accuracy.

Usage:
    .venv/bin/python scripts/backtest_ensemble_spread.py
"""
from __future__ import annotations

import asyncio
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.weather.historical import fetch_historical_actuals, fetch_with_retry

# ── Config ────────────────────────────────────────────────────────────────────
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_ARCHIVE_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"

ENSEMBLE_MODELS = [
    "gfs_seamless", "ecmwf_ifs025",
    "gem_global", "icon_seamless",
]

# Test cities: mix of accurate and uncertain
TEST_CITIES = [
    {"name": "Denver",        "icao": "KBKF", "lat": 39.7017, "lon": -104.7517},
    {"name": "Miami",         "icao": "KMIA", "lat": 25.7617, "lon": -80.1918},
    {"name": "Chicago",       "icao": "KORD", "lat": 41.8781, "lon": -87.6298},
    {"name": "Los Angeles",   "icao": "KLAX", "lat": 34.0522, "lon": -118.2437},
    {"name": "Seattle",       "icao": "KSEA", "lat": 47.6062, "lon": -122.3321},
    {"name": "Dallas",        "icao": "KDAL", "lat": 32.8471, "lon": -96.8518},
    {"name": "San Francisco", "icao": "KSFO", "lat": 37.7749, "lon": -122.4194},
    {"name": "Atlanta",       "icao": "KATL", "lat": 33.7490, "lon": -84.3880},
    {"name": "Houston",       "icao": "KHOU", "lat": 29.6454, "lon": -95.2789},
]

# Open-Meteo ensemble API supports ~3 months of past runs
LOOKBACK_DAYS = 92


HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# Individual models to fetch from the historical forecast archive
INDIVIDUAL_MODELS = ["gfs_seamless", "ecmwf_ifs025", "icon_seamless"]


async def fetch_ensemble_history(
    city: dict, start: date, end: date, client: httpx.AsyncClient
) -> list[tuple[date, float, float]]:
    """Fetch historical inter-model spread for a city.

    Uses Open-Meteo historical-forecast-api to get archived per-model forecasts.
    Computes the standard deviation between models as the "spread" proxy.

    Returns list of (date, model_mean, model_std) tuples.
    """
    # Fetch each model separately from the historical forecast archive
    model_data: dict[str, dict[date, float]] = {}

    for model in INDIVIDUAL_MODELS:
        # API limits to ~3 months per request
        chunk_size = 90
        current = start
        model_data[model] = {}

        while current <= end:
            chunk_end = min(current + timedelta(days=chunk_size - 1), end)
            params = {
                "latitude": city["lat"],
                "longitude": city["lon"],
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
                "start_date": current.isoformat(),
                "end_date": chunk_end.isoformat(),
            }
            url = f"{HISTORICAL_FORECAST_URL}"

            try:
                # historical-forecast-api uses model names in the URL or params
                data = await fetch_with_retry(client, url, {**params, "models": model})
                daily = data.get("daily", {})
                times = daily.get("time", [])
                # Key format: temperature_2m_max or temperature_2m_max_<model>
                highs = None
                for key, values in daily.items():
                    if key.startswith("temperature_2m_max") and isinstance(values, list):
                        highs = values
                        break

                if highs:
                    for t, h in zip(times, highs):
                        if h is not None:
                            model_data[model][date.fromisoformat(t)] = float(h)
            except Exception as e:
                print(f"    Warning: {model} {current}..{chunk_end}: {e}")

            current = chunk_end + timedelta(days=1)
            await asyncio.sleep(0.25)

    # Compute inter-model spread for each date
    results = []
    all_dates = set()
    for m in model_data.values():
        all_dates |= m.keys()

    for d in sorted(all_dates):
        vals = [model_data[m][d] for m in INDIVIDUAL_MODELS if d in model_data[m]]
        if len(vals) >= 2:
            mean = sum(vals) / len(vals)
            std = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
            results.append((d, mean, max(std, 0.5)))

    return results


async def fetch_forecasts_and_actuals(
    city: dict, start: date, end: date, client: httpx.AsyncClient
) -> tuple[dict[date, float], dict[date, float]]:
    """Fetch historical forecasts and actuals for a city."""
    # Actuals
    actual_data = await fetch_with_retry(client, ARCHIVE_URL, {
        "latitude": city["lat"],
        "longitude": city["lon"],
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    })
    actual_daily = actual_data.get("daily", {})
    actual_map = {}
    for t, h in zip(actual_daily.get("time", []), actual_daily.get("temperature_2m_max", [])):
        if h is not None:
            actual_map[date.fromisoformat(t)] = float(h)

    await asyncio.sleep(0.2)

    # Forecasts (previous runs)
    forecast_data = await fetch_with_retry(client, FORECAST_ARCHIVE_URL, {
        "latitude": city["lat"],
        "longitude": city["lon"],
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "past_days": 0,
    })
    forecast_daily = forecast_data.get("daily", {})
    forecast_map = {}
    for t, h in zip(forecast_daily.get("time", []), forecast_daily.get("temperature_2m_max", [])):
        if h is not None:
            forecast_map[date.fromisoformat(t)] = float(h)

    return forecast_map, actual_map


def pearson_r(xs: list[float], ys: list[float]) -> float:
    """Compute Pearson correlation coefficient."""
    n = len(xs)
    if n < 3:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / n)
    sy = math.sqrt(sum((y - my) ** 2 for y in ys) / n)
    if sx == 0 or sy == 0:
        return float("nan")
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    return cov / (sx * sy)


def spearman_r(xs: list[float], ys: list[float]) -> float:
    """Compute Spearman rank correlation coefficient."""
    n = len(xs)
    if n < 3:
        return float("nan")

    def rank(vals: list[float]) -> list[float]:
        sorted_idx = sorted(range(n), key=lambda i: vals[i])
        ranks = [0.0] * n
        for r, i in enumerate(sorted_idx):
            ranks[i] = float(r + 1)
        return ranks

    rx = rank(xs)
    ry = rank(ys)
    return pearson_r(rx, ry)


async def analyze_city(city: dict, client: httpx.AsyncClient) -> dict | None:
    """Run full analysis for one city."""
    end = date.today() - timedelta(days=2)  # latest complete data
    start = end - timedelta(days=LOOKBACK_DAYS)

    print(f"\n{'='*60}")
    print(f"  {city['name']} ({city['icao']})")
    print(f"  Period: {start} to {end} ({LOOKBACK_DAYS} days)")
    print(f"{'='*60}")

    # 1. Fetch ensemble spread
    print(f"  Fetching ensemble spread...")
    ensemble_data = await fetch_ensemble_history(city, start, end, client)
    if not ensemble_data:
        print(f"  ERROR: No ensemble data available")
        return None
    print(f"  Got {len(ensemble_data)} days of ensemble data")

    ensemble_map = {d: (mean, std) for d, mean, std in ensemble_data}

    # 2. Fetch forecasts and actuals
    print(f"  Fetching forecasts and actuals...")
    forecast_map, actual_map = await fetch_forecasts_and_actuals(
        city, start, end, client
    )
    print(f"  Forecasts: {len(forecast_map)} days, Actuals: {len(actual_map)} days")

    # 3. Align data — need all three (ensemble, forecast, actual) for same date
    spread_list: list[float] = []
    abs_error_list: list[float] = []
    error_list: list[float] = []
    aligned_days = 0

    for d in sorted(ensemble_map.keys()):
        if d in forecast_map and d in actual_map:
            _, spread = ensemble_map[d]
            error = forecast_map[d] - actual_map[d]
            spread_list.append(spread)
            abs_error_list.append(abs(error))
            error_list.append(error)
            aligned_days += 1

    if aligned_days < 10:
        print(f"  ERROR: Only {aligned_days} aligned days — insufficient")
        return None

    print(f"  Aligned days: {aligned_days}")

    # 4. Correlation analysis
    r_pearson = pearson_r(spread_list, abs_error_list)
    r_spearman = spearman_r(spread_list, abs_error_list)

    print(f"\n  --- Correlation: ensemble_spread vs |forecast_error| ---")
    print(f"  Pearson  r = {r_pearson:+.4f}")
    print(f"  Spearman ρ = {r_spearman:+.4f}")

    # 5. Group analysis: split by spread terciles
    sorted_pairs = sorted(zip(spread_list, abs_error_list), key=lambda x: x[0])
    n = len(sorted_pairs)
    t1 = n // 3
    t2 = 2 * n // 3

    groups = {
        "LOW":  sorted_pairs[:t1],
        "MID":  sorted_pairs[t1:t2],
        "HIGH": sorted_pairs[t2:],
    }

    print(f"\n  --- Grouped by ensemble spread terciles ---")
    print(f"  {'Group':6s} | {'N':>4s} | {'Spread range':>16s} | {'Mean |error|':>12s} | {'Median |error|':>14s} | {'P(|err|>5°F)':>12s}")
    print(f"  {'-'*6}-+-{'-'*4}-+-{'-'*16}-+-{'-'*12}-+-{'-'*14}-+-{'-'*12}")

    group_stats = {}
    for label, pairs in groups.items():
        if not pairs:
            continue
        spreads = [p[0] for p in pairs]
        errors = [p[1] for p in pairs]
        mean_err = sum(errors) / len(errors)
        sorted_errors = sorted(errors)
        median_err = sorted_errors[len(sorted_errors) // 2]
        p_large = sum(1 for e in errors if e > 5.0) / len(errors)
        spread_lo = min(spreads)
        spread_hi = max(spreads)
        print(f"  {label:6s} | {len(pairs):4d} | {spread_lo:6.1f} - {spread_hi:5.1f}°F | {mean_err:10.2f}°F | {median_err:12.2f}°F | {p_large:10.1%}")
        group_stats[label] = {
            "n": len(pairs),
            "spread_range": [round(spread_lo, 2), round(spread_hi, 2)],
            "mean_abs_error": round(mean_err, 2),
            "median_abs_error": round(median_err, 2),
            "p_large_error": round(p_large, 3),
        }

    # 6. Dynamic threshold simulation
    # Current: fixed threshold = k × historical_std
    # Proposed: threshold = k × historical_std × (spread / avg_spread)
    avg_spread = sum(spread_list) / len(spread_list)
    hist_std = math.sqrt(sum((e - sum(error_list)/len(error_list)) ** 2 for e in error_list) / len(error_list))

    # Simulate how many signals would pass/fail under each scheme
    K_UNCERTAIN = 2.0  # from calibrator.py
    fixed_threshold = K_UNCERTAIN * hist_std
    min_threshold = 3.0
    max_threshold = 15.0

    fixed_t = max(min_threshold, min(max_threshold, fixed_threshold))

    correct_fixed = 0
    correct_dynamic = 0
    signals_fixed = 0
    signals_dynamic = 0
    false_close_fixed = 0  # signal fired but actual was close (error < distance)
    false_close_dynamic = 0

    # Simulate: for each day, a "slot" at forecast+threshold (worst case NO signal)
    for spread, abs_err in zip(spread_list, abs_error_list):
        dynamic_t = max(min_threshold, min(max_threshold,
                        fixed_threshold * (spread / avg_spread)))

        # A signal fires if distance > threshold → we'd expect |error| < distance
        # "Correct" = the actual distance was indeed > threshold (signal was right)
        # "False close" = actual error > threshold (we thought it was safe but it wasn't)

        # With fixed threshold
        signals_fixed += 1
        if abs_err < fixed_t:
            correct_fixed += 1
        else:
            false_close_fixed += 1

        # With dynamic threshold
        signals_dynamic += 1
        if abs_err < dynamic_t:
            correct_dynamic += 1
        else:
            false_close_dynamic += 1

    # More useful: count how often signals would be generated at each threshold
    # For a slot at distance D from forecast, signal fires when D > threshold
    # We want: P(actual lands in slot | distance D) to be low
    # The key metric: at threshold T, what fraction of days had |error| > T?
    false_alarm_fixed = sum(1 for e in abs_error_list if e > fixed_t) / len(abs_error_list)
    false_alarm_dynamic_list = []
    for spread, abs_err in zip(spread_list, abs_error_list):
        dynamic_t = max(min_threshold, min(max_threshold,
                        fixed_threshold * (spread / avg_spread)))
        false_alarm_dynamic_list.append(1 if abs_err > dynamic_t else 0)
    false_alarm_dynamic = sum(false_alarm_dynamic_list) / len(false_alarm_dynamic_list)

    # Count days where dynamic would have lowered threshold (more signals)
    lowered = sum(1 for s in spread_list if s < avg_spread)
    raised = sum(1 for s in spread_list if s > avg_spread)

    print(f"\n  --- Dynamic threshold simulation ---")
    print(f"  Historical σ: {hist_std:.2f}°F")
    print(f"  Average ensemble spread: {avg_spread:.2f}°F")
    print(f"  Fixed threshold (k={K_UNCERTAIN}): {fixed_t:.1f}°F")
    print(f"  Days spread < avg (threshold lowered → more signals): {lowered} ({lowered/n:.0%})")
    print(f"  Days spread > avg (threshold raised → fewer signals): {raised} ({raised/n:.0%})")
    print(f"  P(|error| > fixed threshold):   {false_alarm_fixed:.1%}")
    print(f"  P(|error| > dynamic threshold): {false_alarm_dynamic:.1%}")

    return {
        "city": city["name"],
        "aligned_days": aligned_days,
        "avg_spread": round(avg_spread, 2),
        "hist_std": round(hist_std, 2),
        "pearson_r": round(r_pearson, 4),
        "spearman_r": round(r_spearman, 4),
        "group_stats": group_stats,
        "fixed_threshold": round(fixed_t, 1),
        "false_alarm_fixed": round(false_alarm_fixed, 3),
        "false_alarm_dynamic": round(false_alarm_dynamic, 3),
    }


async def main():
    print("=" * 60)
    print("  BACKTEST: Ensemble Spread vs Forecast Error Correlation")
    print(f"  Period: last {LOOKBACK_DAYS} days")
    print(f"  Cities: {len(TEST_CITIES)}")
    print("=" * 60)

    all_results = []
    async with httpx.AsyncClient(timeout=60) as client:
        for city in TEST_CITIES:
            try:
                result = await analyze_city(city, client)
                if result:
                    all_results.append(result)
            except Exception as e:
                print(f"\n  FAILED {city['name']}: {e}")
            await asyncio.sleep(0.5)  # rate limit between cities

    if not all_results:
        print("\nNo results to summarize.")
        return

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n\n{'='*72}")
    print(f"  SUMMARY ACROSS ALL CITIES")
    print(f"{'='*72}")

    print(f"\n  {'City':16s} | {'Days':>5s} | {'Avg Spread':>10s} | {'Hist σ':>7s} | {'Pearson r':>10s} | {'Spearman ρ':>10s} | {'Fix FA':>7s} | {'Dyn FA':>7s}")
    print(f"  {'-'*16}-+-{'-'*5}-+-{'-'*10}-+-{'-'*7}-+-{'-'*10}-+-{'-'*10}-+-{'-'*7}-+-{'-'*7}")

    all_pearson = []
    all_spearman = []
    for r in all_results:
        p = r["pearson_r"]
        s = r["spearman_r"]
        if not math.isnan(p):
            all_pearson.append(p)
        if not math.isnan(s):
            all_spearman.append(s)
        print(f"  {r['city']:16s} | {r['aligned_days']:5d} | {r['avg_spread']:8.2f}°F | {r['hist_std']:5.2f}°F | {p:+10.4f} | {s:+10.4f} | {r['false_alarm_fixed']:5.1%} | {r['false_alarm_dynamic']:5.1%}")

    if all_pearson:
        avg_p = sum(all_pearson) / len(all_pearson)
        avg_s = sum(all_spearman) / len(all_spearman)
        print(f"\n  Average Pearson r:  {avg_p:+.4f}")
        print(f"  Average Spearman ρ: {avg_s:+.4f}")

    # Group analysis across all cities
    print(f"\n  --- Cross-city group analysis ---")
    for group_label in ["LOW", "MID", "HIGH"]:
        errs = []
        for r in all_results:
            gs = r.get("group_stats", {}).get(group_label)
            if gs:
                errs.append(gs["mean_abs_error"])
        if errs:
            avg_err = sum(errs) / len(errs)
            print(f"  {group_label} spread group → avg |error| across cities: {avg_err:.2f}°F")

    # Conclusion
    print(f"\n  --- CONCLUSION ---")
    if all_pearson:
        avg_p = sum(all_pearson) / len(all_pearson)
        if avg_p > 0.3:
            print(f"  STRONG positive correlation (avg r={avg_p:.3f}).")
            print(f"  Recommendation: Worth implementing dynamic threshold adjustment.")
        elif avg_p > 0.15:
            print(f"  MODERATE positive correlation (avg r={avg_p:.3f}).")
            print(f"  Recommendation: Cautiously worth trying, but effect may be small.")
        elif avg_p > 0.05:
            print(f"  WEAK positive correlation (avg r={avg_p:.3f}).")
            print(f"  Recommendation: Marginal value. Consider cost/complexity before implementing.")
        else:
            print(f"  NO meaningful correlation (avg r={avg_p:.3f}).")
            print(f"  Recommendation: Do NOT implement — ensemble spread does not predict error magnitude.")

    # Save results
    output_path = ROOT / "data" / "backtest_ensemble_spread.json"
    output_path.write_text(json.dumps(all_results, indent=2))
    print(f"\n  Results saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
