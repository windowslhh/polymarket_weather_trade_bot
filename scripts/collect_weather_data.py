#!/usr/bin/env python3
"""Weather model data foundation — collect forecast & actual temperatures daily.

Fetches per-model forecasts (NWS, GFS, ICON, ECMWF) and METAR actuals for
ALL configured cities.  Appends one JSONL record per city per day.

Usage:
    # Collect today's data (run daily via cron/scheduler)
    .venv/bin/python scripts/collect_weather_data.py

    # Backfill historical data (Open-Meteo archive, up to 2 years)
    .venv/bin/python scripts/collect_weather_data.py --backfill 30

    # Specific date
    .venv/bin/python scripts/collect_weather_data.py --date 2026-04-10

    # Generate model comparison snapshot
    .venv/bin/python scripts/collect_weather_data.py --compare

Output:
    data/weather-models/daily_forecasts.jsonl
    data/weather-models/icon_vs_ecmwf_YYYY-MM-DD.json  (with --compare)

JSONL schema:
{
    "date": "2026-04-11",
    "city": "Seattle",
    "icao": "KSEA",
    "lat": 47.45,
    "lon": -122.31,
    "collected_at": "2026-04-11T07:00:00+00:00",
    "forecasts": {
        "nws": {"high_f": 54.0, "low_f": 42.0, "source": "nws"},
        "gfs": {"high_f": 55.2},
        "icon": {"high_f": 53.8},
        "ecmwf": {"high_f": 54.5},
        "ensemble_mean": {"high_f": 54.5, "std_f": 0.7, "spread_f": 0.6},
        "combined": {"high_f": 54.3, "confidence_f": 0.7, "source": "nws+ensemble(3m,Nmem)"}
    },
    "actual": {
        "metar_temp_f": 52.3,
        "metar_time": "2026-04-11T09:53:00+00:00",
        "daily_max_f": 53.1,        // null if day not complete
        "daily_max_source": "metar"  // or "archive" for backfill
    },
    "errors": {
        "nws": -1.0,        // forecast - actual (null if actual unavailable)
        "gfs": 2.1,
        "icon": 0.7,
        "ecmwf": 1.4,
        "ensemble": 1.4,
        "combined": 1.2
    }
}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config, CityConfig
from src.weather.http_utils import fetch_with_retry
from src.weather.nws import get_nws_forecast
from src.weather.metar import get_latest_metar

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "weather-models"
JSONL_FILE = DATA_DIR / "daily_forecasts.jsonl"

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_ARCHIVE_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
SINGLE_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

ENSEMBLE_MODELS = [
    "gfs_seamless",
    "icon_seamless",
    "ecmwf_ifs025",
]

MODEL_NAMES = {
    "gfs_seamless": "gfs",
    "icon_seamless": "icon",
    "ecmwf_ifs025": "ecmwf",
}

# Open-Meteo returns different key suffixes than the model names we send.
# Map from response key suffix → our short model name.
RESPONSE_KEY_MAP = {
    "ncep_gefs_seamless": "gfs",
    "icon_seamless_eps": "icon",
    "ecmwf_ifs025_ensemble": "ecmwf",
}

# Rate limiting
SEMAPHORE = asyncio.Semaphore(5)
REQUEST_DELAY = 0.25  # seconds between requests


# ── Per-model forecast fetch ──────────────────────────────────────

async def fetch_per_model_forecasts(
    city: CityConfig,
    target: date,
    client: httpx.AsyncClient,
) -> dict:
    """Fetch forecast from each model SEPARATELY + ensemble mean.

    Returns dict like:
        {
            "gfs": {"high_f": 55.2},
            "icon": {"high_f": 53.8},
            "ecmwf": {"high_f": 54.5},
            "ensemble_mean": {"high_f": 54.5, "std_f": 0.7, "spread_f": 0.6},
        }
    """
    params = {
        "latitude": city.lat,
        "longitude": city.lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "start_date": target.isoformat(),
        "end_date": target.isoformat(),
        "models": ",".join(ENSEMBLE_MODELS),
    }

    result: dict = {}
    try:
        async with SEMAPHORE:
            data = await fetch_with_retry(client, ENSEMBLE_URL, params)
            await asyncio.sleep(REQUEST_DELAY)

        daily = data.get("daily", {})

        # Extract per-model ensemble means.
        # Open-Meteo returns keys like "temperature_2m_max_ncep_gefs_seamless"
        # (the ensemble mean for that model) plus "..._memberXX_..." (individual
        # ensemble members).  We want the ensemble mean key (no "member" in it).
        all_highs: list[float] = []
        model_highs: dict[str, float] = {}
        for key, values in daily.items():
            if not key.startswith("temperature_2m_max") or "member" in key or key == "time":
                continue
            if not isinstance(values, list) or not values or values[0] is None:
                continue
            # Match key suffix to model name
            suffix = key.replace("temperature_2m_max_", "")
            short_name = RESPONSE_KEY_MAP.get(suffix)
            if short_name:
                h = float(values[0])
                result[short_name] = {"high_f": round(h, 1)}
                model_highs[short_name] = h
                all_highs.append(h)

        # Ensemble statistics
        if all_highs:
            mean = sum(all_highs) / len(all_highs)
            std = (sum((h - mean) ** 2 for h in all_highs) / len(all_highs)) ** 0.5 if len(all_highs) > 1 else 0.0
            spread = max(all_highs) - min(all_highs)
            result["ensemble_mean"] = {
                "high_f": round(mean, 1),
                "std_f": round(std, 2),
                "spread_f": round(spread, 1),
                "n_models": len(all_highs),
            }

    except Exception as e:
        logger.warning("Ensemble fetch failed for %s: %s", city.name, e)

    return result


async def fetch_nws_forecast_safe(
    city: CityConfig,
    target: date,
    client: httpx.AsyncClient,
) -> dict | None:
    """Fetch NWS forecast, return dict or None."""
    try:
        async with SEMAPHORE:
            fc = await get_nws_forecast(city, target, client)
            await asyncio.sleep(REQUEST_DELAY)
        if fc:
            return {
                "high_f": fc.predicted_high_f,
                "low_f": fc.predicted_low_f,
                "source": "nws",
            }
    except Exception as e:
        logger.warning("NWS forecast failed for %s: %s", city.name, e)
    return None


async def fetch_metar_safe(
    city: CityConfig,
    client: httpx.AsyncClient,
) -> dict | None:
    """Fetch latest METAR observation."""
    try:
        async with SEMAPHORE:
            obs = await get_latest_metar(city.icao, client)
            await asyncio.sleep(REQUEST_DELAY)
        if obs:
            return {
                "metar_temp_f": round(obs.temp_f, 1),
                "metar_time": obs.observation_time.isoformat(),
                "daily_max_f": None,  # will be filled by archive or tracker
                "daily_max_source": "metar",
            }
    except Exception as e:
        logger.warning("METAR failed for %s: %s", city.icao, e)
    return None


# ── Archive fetch (for backfill) ──────────────────────────────────

async def fetch_archive_actual(
    city: CityConfig,
    target: date,
    client: httpx.AsyncClient,
) -> float | None:
    """Fetch verified daily max from Open-Meteo archive (for past dates)."""
    try:
        async with SEMAPHORE:
            data = await fetch_with_retry(client, ARCHIVE_URL, {
                "latitude": city.lat,
                "longitude": city.lon,
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
                "start_date": target.isoformat(),
                "end_date": target.isoformat(),
            })
            await asyncio.sleep(REQUEST_DELAY)
        daily = data.get("daily", {})
        highs = daily.get("temperature_2m_max", [])
        if highs and highs[0] is not None:
            return round(float(highs[0]), 1)
    except Exception as e:
        logger.warning("Archive actual failed for %s %s: %s", city.name, target, e)
    return None


async def fetch_archive_forecast(
    city: CityConfig,
    target: date,
    client: httpx.AsyncClient,
) -> float | None:
    """Fetch historical day-ahead forecast from Open-Meteo Previous Runs API."""
    try:
        async with SEMAPHORE:
            data = await fetch_with_retry(client, FORECAST_ARCHIVE_URL, {
                "latitude": city.lat,
                "longitude": city.lon,
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
                "start_date": target.isoformat(),
                "end_date": target.isoformat(),
                "past_days": 0,
            })
            await asyncio.sleep(REQUEST_DELAY)
        daily = data.get("daily", {})
        highs = daily.get("temperature_2m_max", [])
        if highs and highs[0] is not None:
            return round(float(highs[0]), 1)
    except Exception as e:
        logger.warning("Archive forecast failed for %s %s: %s", city.name, target, e)
    return None


# ── Main collection logic ─────────────────────────────────────────

async def collect_city_day(
    city: CityConfig,
    target: date,
    client: httpx.AsyncClient,
    is_backfill: bool = False,
) -> dict:
    """Collect all forecast + actual data for one city-day."""
    record: dict = {
        "date": target.isoformat(),
        "city": city.name,
        "icao": city.icao,
        "lat": city.lat,
        "lon": city.lon,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }

    today = date.today()
    is_today = target == today
    is_past = target < today
    is_future = target > today

    # ── Forecasts ──
    forecasts: dict = {}

    if is_today or is_future:
        # Live forecast: NWS + per-model ensemble
        nws_task = fetch_nws_forecast_safe(city, target, client)
        model_task = fetch_per_model_forecasts(city, target, client)
        nws_result, model_result = await asyncio.gather(nws_task, model_task)

        if nws_result:
            forecasts["nws"] = nws_result
        forecasts.update(model_result)

        # Combined (NWS + ensemble mean)
        nws_high = nws_result["high_f"] if nws_result else None
        ens_high = model_result.get("ensemble_mean", {}).get("high_f")
        if nws_high and ens_high:
            combined_high = round(nws_high * 0.5 + ens_high * 0.5, 1)
            conf = model_result.get("ensemble_mean", {}).get("std_f", 4.0)
            forecasts["combined"] = {
                "high_f": combined_high,
                "confidence_f": conf,
                "source": "nws+ensemble",
            }
        elif nws_high:
            forecasts["combined"] = {"high_f": nws_high, "confidence_f": 3.0, "source": "nws"}
        elif ens_high:
            forecasts["combined"] = {"high_f": ens_high, "confidence_f": conf if ens_high else 4.0, "source": "ensemble"}
    else:
        # Backfill: use Previous Runs API for what the forecast was
        fc_high = await fetch_archive_forecast(city, target, client)
        if fc_high is not None:
            forecasts["archive_forecast"] = {"high_f": fc_high, "source": "previous-runs"}

    record["forecasts"] = forecasts

    # ── Actuals ──
    actual: dict = {}
    if is_today:
        metar = await fetch_metar_safe(city, client)
        if metar:
            actual = metar
    elif is_past:
        archive_high = await fetch_archive_actual(city, target, client)
        if archive_high is not None:
            actual = {
                "metar_temp_f": None,
                "metar_time": None,
                "daily_max_f": archive_high,
                "daily_max_source": "archive",
            }

    record["actual"] = actual

    # ── Errors (forecast - actual) ──
    errors: dict = {}
    actual_high = actual.get("daily_max_f")
    if actual_high is not None:
        for model_key in ["nws", "gfs", "icon", "ecmwf"]:
            fc = forecasts.get(model_key, {})
            if "high_f" in fc:
                errors[model_key] = round(fc["high_f"] - actual_high, 1)
        ens = forecasts.get("ensemble_mean", {})
        if "high_f" in ens:
            errors["ensemble"] = round(ens["high_f"] - actual_high, 1)
        combined = forecasts.get("combined", {})
        if "high_f" in combined:
            errors["combined"] = round(combined["high_f"] - actual_high, 1)
        # Backfill archive forecast error
        af = forecasts.get("archive_forecast", {})
        if "high_f" in af:
            errors["archive_forecast"] = round(af["high_f"] - actual_high, 1)

    record["errors"] = errors

    return record


async def collect_all_cities(
    cities: list[CityConfig],
    target: date,
    is_backfill: bool = False,
) -> list[dict]:
    """Collect data for all cities on a given date."""
    async with httpx.AsyncClient(timeout=30) as client:
        # Process in batches of 5 to respect rate limits
        results = []
        batch_size = 5
        for i in range(0, len(cities), batch_size):
            batch = cities[i:i + batch_size]
            tasks = [collect_city_day(c, target, client, is_backfill) for c in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in batch_results:
                if isinstance(r, Exception):
                    logger.error("Collection failed for batch item: %s", r)
                else:
                    results.append(r)
            if i + batch_size < len(cities):
                await asyncio.sleep(1.0)  # pause between batches
        return results


def append_to_jsonl(records: list[dict]) -> int:
    """Append records to JSONL file, deduplicating by (date, city)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing keys for dedup
    existing_keys: set[tuple[str, str]] = set()
    if JSONL_FILE.exists():
        with open(JSONL_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    existing_keys.add((rec["date"], rec["city"]))
                except (json.JSONDecodeError, KeyError):
                    pass

    new_count = 0
    with open(JSONL_FILE, "a") as f:
        for rec in records:
            key = (rec["date"], rec["city"])
            if key in existing_keys:
                logger.debug("Skip duplicate: %s %s", rec["date"], rec["city"])
                continue
            f.write(json.dumps(rec, default=str) + "\n")
            existing_keys.add(key)
            new_count += 1

    return new_count


# ── Model comparison snapshot ─────────────────────────────────────

def generate_comparison_snapshot(target: date | None = None) -> dict | None:
    """Generate ICON vs ECMWF comparison from collected data.

    Reads daily_forecasts.jsonl and computes per-city and aggregate
    accuracy metrics for each model.
    """
    if not JSONL_FILE.exists():
        logger.warning("No data file found — run collection first")
        return None

    target_str = (target or date.today()).isoformat() if target else None
    records: list[dict] = []
    with open(JSONL_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                records.append(rec)
            except json.JSONDecodeError:
                continue

    if not records:
        return None

    # Compute per-model errors
    model_errors: dict[str, list[float]] = {
        "nws": [], "gfs": [], "icon": [], "ecmwf": [],
        "ensemble": [], "combined": [],
    }
    city_errors: dict[str, dict[str, list[float]]] = {}

    for rec in records:
        errors = rec.get("errors", {})
        city = rec["city"]
        if city not in city_errors:
            city_errors[city] = {m: [] for m in model_errors}
        for model, err in errors.items():
            if model in model_errors and err is not None:
                model_errors[model].append(err)
                city_errors[city][model].append(err)

    def stats(errs: list[float]) -> dict | None:
        if not errs:
            return None
        n = len(errs)
        mean = sum(errs) / n
        mae = sum(abs(e) for e in errs) / n
        rmse = (sum(e ** 2 for e in errs) / n) ** 0.5
        return {
            "n": n,
            "mean_error": round(mean, 2),
            "mae": round(mae, 2),
            "rmse": round(rmse, 2),
            "min": round(min(errs), 1),
            "max": round(max(errs), 1),
        }

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_records": len(records),
        "date_range": {
            "first": min(r["date"] for r in records),
            "last": max(r["date"] for r in records),
        },
        "aggregate": {model: stats(errs) for model, errs in model_errors.items()},
        "per_city": {},
    }

    for city, models in sorted(city_errors.items()):
        city_stats = {m: stats(errs) for m, errs in models.items() if errs}
        if city_stats:
            snapshot["per_city"][city] = city_stats

    return snapshot


# ── CLI ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Collect weather forecast & actual data for all cities",
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Target date (YYYY-MM-DD). Default: today",
    )
    parser.add_argument(
        "--backfill", type=int, default=0,
        help="Backfill N days of historical data",
    )
    parser.add_argument(
        "--compare", action="store_true",
        help="Generate model comparison snapshot (icon_vs_ecmwf_*.json)",
    )
    args = parser.parse_args()

    # Load city configs
    config = load_config()
    cities = config.cities
    logger.info("Loaded %d cities from config", len(cities))

    if args.compare:
        snapshot = generate_comparison_snapshot()
        if snapshot:
            out_file = DATA_DIR / f"icon_vs_ecmwf_{date.today().isoformat()}.json"
            out_file.write_text(json.dumps(snapshot, indent=2, default=str))
            logger.info("Comparison snapshot written to %s", out_file)

            # Print summary
            print("\n=== Model Comparison ===")
            agg = snapshot["aggregate"]
            for model in ["nws", "gfs", "icon", "ecmwf", "ensemble", "combined"]:
                s = agg.get(model)
                if s:
                    print(f"  {model:10s}  MAE={s['mae']:.2f}°F  RMSE={s['rmse']:.2f}°F  bias={s['mean_error']:+.2f}°F  (n={s['n']})")
        else:
            print("No data available for comparison. Run collection first.")
        return

    if args.backfill > 0:
        # Backfill mode: collect past N days
        today = date.today()
        dates = [today - timedelta(days=i) for i in range(1, args.backfill + 1)]
        dates.reverse()  # oldest first

        total_new = 0
        for target in dates:
            logger.info("Backfilling %s ...", target)
            records = asyncio.run(collect_all_cities(cities, target, is_backfill=True))
            new = append_to_jsonl(records)
            total_new += new
            logger.info("  %s: %d records (%d new)", target, len(records), new)

        logger.info("Backfill complete: %d new records across %d days", total_new, len(dates))
        print(f"\nBackfill complete: {total_new} new records for {len(dates)} days")
        return

    # Normal mode: collect target date
    target = date.fromisoformat(args.date) if args.date else date.today()
    logger.info("Collecting data for %s (%d cities)", target, len(cities))

    records = asyncio.run(collect_all_cities(cities, target))
    new = append_to_jsonl(records)

    # Summary
    has_forecast = sum(1 for r in records if r.get("forecasts"))
    has_actual = sum(1 for r in records if r.get("actual", {}).get("daily_max_f") is not None or r.get("actual", {}).get("metar_temp_f") is not None)
    has_errors = sum(1 for r in records if r.get("errors"))

    logger.info(
        "Collection done: %d cities, %d with forecast, %d with actual, %d with errors, %d new records",
        len(records), has_forecast, has_actual, has_errors, new,
    )
    print(f"\n{target}: {len(records)} cities collected ({new} new), "
          f"{has_forecast} forecasts, {has_actual} actuals, {has_errors} errors")

    # Show quick model summary if we have errors
    model_errs: dict[str, list[float]] = {}
    for r in records:
        for m, e in r.get("errors", {}).items():
            if e is not None:
                model_errs.setdefault(m, []).append(e)
    if model_errs:
        print("\nToday's forecast errors:")
        for model in ["nws", "gfs", "icon", "ecmwf", "ensemble", "combined"]:
            errs = model_errs.get(model, [])
            if errs:
                mae = sum(abs(e) for e in errs) / len(errs)
                bias = sum(errs) / len(errs)
                print(f"  {model:10s}  MAE={mae:.1f}°F  bias={bias:+.1f}°F  (n={len(errs)})")


if __name__ == "__main__":
    main()
