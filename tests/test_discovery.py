"""Tests for market discovery parsing utilities."""
from src.config import CityConfig
from src.markets.discovery import _match_city, _parse_date, _parse_temp_bounds


class TestParseTempBounds:
    def test_range(self):
        assert _parse_temp_bounds("78°F to 81°F") == (78.0, 81.0)

    def test_range_no_symbols(self):
        assert _parse_temp_bounds("78 to 81") == (78.0, 81.0)

    def test_above(self):
        assert _parse_temp_bounds("82°F or above") == (82.0, None)

    def test_below(self):
        assert _parse_temp_bounds("Below 60°F") == (None, 60.0)

    def test_single(self):
        assert _parse_temp_bounds("75°F") == (75.0, 75.0)

    def test_no_match(self):
        assert _parse_temp_bounds("something else") == (None, None)

    def test_range_with_dash(self):
        assert _parse_temp_bounds("78F-81F") == (78.0, 81.0)


class TestParseDate:
    def test_month_day(self):
        d = _parse_date("April 5")
        assert d is not None
        assert d.month == 4
        assert d.day == 5

    def test_iso(self):
        d = _parse_date("2026-04-05")
        assert d is not None
        assert d.month == 4
        assert d.day == 5
        assert d.year == 2026

    def test_invalid(self):
        assert _parse_date("not a date") is None


class TestMatchCity:
    def test_exact_match(self):
        cities = [CityConfig("New York", "KLGA", 40.7, -74.0)]
        assert _match_city("New York", cities) is not None

    def test_partial_match(self):
        cities = [CityConfig("New York", "KLGA", 40.7, -74.0)]
        assert _match_city("New York City", cities) is not None

    def test_no_match(self):
        cities = [CityConfig("New York", "KLGA", 40.7, -74.0)]
        assert _match_city("London", cities) is None
