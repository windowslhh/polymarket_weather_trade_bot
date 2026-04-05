"""Tests for resolution source parser."""
from __future__ import annotations

from src.markets.resolution import parse_resolution_source, parse_resolution_from_event, _resolution_cache

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
