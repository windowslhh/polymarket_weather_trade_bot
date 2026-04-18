"""Offline dry-run: simulate the full bot pipeline without external APIs.

Mocks all external dependencies (Gamma API, Open-Meteo, METAR, CLOB)
with realistic synthetic data, then runs the complete Rebalancer cycle
to validate the entire system end-to-end.

Run: python -m tests.dry_run_offline
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, ".")

from src.config import AppConfig, CityConfig, SchedulingConfig, StrategyConfig
from src.execution.executor import Executor
from src.markets.clob_client import ClobClient
from src.markets.models import TempSlot, WeatherMarketEvent
from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker
from src.strategy.rebalancer import Rebalancer
from src.weather.historical import ForecastErrorDistribution
from src.weather.metar import DailyMaxTracker
from src.weather.models import Forecast
from src.weather.settlement import SettlementObservation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dry_run")


# ── Synthetic market data generator ─────────────────────────────────────

def generate_weather_events(cities: list[CityConfig]) -> list[WeatherMarketEvent]:
    """Generate realistic Polymarket weather events for today."""
    events = []
    random.seed(42)

    # Simulated "current forecast" per city
    city_forecasts = {
        "New York": 72, "Los Angeles": 78, "Chicago": 65, "Houston": 85,
        "Phoenix": 95, "Dallas": 82, "San Francisco": 62, "Seattle": 58,
        "Denver": 68, "Miami": 87, "Atlanta": 76, "Boston": 64,
        "Minneapolis": 60, "Detroit": 63, "Nashville": 74, "Las Vegas": 90,
        "Portland": 60, "Memphis": 78, "Louisville": 70, "Salt Lake City": 65,
        "Kansas City": 72, "Charlotte": 73, "St. Louis": 71, "Indianapolis": 66,
        "Cincinnati": 67, "Pittsburgh": 64, "Orlando": 86, "San Antonio": 84,
        "Cleveland": 62, "Tampa": 85,
    }

    for city in cities:
        forecast_high = city_forecasts.get(city.name, 70)
        slots = []

        # Generate temperature slots: 2°F wide, from forecast-20 to forecast+20
        for offset in range(-20, 22, 2):
            lower = forecast_high + offset
            upper = lower + 2
            distance = abs(offset + 1)
            sigma = 4.0
            z = distance / sigma

            # Simulate market pricing with house edge
            fair_no = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
            edge = 0.03 * (1.0 + 0.5 * min(z, 3.0))
            price_no = min(fair_no + edge * (1.0 - fair_no) + random.gauss(0, 0.02), 0.98)
            price_no = max(price_no, 0.50)
            price_yes = round(1.0 - price_no, 4)

            slots.append(TempSlot(
                token_id_yes=f"yes_{city.name}_{lower}_{upper}",
                token_id_no=f"no_{city.name}_{lower}_{upper}",
                outcome_label=f"{lower} to {upper}°F",
                temp_lower_f=float(lower),
                temp_upper_f=float(upper),
                price_yes=round(price_yes, 4),
                price_no=round(price_no, 4),
            ))

        events.append(WeatherMarketEvent(
            event_id=f"evt_{city.name.lower().replace(' ', '_')}_{date.today()}",
            condition_id=f"cond_{city.name.lower().replace(' ', '_')}",
            city=city.name,
            market_date=date.today(),
            slots=slots,
            end_timestamp=datetime.now(timezone.utc) + timedelta(hours=8),
            title=f"Highest temperature in {city.name} on {date.today().strftime('%B %d')}",
        ))

    return events


def generate_forecasts(cities: list[CityConfig]) -> dict[str, Forecast]:
    """Generate forecasts matching the events."""
    city_highs = {
        "New York": 72, "Los Angeles": 78, "Chicago": 65, "Houston": 85,
        "Phoenix": 95, "Dallas": 82, "San Francisco": 62, "Seattle": 58,
        "Denver": 68, "Miami": 87, "Atlanta": 76, "Boston": 64,
        "Minneapolis": 60, "Detroit": 63, "Nashville": 74, "Las Vegas": 90,
        "Portland": 60, "Memphis": 78, "Louisville": 70, "Salt Lake City": 65,
        "Kansas City": 72, "Charlotte": 73, "St. Louis": 71, "Indianapolis": 66,
        "Cincinnati": 67, "Pittsburgh": 64, "Orlando": 86, "San Antonio": 84,
        "Cleveland": 62, "Tampa": 85,
    }
    forecasts = {}
    for city in cities:
        high = city_highs.get(city.name, 70)
        forecasts[city.name] = Forecast(
            city=city.name,
            forecast_date=date.today(),
            predicted_high_f=float(high),
            predicted_low_f=float(high - 15),
            confidence_interval_f=4.0,
            source="mock-open-meteo",
            fetched_at=datetime.now(timezone.utc),
        )
    return forecasts


def generate_observation(city: CityConfig) -> SettlementObservation | None:
    """Simulate a current METAR observation (morning, temp still rising)."""
    city_current = {
        "New York": 65, "Los Angeles": 72, "Chicago": 58, "Houston": 80,
        "Phoenix": 88, "Dallas": 76, "San Francisco": 58, "Seattle": 53,
        "Denver": 60, "Miami": 82, "Atlanta": 70, "Boston": 58,
        "Minneapolis": 54, "Detroit": 57, "Nashville": 68, "Las Vegas": 83,
        "Portland": 55, "Memphis": 72, "Louisville": 64, "Salt Lake City": 58,
        "Kansas City": 66, "Charlotte": 67, "St. Louis": 65, "Indianapolis": 60,
        "Cincinnati": 61, "Pittsburgh": 58, "Orlando": 80, "San Antonio": 78,
        "Cleveland": 56, "Tampa": 80,
    }
    temp = city_current.get(city.name)
    if temp is None:
        return None
    return SettlementObservation(
        city=city.name,
        icao=city.icao,
        temp_f=float(temp),
        observation_time=datetime.now(timezone.utc),
        source="mock-metar",
    )


# ── Main dry-run ────────────────────────────────────────────────────────

async def run_dry_run():
    config = AppConfig(
        strategy=StrategyConfig(
            no_distance_threshold_f=8,
            min_no_ev=0.02,
            max_position_per_slot_usd=5.0,
            max_exposure_per_city_usd=50.0,
            max_total_exposure_usd=1000.0,
            daily_loss_limit_usd=50.0,
            kelly_fraction=0.5,
        ),
        scheduling=SchedulingConfig(),
        cities=[
            CityConfig("New York", "KLGA", 40.7128, -74.006),
            CityConfig("Los Angeles", "KLAX", 34.0522, -118.244),
            CityConfig("Chicago", "KORD", 41.8781, -87.630),
            CityConfig("Houston", "KHOU", 29.6454, -95.279),
            CityConfig("Phoenix", "KPHX", 33.4484, -112.074),
            CityConfig("Dallas", "KDAL", 32.8471, -96.852),
            CityConfig("San Francisco", "KSFO", 37.7749, -122.419),
            CityConfig("Seattle", "KSEA", 47.6062, -122.332),
            CityConfig("Denver", "KBKF", 39.7017, -104.752),
            CityConfig("Miami", "KMIA", 25.7617, -80.192),
            CityConfig("Atlanta", "KATL", 33.749, -84.388),
            CityConfig("Boston", "KBOS", 42.3601, -71.059),
            CityConfig("Minneapolis", "KMSP", 44.9778, -93.265),
            CityConfig("Detroit", "KDTW", 42.3314, -83.046),
            CityConfig("Nashville", "KBNA", 36.1627, -86.782),
            CityConfig("Las Vegas", "KLAS", 36.1699, -115.140),
            CityConfig("Portland", "KPDX", 45.5152, -122.678),
            CityConfig("Memphis", "KMEM", 35.1495, -90.049),
            CityConfig("Louisville", "KSDF", 38.2527, -85.759),
            CityConfig("Salt Lake City", "KSLC", 40.7608, -111.891),
            CityConfig("Kansas City", "KMCI", 39.0997, -94.579),
            CityConfig("Charlotte", "KCLT", 35.2271, -80.843),
            CityConfig("St. Louis", "KSTL", 38.627, -90.199),
            CityConfig("Indianapolis", "KIND", 39.7684, -86.158),
            CityConfig("Cincinnati", "KCVG", 39.1031, -84.512),
            CityConfig("Pittsburgh", "KPIT", 40.4406, -79.996),
            CityConfig("Orlando", "KMCO", 28.5383, -81.379),
            CityConfig("San Antonio", "KSAT", 29.4241, -98.494),
            CityConfig("Cleveland", "KCLE", 41.4993, -81.694),
            CityConfig("Tampa", "KTPA", 27.9506, -82.457),
        ],
        dry_run=True,
        db_path=Path("/tmp/dry_run_bot.db"),
    )

    # Build mock error distributions (realistic synthetic)
    error_dists = {}
    for city in config.cities:
        errors = [random.gauss(0.5, 3.5) for _ in range(200)]
        error_dists[city.name] = ForecastErrorDistribution(city.name, errors)

    # Initialize real components (store, portfolio, executor)
    store = Store(config.db_path)
    await store.initialize()
    clob = ClobClient(config)
    portfolio = PortfolioTracker(store)
    executor = Executor(clob, portfolio)
    max_tracker = DailyMaxTracker()
    rebalancer = Rebalancer(config, clob, portfolio, executor, max_tracker, error_dists)

    # Generate synthetic market data
    events = generate_weather_events(config.cities)
    forecasts = generate_forecasts(config.cities)

    logger.info("=" * 70)
    logger.info("POLYMARKET WEATHER BOT — OFFLINE DRY RUN")
    logger.info("=" * 70)
    logger.info("Mode:       DRY RUN (no real orders)")
    logger.info("Cities:     %d", len(config.cities))
    logger.info("Markets:    %d events, %d total slots",
                len(events), sum(len(e.slots) for e in events))
    logger.info("Strategy:   NO threshold=%d°F, min EV=%.2f",
                config.strategy.no_distance_threshold_f, config.strategy.min_no_ev)
    logger.info("Limits:     $%.0f/slot, $%.0f/city, $%.0f total",
                config.strategy.max_position_per_slot_usd,
                config.strategy.max_exposure_per_city_usd,
                config.strategy.max_total_exposure_usd)
    logger.info("=" * 70)

    # Mock external API calls, inject our synthetic data
    async def mock_fetch_settlement(city_name, client=None):
        city_cfg = next((c for c in config.cities if c.name == city_name), None)
        if city_cfg:
            return generate_observation(city_cfg)
        return None

    with (
        patch("src.strategy.rebalancer.discover_weather_markets", new_callable=AsyncMock) as mock_disc,
        patch("src.strategy.rebalancer.get_forecasts_batch", new_callable=AsyncMock) as mock_fc,
        patch("src.strategy.rebalancer.fetch_settlement_temp", side_effect=mock_fetch_settlement),
        patch("src.strategy.rebalancer.validate_station_config", return_value=[]),
    ):
        mock_disc.return_value = events
        mock_fc.return_value = forecasts

        # Run the full rebalancer cycle
        signals = await rebalancer.run()

    # Report results
    logger.info("")
    logger.info("=" * 70)
    logger.info("DRY RUN RESULTS")
    logger.info("=" * 70)

    if not signals:
        logger.info("No signals generated.")
        await store.close()
        return

    # Group by city
    from collections import defaultdict
    by_city: dict[str, list] = defaultdict(list)
    for s in signals:
        by_city[s.event.city].append(s)

    total_exposure = 0.0
    total_signals = 0
    buy_count = 0
    sell_count = 0

    logger.info("")
    logger.info("%-18s %5s %5s %8s %8s  %s", "City", "BUY", "SELL", "Exposure", "Avg EV", "Top Signal")
    logger.info("-" * 80)

    for city in sorted(by_city.keys()):
        city_signals = by_city[city]
        buys = [s for s in city_signals if s.side.value == "BUY"]
        sells = [s for s in city_signals if s.side.value == "SELL"]
        exposure = sum(s.suggested_size_usd for s in buys)
        avg_ev = sum(s.expected_value for s in buys) / len(buys) if buys else 0
        top = max(buys, key=lambda s: s.expected_value) if buys else None
        top_str = f"NO {top.slot.outcome_label} (EV={top.expected_value:.4f})" if top else "-"

        logger.info("%-18s %5d %5d  $%6.2f   %.4f  %s",
                    city, len(buys), len(sells), exposure, avg_ev, top_str)

        total_exposure += exposure
        total_signals += len(city_signals)
        buy_count += len(buys)
        sell_count += len(sells)

    logger.info("-" * 80)
    logger.info("%-18s %5d %5d  $%6.2f", "TOTAL", buy_count, sell_count, total_exposure)

    # Portfolio state
    db_exposure = await portfolio.get_total_exposure()
    logger.info("")
    logger.info("Portfolio after execution:")
    logger.info("  Total exposure (DB): $%.2f", db_exposure)
    logger.info("  Positions opened:    %d", buy_count)
    logger.info("  Cities active:       %d / %d", len(by_city), len(config.cities))

    # Risk check
    from src.portfolio.risk import check_geographic_correlation
    corr = check_geographic_correlation(list(by_city.keys()), config.cities, 500)
    if corr:
        logger.info("")
        logger.info("Geographic correlation warnings:")
        for c1, c2, dist in corr:
            logger.info("  %s <-> %s: %.0f km", c1, c2, dist)

    logger.info("")
    logger.info("=" * 70)
    logger.info("DRY RUN COMPLETE — all systems operational")
    logger.info("=" * 70)

    await store.close()


if __name__ == "__main__":
    asyncio.run(run_dry_run())
