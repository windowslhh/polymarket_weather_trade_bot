"""Tests for METAR parsing and daily max tracking."""
from datetime import date, datetime, timezone

from src.weather.metar import DailyMaxTracker, _celsius_to_fahrenheit
from src.weather.models import Observation


class TestCelsiusToFahrenheit:
    def test_freezing_point(self):
        assert _celsius_to_fahrenheit(0) == 32.0

    def test_boiling_point(self):
        assert _celsius_to_fahrenheit(100) == 212.0

    def test_body_temp(self):
        assert abs(_celsius_to_fahrenheit(37) - 98.6) < 0.1

    def test_negative(self):
        assert _celsius_to_fahrenheit(-40) == -40.0


class TestDailyMaxTracker:
    def test_tracks_max(self):
        tracker = DailyMaxTracker()
        obs1 = Observation(icao="KLGA", temp_f=72.0,
                          observation_time=datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc))
        obs2 = Observation(icao="KLGA", temp_f=78.0,
                          observation_time=datetime(2026, 4, 4, 14, 0, tzinfo=timezone.utc))
        obs3 = Observation(icao="KLGA", temp_f=75.0,
                          observation_time=datetime(2026, 4, 4, 18, 0, tzinfo=timezone.utc))

        tracker.update(obs1)
        assert tracker.get_max("KLGA", date(2026, 4, 4)) == 72.0

        tracker.update(obs2)
        assert tracker.get_max("KLGA", date(2026, 4, 4)) == 78.0

        # Lower temp doesn't reduce max
        tracker.update(obs3)
        assert tracker.get_max("KLGA", date(2026, 4, 4)) == 78.0

    def test_separate_days(self):
        tracker = DailyMaxTracker()
        obs1 = Observation(icao="KLGA", temp_f=80.0,
                          observation_time=datetime(2026, 4, 4, 14, 0, tzinfo=timezone.utc))
        obs2 = Observation(icao="KLGA", temp_f=70.0,
                          observation_time=datetime(2026, 4, 5, 10, 0, tzinfo=timezone.utc))

        tracker.update(obs1)
        tracker.update(obs2)

        assert tracker.get_max("KLGA", date(2026, 4, 4)) == 80.0
        assert tracker.get_max("KLGA", date(2026, 4, 5)) == 70.0

    def test_separate_stations(self):
        tracker = DailyMaxTracker()
        obs1 = Observation(icao="KLGA", temp_f=80.0,
                          observation_time=datetime(2026, 4, 4, 14, 0, tzinfo=timezone.utc))
        obs2 = Observation(icao="KLAX", temp_f=90.0,
                          observation_time=datetime(2026, 4, 4, 14, 0, tzinfo=timezone.utc))

        tracker.update(obs1)
        tracker.update(obs2)

        assert tracker.get_max("KLGA", date(2026, 4, 4)) == 80.0
        assert tracker.get_max("KLAX", date(2026, 4, 4)) == 90.0

    def test_get_max_no_data(self):
        tracker = DailyMaxTracker()
        assert tracker.get_max("KLGA") is None

    def test_cleanup(self):
        tracker = DailyMaxTracker()
        obs = Observation(icao="KLGA", temp_f=75.0,
                         observation_time=datetime(2026, 4, 3, 14, 0, tzinfo=timezone.utc))
        tracker.update(obs)

        tracker.cleanup_old(keep_date=date(2026, 4, 4))
        assert tracker.get_max("KLGA", date(2026, 4, 3)) is None
