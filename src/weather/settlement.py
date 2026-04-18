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
#
# GROUND-TRUTH: each entry is verified by inspecting live Gamma event
# resolutionSource URLs (https://www.wunderground.com/history/daily/us/<state>/<ICAO>).
# `check_station_alignment()` re-verifies this map against live Gamma on every
# startup so future drift is caught in seconds instead of months.  The entries
# below are grouped by commonly-misconfigured cities (these caused the
# 2026-04-17 Houston locked-win blow-up) and entries with multiple plausible
# airports — if you touch one, also add a `# verified via event-id ...` note.
#
# Commonly misconfigured (double-check before editing):
#   Houston:  KHOU (Hobby), NOT KIAH (Intercontinental)
#   Dallas:   KDAL (Love Field), NOT KDFW (DFW)
#   Denver:   KBKF (Buckley SFB), NOT KDEN (Denver Intl)
SETTLEMENT_STATIONS: dict[str, tuple[str, str, str]] = {
    "New York": ("KLGA", "KLGA", "LaGuardia Airport"),
    "Los Angeles": ("KLAX", "KLAX", "Los Angeles International"),
    "Chicago": ("KORD", "KORD", "O'Hare International"),
    # Houston: Hobby — verified from Gamma resolutionSource (.../us/tx/KHOU/...)
    "Houston": ("KHOU", "KHOU", "William P. Hobby Airport"),
    "Phoenix": ("KPHX", "KPHX", "Phoenix Sky Harbor"),
    # Dallas: Love Field — verified from Gamma resolutionSource (.../us/tx/KDAL/...)
    "Dallas": ("KDAL", "KDAL", "Dallas Love Field"),
    "San Francisco": ("KSFO", "KSFO", "San Francisco International"),
    "Seattle": ("KSEA", "KSEA", "Seattle-Tacoma International"),
    # Denver: Buckley SFB — verified from Gamma resolutionSource (.../us/co/KBKF/...)
    "Denver": ("KBKF", "KBKF", "Buckley Space Force Base"),
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


@dataclass
class AlignmentIssue:
    """A mismatch between the bot's configured ICAO and the live Gamma event.

    kind:
      - MISMATCH:    config says KXXX, Gamma event says KYYY → hard error
      - UNRESOLVED:  Gamma event has no machine-extractable K-code → warn only
      - NO_EVENT:    no active weather event discovered for this city → warn only
      - GAMMA_ERROR: discover_weather_markets itself threw — alignment check
                     was unable to run.  Surfaced as an explicit WARN in
                     main.py so operators notice vs. a silent "all clear".
                     Not a hard error (transient Gamma outages shouldn't
                     block deploys).
    """
    city: str
    config_icao: str
    gamma_icao: str  # empty when UNRESOLVED / NO_EVENT / GAMMA_ERROR
    event_id: str
    kind: str


async def check_station_alignment(
    cities: list,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[AlignmentIssue]:
    """Fail-fast startup guard.

    Pulls live Polymarket weather events and verifies each city's configured
    ICAO matches the K-code extracted from the Gamma event's resolutionSource.
    The bot used the wrong station for Houston/Dallas/Denver for ~2 years
    before this was caught manually (2026-04-17).  This check exists so the
    next such drift is caught on the next deploy.

    Returns a list of AlignmentIssue; empty list means all-clear.  The caller
    is responsible for the refuse-to-start policy (typically: any MISMATCH
    aborts startup; UNRESOLVED / NO_EVENT only log WARN).
    """
    # Local import avoids a circular dependency (settlement → discovery → …).
    from src.markets.discovery import discover_weather_markets

    issues: list[AlignmentIssue] = []
    try:
        events = await discover_weather_markets(cities, client=client, min_volume=0)
    except Exception:
        logger.exception(
            "check_station_alignment: failed to fetch Gamma events — live alignment "
            "check will be SKIPPED for this startup. Fix transient Gamma error and "
            "restart, or investigate if persistent.",
        )
        # Surface the failure as a sentinel issue so main.py can WARN
        # explicitly rather than treating an empty list as "all clear".
        return [AlignmentIssue(
            city="*", config_icao="", gamma_icao="",
            event_id="", kind="GAMMA_ERROR",
        )]

    # Collapse to one event per city.  Prefer events with a non-empty
    # extracted_icao — otherwise a Gamma listing where the first event lacks
    # a machine-extractable K-code would silently mask a MISMATCH in a later
    # event (review PR#5 Q5).
    by_city: dict[str, object] = {}
    for ev in events:
        prev = by_city.get(ev.city)
        ev_icao = (getattr(ev, "extracted_icao", "") or "")
        prev_icao = (getattr(prev, "extracted_icao", "") or "") if prev is not None else ""
        if prev is None or (not prev_icao and ev_icao):
            by_city[ev.city] = ev

    for city_cfg in cities:
        city_name = city_cfg.name if hasattr(city_cfg, "name") else city_cfg["name"]
        config_icao = city_cfg.icao if hasattr(city_cfg, "icao") else city_cfg["icao"]

        ev = by_city.get(city_name)
        if ev is None:
            issues.append(AlignmentIssue(
                city=city_name, config_icao=config_icao, gamma_icao="",
                event_id="", kind="NO_EVENT",
            ))
            continue

        gamma_icao = (getattr(ev, "extracted_icao", "") or "").upper()
        if not gamma_icao:
            issues.append(AlignmentIssue(
                city=city_name, config_icao=config_icao, gamma_icao="",
                event_id=getattr(ev, "event_id", ""), kind="UNRESOLVED",
            ))
            continue

        if gamma_icao != config_icao.upper():
            issues.append(AlignmentIssue(
                city=city_name, config_icao=config_icao, gamma_icao=gamma_icao,
                event_id=getattr(ev, "event_id", ""), kind="MISMATCH",
            ))

    return issues
