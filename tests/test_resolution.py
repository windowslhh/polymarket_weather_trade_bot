"""Tests for resolution source parser."""
from __future__ import annotations

from src.markets.resolution import (
    _resolution_cache,
    extract_settlement_icao,
    parse_resolution_from_event,
    parse_resolution_source,
)

import pytest


@pytest.fixture(autouse=True)
def clear_cache():
    _resolution_cache.clear()
    yield
    _resolution_cache.clear()


def test_detects_nws():
    assert parse_resolution_source("e1", "Resolved using National Weather Service data") == "nws"


def test_detects_noaa():
    assert parse_resolution_source("e2", "Data from NOAA.gov station") == "noaa"


def test_detects_wunderground():
    assert parse_resolution_source("e3", "Weather Underground historical data") == "wunderground"


def test_detects_hko():
    assert parse_resolution_source("e4", "Hong Kong Observatory readings") == "hko"


def test_unknown_source():
    assert parse_resolution_source("e5", "Some random text") == "unknown"


def test_empty_description():
    assert parse_resolution_source("e6", "") == "unknown"


def test_caching():
    parse_resolution_source("e7", "NWS data")
    assert _resolution_cache["e7"] == "nws"
    # Second call returns cached
    assert parse_resolution_source("e7", "completely different text") == "nws"


def test_parse_from_event():
    event = {
        "id": "e8",
        "description": "This market resolves based on weather.gov data",
    }
    assert parse_resolution_from_event(event) == "nws"


def test_parse_from_event_resolution_field():
    event = {
        "id": "e9",
        "description": "",
        "resolutionSource": "NOAA official records",
    }
    assert parse_resolution_from_event(event) == "noaa"


class TestExtractSettlementIcao:
    def test_wunderground_url(self):
        event = {
            "description": "",
            "resolutionSource": "https://www.wunderground.com/history/daily/us/ny/KLGA/date/2026-4-17",
        }
        assert extract_settlement_icao(event) == "KLGA"

    def test_bare_kcode(self):
        event = {"description": "Resolves using Weather Underground station KDAL (Dallas Love Field)."}
        assert extract_settlement_icao(event) == "KDAL"

    def test_no_icao(self):
        event = {"description": "Resolves using NOAA data for New York City."}
        assert extract_settlement_icao(event) is None

    def test_empty(self):
        assert extract_settlement_icao({}) is None

    def test_majority_vote(self):
        # Two mentions of KBKF, one stray KDEN → KBKF wins
        event = {
            "description": "Settlement station: KBKF (Buckley).",
            "resolutionSource": "https://www.wunderground.com/history/daily/us/co/KBKF/",
            "rules": "Historical note: KDEN was used before the migration.",
        }
        assert extract_settlement_icao(event) == "KBKF"

    def test_houston_hobby_vs_iah(self):
        # The regression case from 2026-04-17: config had KIAH, Gamma actually KHOU.
        event = {
            "resolutionSource": "https://www.wunderground.com/history/daily/us/tx/KHOU/date/2026-4-17",
        }
        assert extract_settlement_icao(event) == "KHOU"
