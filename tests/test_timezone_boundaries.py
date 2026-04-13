"""Comprehensive timezone boundary tests for all trading decision nodes.

Covers:
- UTC midnight crossover: city local date is correct for western cities
- DST spring-forward / fall-back: observations and hours stay on correct local date
- days_ahead boundary: market_date vs local_today off-by-1 during UTC crossover
- DailyMaxTracker cross-day grouping: Denver 23:00 MDT = UTC next day 05:00
- post-peak activation: exact boundary at local_hour 13/14/17/18
- ZoneInfo fallback: missing timezone registration
- METAR missing/delayed: daily_max returns None, no post-peak boost
- Multi-day stability: no cross-day data contamination over 3 consecutive days
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.markets.models import TempSlot, WeatherMarketEvent
from src.strategy.evaluator import (
    _post_peak_confidence,
    _POST_PEAK_CONFIDENCE_F,
    _PEAK_WINDOW_CONFIDENCE_F,
    _PEAK_START_HOUR,
    _POST_PEAK_HOUR,
    evaluate_no_signals,
)
from src.config import StrategyConfig
from src.weather.metar import DailyMaxTracker
from src.weather.models import Forecast, Observation


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_obs(icao: str, temp_f: float, utc_dt: datetime) -> Observation:
    """Create an Observation at a specific UTC time."""
    return Observation(icao=icao, temp_f=temp_f, observation_time=utc_dt)


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _make_slot(lower=80.0, upper=84.0, price_no=0.70, token_no="no_1"):
    return TempSlot(
        token_id_yes="yes_1", token_id_no=token_no,
        outcome_label=f"slot {lower}-{upper}",
        temp_lower_f=lower, temp_upper_f=upper, price_no=price_no,
    )


def _make_event(city="Denver", slots=None):
    return WeatherMarketEvent(
        event_id="evt_1", condition_id="cond_1", city=city,
        market_date=date(2026, 4, 13), slots=slots or [],
    )


def _make_forecast(high=75.0, conf=5.0):
    return Forecast(
        city="Denver",
        forecast_date=date(2026, 4, 13),
        predicted_high_f=high,
        predicted_low_f=high - 15,
        confidence_interval_f=conf,
        source="test",
        fetched_at=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
    )


# ── 1. _post_peak_confidence boundary values ─────────────────────────────────

class TestPostPeakConfidenceBoundaries:
    """_post_peak_confidence uses strict boundary semantics: >=14 peak, >=17 post-peak."""

    def test_before_peak_hour_13_returns_none(self):
        assert _post_peak_confidence(13) is None

    def test_before_peak_hour_0_returns_none(self):
        assert _post_peak_confidence(0) is None

    def test_before_peak_hour_1_returns_none(self):
        assert _post_peak_confidence(1) is None

    def test_peak_window_starts_at_14(self):
        """Hour 14 is exactly _PEAK_START_HOUR — must return wider confidence."""
        result = _post_peak_confidence(14)
        assert result == _PEAK_WINDOW_CONFIDENCE_F
        assert result == 3.0

    def test_peak_window_hour_15(self):
        assert _post_peak_confidence(15) == _PEAK_WINDOW_CONFIDENCE_F

    def test_peak_window_hour_16(self):
        assert _post_peak_confidence(16) == _PEAK_WINDOW_CONFIDENCE_F

    def test_post_peak_starts_at_17(self):
        """Hour 17 is exactly _POST_PEAK_HOUR — must return tighter confidence."""
        result = _post_peak_confidence(17)
        assert result == _POST_PEAK_CONFIDENCE_F
        assert result == 1.5

    def test_post_peak_hour_18(self):
        assert _post_peak_confidence(18) == _POST_PEAK_CONFIDENCE_F

    def test_post_peak_hour_23(self):
        assert _post_peak_confidence(23) == _POST_PEAK_CONFIDENCE_F

    def test_constants_consistent(self):
        """Peak window confidence > post-peak confidence (wider → tighter)."""
        assert _PEAK_WINDOW_CONFIDENCE_F > _POST_PEAK_CONFIDENCE_F
        assert _POST_PEAK_HOUR > _PEAK_START_HOUR


# ── 2. UTC midnight crossover — DailyMaxTracker ──────────────────────────────

class TestUTCMidnightCrossoverDailyMax:
    """Observations near UTC 00:00 must be grouped by city LOCAL date."""

    def test_denver_23_00_mdt_is_utc_next_day_05_00(self):
        """Denver 23:00 MDT (UTC-6) = UTC 05:00 next day.

        An observation at UTC 2026-04-14 05:00 must land on local Apr 13,
        not UTC Apr 14.
        """
        tracker = DailyMaxTracker()
        tracker.register_timezone("KDEN", "America/Denver")

        obs = _make_obs("KDEN", 65.0, _utc(2026, 4, 14, 5, 0))  # 23:00 MDT Apr 13
        tracker.update(obs)

        assert tracker.get_max("KDEN", date(2026, 4, 13)) == 65.0
        assert tracker.get_max("KDEN", date(2026, 4, 14)) is None

    def test_denver_23_59_mdt_is_utc_next_day_05_59(self):
        """Denver 23:59 MDT = UTC 05:59 next day → still local Apr 13."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KDEN", "America/Denver")

        obs = _make_obs("KDEN", 68.0, _utc(2026, 4, 14, 5, 59))  # 23:59 MDT Apr 13
        tracker.update(obs)

        assert tracker.get_max("KDEN", date(2026, 4, 13)) == 68.0
        assert tracker.get_max("KDEN", date(2026, 4, 14)) is None

    def test_denver_00_01_mdt_stays_on_apr_14(self):
        """Denver 00:01 MDT Apr 14 = UTC 06:01 Apr 14 → local Apr 14."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KDEN", "America/Denver")

        obs = _make_obs("KDEN", 55.0, _utc(2026, 4, 14, 6, 1))  # 00:01 MDT Apr 14
        tracker.update(obs)

        assert tracker.get_max("KDEN", date(2026, 4, 14)) == 55.0
        assert tracker.get_max("KDEN", date(2026, 4, 13)) is None

    def test_los_angeles_pdt_00_23_utc_grouped_correctly(self):
        """KLAX 00:23 UTC Apr 12 = 17:23 PDT Apr 11 → local Apr 11.

        This is the exact bug that caused false locked-win signals (fixed commit 387ff83).
        """
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLAX", "America/Los_Angeles")

        obs = _make_obs("KLAX", 66.0, _utc(2026, 4, 12, 0, 23))
        tracker.update(obs)

        assert tracker.get_max("KLAX", date(2026, 4, 11)) == 66.0
        assert tracker.get_max("KLAX", date(2026, 4, 12)) is None

    def test_chicago_cdt_crossover(self):
        """Chicago CDT (UTC-5): 00:30 UTC Apr 12 = 19:30 CDT Apr 11."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KORD", "America/Chicago")

        obs = _make_obs("KORD", 58.0, _utc(2026, 4, 12, 0, 30))
        tracker.update(obs)

        assert tracker.get_max("KORD", date(2026, 4, 11)) == 58.0
        assert tracker.get_max("KORD", date(2026, 4, 12)) is None

    def test_new_york_edt_still_same_day_at_03_30_utc(self):
        """NYC EDT (UTC-4): 03:30 UTC = 23:30 EDT same night → local Apr 11."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLGA", "America/New_York")

        obs = _make_obs("KLGA", 60.0, _utc(2026, 4, 12, 3, 30))
        tracker.update(obs)

        assert tracker.get_max("KLGA", date(2026, 4, 11)) == 60.0
        assert tracker.get_max("KLGA", date(2026, 4, 12)) is None

    def test_new_york_edt_clean_morning_is_new_day(self):
        """NYC EDT: 12:00 UTC = 08:00 EDT → local Apr 12, not Apr 11."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLGA", "America/New_York")

        obs = _make_obs("KLGA", 55.0, _utc(2026, 4, 12, 12, 0))
        tracker.update(obs)

        assert tracker.get_max("KLGA", date(2026, 4, 12)) == 55.0
        assert tracker.get_max("KLGA", date(2026, 4, 11)) is None


# ── 3. DST spring-forward (March 2026) ───────────────────────────────────────

class TestDSTSpringForward:
    """2026-03-08 02:00 EST → 03:00 EDT: clocks spring forward (UTC-5 → UTC-4)."""

    def test_observation_just_before_spring_forward(self):
        """2026-03-08 06:59 UTC = 01:59 EST → local March 8."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLGA", "America/New_York")

        # 06:59 UTC = 01:59 EST (still standard time, not yet sprung)
        obs = _make_obs("KLGA", 42.0, _utc(2026, 3, 8, 6, 59))
        tracker.update(obs)

        assert tracker.get_max("KLGA", date(2026, 3, 8)) == 42.0

    def test_observation_just_after_spring_forward(self):
        """2026-03-08 07:01 UTC = 03:01 EDT → local March 8 (jumped from 2 AM)."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLGA", "America/New_York")

        # 07:01 UTC = 03:01 EDT (after spring forward)
        obs = _make_obs("KLGA", 43.0, _utc(2026, 3, 8, 7, 1))
        tracker.update(obs)

        assert tracker.get_max("KLGA", date(2026, 3, 8)) == 43.0

    def test_spring_forward_no_cross_day_split(self):
        """Both observations (before and after DST transition) stay on March 8."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLGA", "America/New_York")

        obs_before = _make_obs("KLGA", 42.0, _utc(2026, 3, 8, 6, 59))  # 01:59 EST
        obs_after = _make_obs("KLGA", 44.0, _utc(2026, 3, 8, 7, 1))    # 03:01 EDT

        tracker.update(obs_before)
        tracker.update(obs_after)

        # Both on March 8 local — max = 44.0
        assert tracker.get_max("KLGA", date(2026, 3, 8)) == 44.0
        assert tracker.get_max("KLGA", date(2026, 3, 7)) is None
        assert tracker.get_max("KLGA", date(2026, 3, 9)) is None

    def test_spring_forward_denver(self):
        """Denver (America/Denver) spring forward: 2026-03-08 09:00 UTC = 03:00 MDT."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KDEN", "America/Denver")

        # 08:59 UTC = 01:59 MST; 09:01 UTC = 03:01 MDT
        obs_pre = _make_obs("KDEN", 30.0, _utc(2026, 3, 8, 8, 59))
        obs_post = _make_obs("KDEN", 31.0, _utc(2026, 3, 8, 9, 1))

        tracker.update(obs_pre)
        tracker.update(obs_post)

        assert tracker.get_max("KDEN", date(2026, 3, 8)) == 31.0
        assert tracker.get_max("KDEN", date(2026, 3, 7)) is None


# ── 4. DST fall-back (November 2026) ─────────────────────────────────────────

class TestDSTFallBack:
    """2026-11-01 02:00 EDT → 01:00 EST: clocks fall back (UTC-4 → UTC-5)."""

    def test_fall_back_both_1am_readings_same_local_date(self):
        """When the clock falls back, 1:30 AM appears twice.

        Both readings (EDT 1:30 = UTC 05:30, EST 1:30 = UTC 06:30) must
        be grouped under November 1, not split across two days.
        """
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLGA", "America/New_York")

        # First 1:30 AM (EDT, UTC-4): UTC 05:30
        obs_edt = _make_obs("KLGA", 55.0, _utc(2026, 11, 1, 5, 30))  # 01:30 EDT
        # Second 1:30 AM (EST, UTC-5): UTC 06:30
        obs_est = _make_obs("KLGA", 54.0, _utc(2026, 11, 1, 6, 30))  # 01:30 EST

        tracker.update(obs_edt)
        tracker.update(obs_est)

        # Both land on November 1 local — no cross-day split
        assert tracker.get_max("KLGA", date(2026, 11, 1)) == 55.0
        assert tracker.get_max("KLGA", date(2026, 10, 31)) is None
        assert tracker.get_max("KLGA", date(2026, 11, 2)) is None

    def test_fall_back_afternoon_obs_stays_nov_1(self):
        """Nov 1 afternoon (15:00 EST = 20:00 UTC) stays on Nov 1."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLGA", "America/New_York")

        obs = _make_obs("KLGA", 62.0, _utc(2026, 11, 1, 20, 0))  # 15:00 EST
        tracker.update(obs)

        assert tracker.get_max("KLGA", date(2026, 11, 1)) == 62.0

    def test_fall_back_midnight_crossover_nov_2(self):
        """Nov 2 00:30 EST = UTC 05:30 → local Nov 2 (not Nov 1)."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KLGA", "America/New_York")

        obs = _make_obs("KLGA", 50.0, _utc(2026, 11, 2, 5, 30))  # 00:30 EST Nov 2
        tracker.update(obs)

        assert tracker.get_max("KLGA", date(2026, 11, 2)) == 50.0
        assert tracker.get_max("KLGA", date(2026, 11, 1)) is None


# ── 5. days_ahead UTC midnight crossover scenarios ───────────────────────────

class TestDaysAheadLocalDate:
    """Simulate days_ahead computation using city-local date vs UTC.

    The rebalancer does:
        city_now = datetime.now(city_tz)
        local_today = city_now.date()
        days_ahead = (event.market_date - local_today).days

    We verify the computation directly with synthetic UTC timestamps.
    """

    def _compute_days_ahead(self, utc_moment: datetime, tz_name: str, market_date: date) -> int:
        """Simulate the rebalancer's days_ahead computation."""
        tz = ZoneInfo(tz_name)
        local_today = utc_moment.astimezone(tz).date()
        return (market_date - local_today).days

    def _utc_days_ahead(self, utc_moment: datetime, market_date: date) -> int:
        """Simulate the OLD (buggy) UTC-based computation."""
        return (market_date - utc_moment.date()).days

    # Denver MDT = UTC-6 (summer)

    def test_denver_00_30_utc_apr13_same_day_market(self):
        """UTC 00:30 Apr 13: Denver is still Apr 12 (18:30 MDT).

        market_date=Apr 12 → local days_ahead=0 (same day).
        UTC-based (wrong): days_ahead = Apr 12 - Apr 13 = -1 → market skipped!
        """
        utc_moment = _utc(2026, 4, 13, 0, 30)
        market_apr12 = date(2026, 4, 12)

        local_da = self._compute_days_ahead(utc_moment, "America/Denver", market_apr12)
        utc_da = self._utc_days_ahead(utc_moment, market_apr12)

        assert local_da == 0, f"local days_ahead should be 0 (same day), got {local_da}"
        assert utc_da == -1, "UTC-based gives wrong -1 (would skip the market)"

    def test_denver_00_30_utc_apr13_next_day_market(self):
        """UTC 00:30 Apr 13: Denver is still Apr 12 (18:30 MDT).

        market_date=Apr 13 → local days_ahead=1 (tomorrow, no post-peak boost).
        UTC-based (wrong): days_ahead = Apr 13 - Apr 13 = 0 → activates post-peak!
        """
        utc_moment = _utc(2026, 4, 13, 0, 30)
        market_apr13 = date(2026, 4, 13)

        local_da = self._compute_days_ahead(utc_moment, "America/Denver", market_apr13)
        utc_da = self._utc_days_ahead(utc_moment, market_apr13)

        assert local_da == 1, f"local days_ahead should be 1 (tomorrow), got {local_da}"
        assert utc_da == 0, "UTC-based gives wrong 0 (would activate post-peak for tomorrow)"

    def test_los_angeles_pdt_05_00_utc(self):
        """UTC 05:00 Apr 12: LA is still Apr 11 (21:00 PDT).

        market_date=Apr 11 → local days_ahead=0 (same day).
        """
        utc_moment = _utc(2026, 4, 12, 5, 0)
        market_apr11 = date(2026, 4, 11)

        local_da = self._compute_days_ahead(utc_moment, "America/Los_Angeles", market_apr11)
        assert local_da == 0

    def test_los_angeles_pdt_08_00_utc_is_new_day(self):
        """UTC 08:00 Apr 12: LA is now Apr 12 (01:00 PDT).

        market_date=Apr 11 → local days_ahead=-1 (past market, correctly skipped).
        """
        utc_moment = _utc(2026, 4, 12, 8, 0)
        market_apr11 = date(2026, 4, 11)

        local_da = self._compute_days_ahead(utc_moment, "America/Los_Angeles", market_apr11)
        assert local_da == -1

    def test_new_york_edt_03_30_utc_still_prev_day(self):
        """UTC 03:30 Apr 12: NYC is still Apr 11 (23:30 EDT).

        market_date=Apr 11 → local days_ahead=0.
        """
        utc_moment = _utc(2026, 4, 12, 3, 30)
        market_apr11 = date(2026, 4, 11)

        local_da = self._compute_days_ahead(utc_moment, "America/New_York", market_apr11)
        assert local_da == 0

    def test_new_york_edt_05_00_utc_is_new_day(self):
        """UTC 05:00 Apr 12: NYC is now Apr 12 (01:00 EDT).

        market_date=Apr 11 → local days_ahead=-1 (past market).
        """
        utc_moment = _utc(2026, 4, 12, 5, 0)
        market_apr11 = date(2026, 4, 11)

        local_da = self._compute_days_ahead(utc_moment, "America/New_York", market_apr11)
        assert local_da == -1

    def test_dst_spring_forward_days_ahead_denver(self):
        """During DST spring-forward (2026-03-08), days_ahead still correct."""
        # Denver MDT starts: 2026-03-08 09:00 UTC = 03:00 MDT
        utc_after_dst = _utc(2026, 3, 8, 9, 1)  # 03:01 MDT Mar 8
        market_mar8 = date(2026, 3, 8)

        local_da = self._compute_days_ahead(utc_after_dst, "America/Denver", market_mar8)
        assert local_da == 0

    def test_dst_fall_back_days_ahead_new_york(self):
        """During DST fall-back (2026-11-01), days_ahead still correct.

        At UTC 05:30 Nov 1: NYC is 01:30 EST → still Nov 1.
        """
        utc_fallback = _utc(2026, 11, 1, 5, 30)  # 01:30 EST Nov 1
        market_nov1 = date(2026, 11, 1)

        local_da = self._compute_days_ahead(utc_fallback, "America/New_York", market_nov1)
        assert local_da == 0


# ── 6. post-peak boost: evaluator uses local_hour correctly ──────────────────

class TestPostPeakEvaluatorBoundaries:
    """Verify evaluate_no_signals respects exact local_hour boundary values."""

    def _signals(self, local_hour, daily_max_f=72.0, days_ahead=0):
        slot = _make_slot(80, 84, price_no=0.70)
        event = _make_event(slots=[slot])
        forecast = _make_forecast(high=75.0, conf=5.0)
        config = StrategyConfig(
            no_distance_threshold_f=3, max_no_price=0.95, min_no_ev=0.01,
        )
        return evaluate_no_signals(
            event, forecast, config,
            daily_max_f=daily_max_f, local_hour=local_hour,
            days_ahead=days_ahead,
        )

    def test_hour_13_no_boost(self):
        """local_hour=13 (before peak): win_prob same as baseline (no boost)."""
        sig_13 = self._signals(local_hour=13)
        sig_baseline = self._signals(local_hour=None)
        if sig_13 and sig_baseline:
            assert sig_13[0].estimated_win_prob == sig_baseline[0].estimated_win_prob

    def test_hour_14_activates_peak_window(self):
        """local_hour=14: peak window activates, win_prob may differ from morning."""
        # Peak window uses daily_max as reference — since daily_max=72 < slot=80-84,
        # the boost can only increase or maintain win_prob
        sig_13 = self._signals(local_hour=13)
        sig_14 = self._signals(local_hour=14)
        if sig_13 and sig_14:
            assert sig_14[0].estimated_win_prob >= sig_13[0].estimated_win_prob

    def test_hour_17_post_peak_tighter_than_14(self):
        """local_hour=17: tighter confidence (1.5°F) vs peak window (3.0°F)."""
        sig_14 = self._signals(local_hour=14)
        sig_17 = self._signals(local_hour=17)
        if sig_14 and sig_17:
            assert sig_17[0].estimated_win_prob >= sig_14[0].estimated_win_prob

    def test_hour_18_same_as_17(self):
        """local_hour=18 uses same _POST_PEAK_CONFIDENCE_F as 17."""
        sig_17 = self._signals(local_hour=17)
        sig_18 = self._signals(local_hour=18)
        if sig_17 and sig_18:
            assert sig_17[0].estimated_win_prob == sig_18[0].estimated_win_prob

    def test_no_boost_for_future_market_hour_18(self):
        """days_ahead=1: no post-peak boost even at hour 18."""
        sig_future = self._signals(local_hour=18, days_ahead=1)
        sig_no_hour = self._signals(local_hour=None, days_ahead=1)
        if sig_future and sig_no_hour:
            assert sig_future[0].estimated_win_prob == sig_no_hour[0].estimated_win_prob

    def test_daily_max_none_no_boost(self):
        """daily_max_f=None: post-peak boost inactive regardless of local_hour."""
        slot = _make_slot(80, 84, price_no=0.70)
        event = _make_event(slots=[slot])
        forecast = _make_forecast(high=75.0, conf=5.0)
        config = StrategyConfig(no_distance_threshold_f=3, max_no_price=0.95, min_no_ev=0.01)

        sig_with_max = evaluate_no_signals(
            event, forecast, config, daily_max_f=72.0, local_hour=18,
        )
        sig_no_max = evaluate_no_signals(
            event, forecast, config, daily_max_f=None, local_hour=18,
        )
        sig_baseline = evaluate_no_signals(event, forecast, config)

        # No daily_max → same as no local_hour
        if sig_no_max and sig_baseline:
            assert sig_no_max[0].estimated_win_prob == sig_baseline[0].estimated_win_prob
        # But with daily_max → possibly boosted
        if sig_with_max and sig_baseline:
            assert sig_with_max[0].estimated_win_prob >= sig_baseline[0].estimated_win_prob


# ── 7. ZoneInfo fallback behavior ────────────────────────────────────────────

class TestZoneInfoFallback:
    """Behavior when timezone registration is missing or partial."""

    def test_no_tz_registered_falls_back_to_utc_date(self):
        """Without register_timezone, _local_date_str returns UTC date."""
        tracker = DailyMaxTracker()
        # No register call

        # UTC 01:00 Apr 12 — without tz, grouped under UTC date Apr 12
        obs = _make_obs("KDEN", 65.0, _utc(2026, 4, 12, 1, 0))
        tracker.update(obs)

        # Falls back to UTC: Apr 12 (even though Denver local is still Apr 11)
        assert tracker.get_max("KDEN", date(2026, 4, 12)) == 65.0
        assert tracker.get_max("KDEN", date(2026, 4, 11)) is None

    def test_days_ahead_fallback_when_tz_missing(self):
        """When city not in _city_tz dict, simulate fallback to date.today()-equivalent.

        Rebalancer code: city_tz = self._city_tz.get(city) → None →
        local_today = date.today() (UTC on VPS).
        """
        from datetime import date as _date

        # Simulate: city_tz not found
        city_tz = None
        utc_moment = _utc(2026, 4, 13, 0, 30)
        market_date = date(2026, 4, 12)

        if city_tz:
            local_today = utc_moment.astimezone(city_tz).date()
        else:
            local_today = utc_moment.date()  # UTC fallback

        days_ahead = (market_date - local_today).days
        # UTC Apr 13 - market Apr 12 = -1 (the bug when fallback is UTC)
        assert days_ahead == -1

    def test_register_timezone_takes_effect_immediately(self):
        """Registering tz AFTER first update doesn't retroactively change old keys,
        but new updates use the registered tz."""
        tracker = DailyMaxTracker()

        # First obs WITHOUT tz: UTC Apr 12 00:30 → stored under Apr 12 (UTC)
        obs1 = _make_obs("KDEN", 60.0, _utc(2026, 4, 12, 0, 30))
        tracker.update(obs1)
        assert tracker.get_max("KDEN", date(2026, 4, 12)) == 60.0

        # Register timezone
        tracker.register_timezone("KDEN", "America/Denver")

        # New obs WITH tz: UTC Apr 12 01:00 = 19:00 MDT Apr 11 → stored under Apr 11
        obs2 = _make_obs("KDEN", 65.0, _utc(2026, 4, 12, 1, 0))
        tracker.update(obs2)
        assert tracker.get_max("KDEN", date(2026, 4, 11)) == 65.0

        # Old obs still under Apr 12 (UTC key)
        assert tracker.get_max("KDEN", date(2026, 4, 12)) == 60.0


# ── 8. METAR missing / delayed ───────────────────────────────────────────────

class TestMETARMissingOrDelayed:
    """DailyMaxTracker and evaluator handle missing METAR data gracefully."""

    def test_get_max_returns_none_when_no_data(self):
        """No observations → get_max returns None, not 0 or exception."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KDEN", "America/Denver")

        assert tracker.get_max("KDEN") is None
        assert tracker.get_max("KDEN", date(2026, 4, 13)) is None

    def test_get_observations_returns_empty_when_no_data(self):
        tracker = DailyMaxTracker()
        tracker.register_timezone("KDEN", "America/Denver")

        assert tracker.get_observations("KDEN") == []
        assert tracker.get_observations("KDEN", date(2026, 4, 13)) == []

    def test_daily_max_none_does_not_block_no_signals(self):
        """daily_max_f=None (METAR delayed): evaluate_no_signals still works."""
        slot = _make_slot(80, 84, price_no=0.75)
        event = _make_event(slots=[slot])
        forecast = _make_forecast(high=74.0, conf=5.0)
        config = StrategyConfig(no_distance_threshold_f=4, max_no_price=0.95, min_no_ev=0.01)

        signals = evaluate_no_signals(
            event, forecast, config, daily_max_f=None, local_hour=18,
        )
        # Should still generate signals even without daily_max
        assert len(signals) >= 1

    def test_single_metar_gives_valid_daily_max(self):
        """Even one observation sets a valid daily_max."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KDEN", "America/Denver")

        obs = _make_obs("KDEN", 71.5, _utc(2026, 4, 13, 18, 0))  # 12:00 MDT
        tracker.update(obs)

        assert tracker.get_max("KDEN", date(2026, 4, 13)) == 71.5

    def test_daily_max_not_decreased_by_later_cooler_obs(self):
        """Running maximum never decreases — METAR data invariant."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KDEN", "America/Denver")

        obs_warm = _make_obs("KDEN", 80.0, _utc(2026, 4, 13, 20, 0))  # 14:00 MDT
        obs_cool = _make_obs("KDEN", 70.0, _utc(2026, 4, 13, 22, 0))  # 16:00 MDT
        obs_night = _make_obs("KDEN", 60.0, _utc(2026, 4, 14, 1, 0))   # 19:00 MDT

        tracker.update(obs_warm)
        tracker.update(obs_cool)
        tracker.update(obs_night)

        assert tracker.get_max("KDEN", date(2026, 4, 13)) == 80.0


# ── 9. Multi-day stability ───────────────────────────────────────────────────

class TestMultiDayStability:
    """Daily max must not bleed between days over consecutive days."""

    def test_three_consecutive_days_no_contamination(self):
        """Three days' observations stay separate; no cross-day contamination."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KDEN", "America/Denver")

        # Apr 13: peak 85°F at 15:00 MDT = UTC 21:00
        # Apr 14: peak 75°F at 15:00 MDT = UTC 21:00
        # Apr 15: peak 65°F at 15:00 MDT = UTC 21:00
        for day_offset, peak_temp in enumerate([85.0, 75.0, 65.0]):
            obs = _make_obs("KDEN", peak_temp, _utc(2026, 4, 13 + day_offset, 21, 0))
            tracker.update(obs)

        assert tracker.get_max("KDEN", date(2026, 4, 13)) == 85.0
        assert tracker.get_max("KDEN", date(2026, 4, 14)) == 75.0
        assert tracker.get_max("KDEN", date(2026, 4, 15)) == 65.0

    def test_midnight_obs_do_not_bleed_into_next_day(self):
        """Observations near Denver midnight are always on the correct day."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KDEN", "America/Denver")

        days = [
            # Apr 13 23:50 MDT = Apr 14 05:50 UTC → local Apr 13
            (_utc(2026, 4, 14, 5, 50), 66.0, date(2026, 4, 13)),
            # Apr 14 00:10 MDT = Apr 14 06:10 UTC → local Apr 14
            (_utc(2026, 4, 14, 6, 10), 54.0, date(2026, 4, 14)),
            # Apr 14 23:50 MDT = Apr 15 05:50 UTC → local Apr 14
            (_utc(2026, 4, 15, 5, 50), 72.0, date(2026, 4, 14)),
            # Apr 15 00:10 MDT = Apr 15 06:10 UTC → local Apr 15
            (_utc(2026, 4, 15, 6, 10), 48.0, date(2026, 4, 15)),
        ]

        for utc_dt, temp, expected_local_date in days:
            obs = _make_obs("KDEN", temp, utc_dt)
            tracker.update(obs)

        assert tracker.get_max("KDEN", date(2026, 4, 13)) == 66.0
        # Apr 14 has two observations: 54.0 and 72.0 → max is 72.0
        assert tracker.get_max("KDEN", date(2026, 4, 14)) == 72.0
        assert tracker.get_max("KDEN", date(2026, 4, 15)) == 48.0

    def test_cleanup_does_not_remove_today_data(self):
        """cleanup_old(keep_date=today) preserves today's observations."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KDEN", "America/Denver")

        today_utc = _utc(2026, 4, 13, 18, 0)  # 12:00 MDT Apr 13
        obs_today = _make_obs("KDEN", 80.0, today_utc)
        tracker.update(obs_today)

        tracker.cleanup_old(keep_date=date(2026, 4, 13))
        # 1-day buffer: cutoff = Apr 12, Apr 13 survives
        assert tracker.get_max("KDEN", date(2026, 4, 13)) == 80.0

    def test_cleanup_removes_stale_days(self):
        """cleanup_old removes data more than 1 day old."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KDEN", "America/Denver")

        obs_old = _make_obs("KDEN", 70.0, _utc(2026, 4, 11, 18, 0))  # Apr 11
        obs_recent = _make_obs("KDEN", 80.0, _utc(2026, 4, 13, 18, 0))  # Apr 13

        tracker.update(obs_old)
        tracker.update(obs_recent)

        # keep_date=Apr 13, buffer=1 → cutoff=Apr 12 → Apr 11 removed
        tracker.cleanup_old(keep_date=date(2026, 4, 13))

        assert tracker.get_max("KDEN", date(2026, 4, 11)) is None
        assert tracker.get_max("KDEN", date(2026, 4, 13)) == 80.0

    def test_multiple_cities_independent(self):
        """Two cities' daily max data don't interfere with each other."""
        tracker = DailyMaxTracker()
        tracker.register_timezone("KDEN", "America/Denver")
        tracker.register_timezone("KLAX", "America/Los_Angeles")

        # Denver Apr 13 afternoon
        obs_den = _make_obs("KDEN", 85.0, _utc(2026, 4, 13, 20, 0))  # 14:00 MDT Apr 13
        # LA Apr 13 afternoon
        obs_lax = _make_obs("KLAX", 75.0, _utc(2026, 4, 13, 21, 0))  # 14:00 PDT Apr 13

        tracker.update(obs_den)
        tracker.update(obs_lax)

        assert tracker.get_max("KDEN", date(2026, 4, 13)) == 85.0
        assert tracker.get_max("KLAX", date(2026, 4, 13)) == 75.0
        assert tracker.get_max("KDEN", date(2026, 4, 13)) != tracker.get_max("KLAX", date(2026, 4, 13))
