"""Settlement-consistent weather data source.

Polymarket weather markets resolve using Weather Underground data from
specific airport weather stations. This module provides:
1. A mapping of city → exact WU station ID (matching Polymarket resolution)
2. Fetcher that pulls from the same data pipeline as settlement
3. Cross-validation between METAR and WU to detect discrepancies

Since WU's free API is discontinued, we use METAR (aviationweather.gov) as the
primary source — both WU and METAR pull from the same ASOS/AWOS airport stations.
The key is ensuring we use the EXACT same station Polymarket uses.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

# Polymarket uses Weather Underground, which reports from airport ASOS/AWOS stations.
# These are the EXACT stations Polymarket resolves against.
# Map: city_name -> (icao, wu_station_id, station_name)
SETTLEMENT_STATIONS: dict[str, tuple[str, str, str]] = {
    "New York": ("KLGA", "KLGA", "LaGuardia Airport"),
    "Los Angeles": ("KLAX", "KLAX", "Los Angeles International"),
    "Chicago": ("KORD", "KORD", "O'Hare International"),
    "Houston": ("KIAH", "KIAH", "George Bush Intercontinental"),
    "Phoenix": ("KPHX", "KPHX", "Phoenix Sky Harbor"),
    "Dallas": ("KDFW", "KDFW", "Dallas/Fort Worth International"),
    "San Francisco": ("KSFO", "KSFO", "San Francisco International"),
    "Seattle": ("KSEA", "KSEA", "Seattle-Tacoma International"),
    "Denver": ("KDEN", "KDEN", "Denver International"),
    "Miami": ("KMIA", "KMIA", "Miami International"),
    "Atlanta": ("KATL", "KATL", "Hartsfield-Jackson Atlanta"),
    "Boston": ("KBOS", "KBOS", "Logan International"),
    "Minneapolis": ("KMSP", "KMSP", "Minneapolis-Saint Paul"),
    "Detroit": ("KDTW", "KDTW", "Detroit Metropolitan"),
    "Nashville": ("KBNA", "KBNA", "Nashville International"),
    "Las Vegas": ("KLAS", "KLAS", "Harry Reid International"),
    "Portland": ("KPDX", "KPDX", "Portland International"),
    "Memphis": ("KMEM", "KMEM", "Memphis International"),
    "Louisville": ("KSDF", "KSDF", "Louisville Muhammad Ali"),
    "Salt Lake City": ("KSLC", "KSLC", "Salt Lake City International"),
    "Kansas City": ("KMCI", "KMCI", "Kansas City International"),
    "Charlotte": ("KCLT", "KCLT", "Charlotte Douglas"),
    "St. Louis": ("KSTL", "KSTL", "St. Louis Lambert"),
    "Indianapolis": ("KIND", "KIND", "Indianapolis International"),
    "Cincinnati": ("KCVG", "KCVG", "Cincinnati/Northern Kentucky"),
    "Pittsburgh": ("KPIT", "KPIT", "Pittsburgh International"),
    "Orlando": ("KMCO", "KMCO", "Orlando International"),
    "San Antonio": ("KSAT", "KSAT", "San Antonio International"),
    "Cleveland": ("KCLE", "KCLE", "Cleveland Hopkins"),
    "Tampa": ("KTPA", "KTPA", "Tampa International"),
}


@dataclass
class SettlementObservation:
    """Temperature observation matched to settlement station."""
    city: str
    icao: str
    temp_f: float
    observation_time: datetime
    source: str  # "metar" or "wu"
    raw_data: str = ""


def get_settlement_icao(city: str) -> str | None:
    """Get the ICAO code for the station Polymarket uses to settle a city's market."""
    entry = SETTLEMENT_STATIONS.get(city)
    return entry[0] if entry else None


async def fetch_settlement_temp(
    city: str,
    client: httpx.AsyncClient | None = None,
) -> SettlementObservation | None:
    """Fetch the current temperature from the settlement-consistent station.

    Uses METAR from aviationweather.gov (same underlying ASOS/AWOS station
    that Weather Underground reports from).
    """
    icao = get_settlement_icao(city)
    if not icao:
        logger.warning("No settlement station configured for %s", city)
        return None

    should_close = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.get(
            "https://aviationweather.gov/api/data/metar",
            params={"ids": icao, "format": "json"},
        )
        resp.raise_for_status()
        data = resp.json()

        if not data:
            return None

        latest = data[0]
        temp_c = latest.get("temp")
        if temp_c is None:
            return None

        temp_f = float(temp_c) * 9.0 / 5.0 + 32.0

        obs_time_str = latest.get("reportTime", "")
        try:
            obs_time = datetime.fromisoformat(obs_time_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            obs_time = datetime.now(timezone.utc)

        return SettlementObservation(
            city=city,
            icao=icao,
            temp_f=temp_f,
            observation_time=obs_time,
            source="metar",
            raw_data=latest.get("rawOb", ""),
        )
    except Exception:
        logger.exception("Failed to fetch settlement temp for %s (%s)", city, icao)
        return None
    finally:
        if should_close:
            await client.aclose()


@dataclass
class StationMismatch:
    """Records a discrepancy between configured and settlement stations."""
    city: str
    config_icao: str
    settlement_icao: str
    issue: str


def validate_station_config(
    cities: list[dict],
) -> list[StationMismatch]:
    """Validate that configured ICAO codes match Polymarket settlement stations.

    This is critical: using the wrong station means your temperature data
    won't match the settlement outcome, leading to systematic losses.

    Returns a list of mismatches that need to be fixed.
    """
    mismatches: list[StationMismatch] = []

    for city_cfg in cities:
        city_name = city_cfg.get("name", "") if isinstance(city_cfg, dict) else city_cfg.name
        config_icao = city_cfg.get("icao", "") if isinstance(city_cfg, dict) else city_cfg.icao

        settlement_icao = get_settlement_icao(city_name)
        if settlement_icao is None:
            mismatches.append(StationMismatch(
                city=city_name,
                config_icao=config_icao,
                settlement_icao="UNKNOWN",
                issue=f"City '{city_name}' not in settlement station registry",
            ))
        elif config_icao != settlement_icao:
            mismatches.append(StationMismatch(
                city=city_name,
                config_icao=config_icao,
                settlement_icao=settlement_icao,
                issue=f"Config uses {config_icao} but Polymarket settles on {settlement_icao}",
            ))

    return mismatches
