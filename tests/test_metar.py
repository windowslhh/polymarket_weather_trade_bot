"""Tests for METAR parsing and daily max tracking."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
        assert tracker.get_max("KLGA", day=date(2026, 4, 4)) == 72.0

        tracker.update(obs2)
        assert tracker.get_max("KLGA", day=date(2026, 4, 4)) == 78.0

        # Lower temp doesn't reduce max
        tracker.update(obs3)
        assert tracker.get_max("KLGA", day=date(2026, 4, 4)) == 78.0

    def test_separate_days(self):
        tracker = DailyMaxTracker()
        obs1 = Observation(icao="KLGA", temp_f=80.0,
                          observation_time=datetime(2026, 4, 4, 14, 0, tzinfo=timezone.utc))
        obs2 = Observation(icao="KLGA", temp_f=70.0,
                          observation_time=datetime(2026, 4, 5, 10, 0, tzinfo=timezone.utc))

        tracker.update(obs1)
        tracker.update(obs2)

        assert tracker.get_max("KLGA", day=date(2026, 4, 4)) == 80.0
        assert tracker.get_max("KLGA", day=date(2026, 4, 5)) == 70.0

    def test_separate_stations(self):
        tracker = DailyMaxTracker()
        obs1 = Observation(icao="KLGA", temp_f=80.0,
                          observation_time=datetime(2026, 4, 4, 14, 0, tzinfo=timezone.utc))
        obs2 = Observation(icao="KLAX", temp_f=90.0,
                          observation_time=datetime(2026, 4, 4, 14, 0, tzinfo=timezone.utc))

        tracker.update(obs1)
        tracker.update(obs2)

        assert tracker.get_max("KLGA", day=date(2026, 4, 4)) == 80.0
        assert tracker.get_max("KLAX", day=date(2026, 4, 4)) == 90.0

    def test_get_max_no_data(self):
        tracker = DailyMaxTracker()
        assert tracker.get_max("KLGA", day=date(2026, 1, 1)) is None

    def test_cleanup(self):
        tracker = DailyMaxTracker()
        obs = Observation(icao="KLGA", temp_f=75.0,
                         observation_time=datetime(2026, 4, 3, 14, 0, tzinfo=timezone.utc))
        tracker.update(obs)

        # cleanup_old has 1-day buffer, so keep_date=4/5 removes 4/3
        tracker.cleanup_old(keep_date=date(2026, 4, 5))
        assert tracker.get_max("KLGA", day=date(2026, 4, 3)) is None

    def test_observations_recorded(self):
        tracker = DailyMaxTracker()
        obs1 = Observation(icao="KLGA", temp_f=65.0,
                          observation_time=datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc))
        obs2 = Observation(icao="KLGA", temp_f=70.0,
                          observation_time=datetime(2026, 4, 4, 12, 0, tzinfo=timezone.utc))
        obs3 = Observation(icao="KLGA", temp_f=68.0,
                          observation_time=datetime(2026, 4, 4, 14, 0, tzinfo=timezone.utc))

        tracker.update(obs1)
        tracker.update(obs2)
        tracker.update(obs3)

        series = tracker.get_observations("KLGA", day=date(2026, 4, 4))
        assert len(series) == 3
        assert series[0] == (obs1.observation_time.isoformat(), 65.0)
        assert series[1] == (obs2.observation_time.isoformat(), 70.0)
        assert series[2] == (obs3.observation_time.isoformat(), 68.0)

    def test_observations_dedup(self):
        tracker = DailyMaxTracker()
        t = datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc)
        obs1 = Observation(icao="KLGA", temp_f=65.0, observation_time=t)
        obs2 = Observation(icao="KLGA", temp_f=65.0, observation_time=t)

        tracker.update(obs1)
        tracker.update(obs2)

        series = tracker.get_observations("KLGA", day=date(2026, 4, 4))
        assert len(series) == 1

    def test_observations_cleanup(self):
        tracker = DailyMaxTracker()
        obs = Observation(icao="KLGA", temp_f=72.0,
                         observation_time=datetime(2026, 4, 3, 14, 0, tzinfo=timezone.utc))
        tracker.update(obs)

        assert len(tracker.get_observations("KLGA", day=date(2026, 4, 3))) == 1
        # 1-day buffer: keep_date=4/5 removes 4/3 (4/5 - 1 = 4/4, and 4/3 < 4/4)
        tracker.cleanup_old(keep_date=date(2026, 4, 5))
        assert tracker.get_observations("KLGA", day=date(2026, 4, 3)) == []

    def test_observations_empty(self):
        tracker = DailyMaxTracker()
        assert tracker.get_observations("KXYZ", day=date(2026, 1, 1)) == []
        assert tracker.get_observations("KXYZ", day=date(2026, 1, 2)) == []


class TestTimezoneAwareDailyMax:
    """Verify DailyMaxTracker groups observations by local date, not UTC."""

    def test_utc_midnight_crossover_pacific(self):
        """A KLAX obs at 00:23 UTC Apr 12 = 5:23 PM PDT Apr 11.

        Must be grouped under Apr 11 (local), NOT Apr 12 (UTC).
        This is the exact bug that caused the LA false LOCKED WIN.
        """
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLAX", "America/Los_Angeles")

        # 00:23 UTC on April 12 = 17:23 PDT on April 11
        obs = Observation(
            icao="KLAX", temp_f=66.0,
            observation_time=datetime(2026, 4, 12, 0, 23, tzinfo=timezone.utc),
        )
        tracker.update(obs)

        # Should be under April 11 (local), NOT April 12
        assert tracker.get_max("KLAX", day=date(2026, 4, 11)) == 66.0
        assert tracker.get_max("KLAX", day=date(2026, 4, 12)) is None

    def test_utc_midnight_crossover_eastern(self):
        """KLGA at 03:30 UTC Apr 12 = 11:30 PM EDT Apr 11."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLGA", "America/New_York")

        # 03:30 UTC on April 12 = 23:30 EDT on April 11
        obs = Observation(
            icao="KLGA", temp_f=60.0,
            observation_time=datetime(2026, 4, 12, 3, 30, tzinfo=timezone.utc),
        )
        tracker.update(obs)

        assert tracker.get_max("KLGA", day=date(2026, 4, 11)) == 60.0
        assert tracker.get_max("KLGA", day=date(2026, 4, 12)) is None

    def test_local_afternoon_stays_same_day(self):
        """KLAX obs at 22:00 UTC Apr 11 = 3:00 PM PDT Apr 11 → same day."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLAX", "America/Los_Angeles")

        obs = Observation(
            icao="KLAX", temp_f=75.0,
            observation_time=datetime(2026, 4, 11, 22, 0, tzinfo=timezone.utc),
        )
        tracker.update(obs)

        assert tracker.get_max("KLAX", day=date(2026, 4, 11)) == 75.0
        assert tracker.get_max("KLAX", day=date(2026, 4, 12)) is None

    def test_local_morning_after_midnight_utc(self):
        """KLAX obs at 15:00 UTC Apr 12 = 8:00 AM PDT Apr 12 → Apr 12."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLAX", "America/Los_Angeles")

        obs = Observation(
            icao="KLAX", temp_f=58.0,
            observation_time=datetime(2026, 4, 12, 15, 0, tzinfo=timezone.utc),
        )
        tracker.update(obs)

        assert tracker.get_max("KLAX", day=date(2026, 4, 12)) == 58.0
        assert tracker.get_max("KLAX", day=date(2026, 4, 11)) is None

    def test_mixed_days_correct_max(self):
        """Observations spanning UTC midnight should split correctly."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLAX", "America/Los_Angeles")

        # Apr 11 5:00 PM PDT (= Apr 12 00:00 UTC) → local Apr 11
        obs_apr11_evening = Observation(
            icao="KLAX", temp_f=66.0,
            observation_time=datetime(2026, 4, 12, 0, 0, tzinfo=timezone.utc),
        )
        # Apr 12 8:00 AM PDT (= Apr 12 15:00 UTC) → local Apr 12
        obs_apr12_morning = Observation(
            icao="KLAX", temp_f=55.0,
            observation_time=datetime(2026, 4, 12, 15, 0, tzinfo=timezone.utc),
        )

        tracker.update(obs_apr11_evening)
        tracker.update(obs_apr12_morning)

        assert tracker.get_max("KLAX", day=date(2026, 4, 11)) == 66.0
        assert tracker.get_max("KLAX", day=date(2026, 4, 12)) == 55.0

    def test_no_timezone_registered_falls_back_to_utc(self):
        """Without register_timezone, falls back to UTC date (old behavior)."""
        tracker = DailyMaxTracker()
        # No register_timezone call

        obs = Observation(
            icao="KLAX", temp_f=66.0,
            observation_time=datetime(2026, 4, 12, 0, 23, tzinfo=timezone.utc),
        )
        tracker.update(obs)

        # Fallback: uses UTC date, so it goes under Apr 12
        assert tracker.get_max("KLAX", day=date(2026, 4, 12)) == 66.0

    def test_observations_grouped_by_local_date(self):
        """Observation series should be grouped by local date."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLAX", "America/Los_Angeles")

        # Both at UTC Apr 12, but local dates differ
        obs1 = Observation(
            icao="KLAX", temp_f=66.0,
            observation_time=datetime(2026, 4, 12, 0, 0, tzinfo=timezone.utc),  # PDT Apr 11
        )
        obs2 = Observation(
            icao="KLAX", temp_f=55.0,
            observation_time=datetime(2026, 4, 12, 15, 0, tzinfo=timezone.utc),  # PDT Apr 12
        )

        tracker.update(obs1)
        tracker.update(obs2)

        series_11 = tracker.get_observations("KLAX", day=date(2026, 4, 11))
        series_12 = tracker.get_observations("KLAX", day=date(2026, 4, 12))

        assert len(series_11) == 1
        assert series_11[0][1] == 66.0
        assert len(series_12) == 1
        assert series_12[0][1] == 55.0

    def test_midnight_crossover_update_returns_wrong_day_max(self):
        """Production bug 2026-04-15: near midnight EDT, tracker.update() returns
        the max for the NEXT local day (just a nighttime reading) instead of
        the current day's actual peak.

        At 04:03 UTC = 00:03 EDT Apr 15, a METAR observation maps to local
        date Apr 15.  tracker.update() returns the Apr 15 max (69°F nighttime),
        not the Apr 14 max (85°F daytime peak from backfill).

        The rebalancer must use get_max(icao, day=event.market_date) instead of
        the return value from update() to avoid this mismatch.
        """
        tracker = DailyMaxTracker()
        tracker.register_timezone("KATL", "America/New_York")

        # Backfill: daytime peak on Apr 14 (EDT) at 19:00 UTC = 15:00 EDT
        peak_obs = Observation(
            icao="KATL", temp_f=85.0,
            observation_time=datetime(2026, 4, 14, 19, 0, tzinfo=timezone.utc),
        )
        tracker.update(peak_obs)
        assert tracker.get_max("KATL", day=date(2026, 4, 14)) == 85.0

        # Live METAR just after midnight EDT:
        # 04:03 UTC Apr 15 = 00:03 EDT Apr 15 → keyed to Apr 15
        midnight_obs = Observation(
            icao="KATL", temp_f=69.0,
            observation_time=datetime(2026, 4, 15, 4, 3, tzinfo=timezone.utc),
        )
        max_from_update, _ = tracker.update(midnight_obs)

        # update() returns Apr 15 max (69°F) — NOT the Apr 14 max (85°F)!
        assert max_from_update == 69.0

        # But get_max with explicit day= still returns the correct values:
        assert tracker.get_max("KATL", day=date(2026, 4, 14)) == 85.0
        assert tracker.get_max("KATL", day=date(2026, 4, 15)) == 69.0

    def test_pre_midnight_update_returns_correct_day(self):
        """Just before midnight EDT (03:53 UTC), update() still returns
        the correct day's max because the observation maps to Apr 14 local."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KATL", "America/New_York")

        # Backfill peak
        peak_obs = Observation(
            icao="KATL", temp_f=85.0,
            observation_time=datetime(2026, 4, 14, 19, 0, tzinfo=timezone.utc),
        )
        tracker.update(peak_obs)

        # METAR at 03:53 UTC = 23:53 EDT Apr 14 → still Apr 14
        pre_midnight_obs = Observation(
            icao="KATL", temp_f=69.0,
            observation_time=datetime(2026, 4, 15, 3, 53, tzinfo=timezone.utc),
        )
        max_from_update, _ = tracker.update(pre_midnight_obs)

        # update() returns Apr 14 max including the backfilled peak
        assert max_from_update == 85.0

    def test_cleanup_with_timezone_buffer(self):
        """cleanup_old has 1-day buffer to protect cross-timezone entries."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLAX", "America/Los_Angeles")

        obs = Observation(
            icao="KLAX", temp_f=70.0,
            observation_time=datetime(2026, 4, 11, 22, 0, tzinfo=timezone.utc),  # PDT Apr 11
        )
        tracker.update(obs)

        # keep_date=Apr 12 with 1-day buffer → cutoff = Apr 11 → Apr 11 data survives
        tracker.cleanup_old(keep_date=date(2026, 4, 12))
        assert tracker.get_max("KLAX", day=date(2026, 4, 11)) == 70.0

        # keep_date=Apr 13 with 1-day buffer → cutoff = Apr 12 → Apr 11 cleaned up
        tracker.cleanup_old(keep_date=date(2026, 4, 13))
        assert tracker.get_max("KLAX", day=date(2026, 4, 11)) is None
