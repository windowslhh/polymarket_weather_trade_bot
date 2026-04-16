"""Tests for evaluate_no_signals(rejects=...) observability hook.

See docs/fixes/2026-04-16-strategy-p0-fixes.md#fix-3.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from src.config import StrategyConfig
from src.markets.models import TempSlot, WeatherMarketEvent
from src.strategy.evaluator import evaluate_no_signals
from src.weather.models import Forecast


def _slot(lower, upper, price_no=0.50, tid_no="no_1"):
    label = f"{lower}°F to {upper}°F" if (lower is not None and upper is not None) else (
        f"{lower}°F or above" if lower is not None else f"Below {upper}°F"
    )
    return TempSlot(
        token_id_yes="yes_1", token_id_no=tid_no,
        outcome_label=label, temp_lower_f=lower, temp_upper_f=upper,
        price_yes=1.0 - price_no, price_no=price_no,
    )


def _event(slots, city="Denver"):
    return WeatherMarketEvent(
        event_id="e1", condition_id="c1", city=city,
        market_date=date.today(), slots=slots,
        end_timestamp=datetime(2026, 4, 30, 23, 0, tzinfo=timezone.utc),
        title=f"Highest temperature in {city}",
    )


def _forecast(high=75.0):
    return Forecast(
        city="Denver", forecast_date=date.today(),
        predicted_high_f=high, predicted_low_f=high - 15,
        confidence_interval_f=3.0, source="test",
        fetched_at=datetime.now(timezone.utc),
    )


class TestRejectCapture:
    """evaluate_no_signals populates the rejects list when provided."""

    def test_none_param_is_zero_overhead(self):
        """No rejects param → original behaviour, no side effects."""
        event = _event([_slot(74, 78, price_no=0.50)])
        sigs = evaluate_no_signals(event, _forecast(75.0), StrategyConfig())
        # No exception, no change in return shape.
        assert isinstance(sigs, list)

    def test_price_too_high_is_captured(self):
        """Slot above max_no_price → reason=PRICE_TOO_HIGH."""
        cfg = StrategyConfig(no_distance_threshold_f=3, min_no_ev=-1.0, max_no_price=0.70)
        # Distant slot (passes distance), price 0.75 > cap 0.70
        event = _event([_slot(55, 59, price_no=0.75)])
        rejects: list[dict] = []
        evaluate_no_signals(event, _forecast(75.0), cfg, rejects=rejects)
        reasons = [r["reason"] for r in rejects]
        assert "PRICE_TOO_HIGH" in reasons

    def test_dist_too_close_is_captured(self):
        """Slot within distance threshold → reason=DIST_TOO_CLOSE."""
        cfg = StrategyConfig(no_distance_threshold_f=10, min_no_ev=-1.0)
        # Forecast=75, slot [73,77] midpoint 75 → distance 0
        event = _event([_slot(73, 77, price_no=0.50)])
        rejects: list[dict] = []
        evaluate_no_signals(event, _forecast(75.0), cfg, rejects=rejects)
        reasons = [r["reason"] for r in rejects]
        assert "DIST_TOO_CLOSE" in reasons
        close_entry = next(r for r in rejects if r["reason"] == "DIST_TOO_CLOSE")
        assert close_entry["distance_f"] < 10

    def test_ev_below_gate_is_captured(self):
        """Slot with positive distance but EV below threshold → EV_BELOW_GATE."""
        # distance=3 (z=1, cdf~0.84), market=0.60 → gap 0.24 avoids divergence;
        # EV ≈ 0.84*0.40 - 0.16*0.60 ≈ 0.24 < min_no_ev=0.60 → EV_BELOW_GATE
        cfg = StrategyConfig(no_distance_threshold_f=3, min_no_ev=0.60, max_no_price=0.95)
        event = _event([_slot(68, 72, price_no=0.60)])
        rejects: list[dict] = []
        sigs = evaluate_no_signals(event, _forecast(75.0), cfg, rejects=rejects)
        assert sigs == []
        reasons = [r["reason"] for r in rejects]
        assert "EV_BELOW_GATE" in reasons

    def test_price_too_low_is_captured(self):
        """Slot below min_no_price → reason=PRICE_TOO_LOW."""
        cfg = StrategyConfig(
            no_distance_threshold_f=3, min_no_ev=-1.0,
            min_no_price=0.20, max_no_price=0.95,
        )
        event = _event([_slot(55, 59, price_no=0.05)])
        rejects: list[dict] = []
        evaluate_no_signals(event, _forecast(75.0), cfg, rejects=rejects)
        reasons = [r["reason"] for r in rejects]
        assert "PRICE_TOO_LOW" in reasons

    def test_reject_entry_has_slot_metadata(self):
        """Each reject entry carries slot_label, price_no, and reason."""
        cfg = StrategyConfig(no_distance_threshold_f=3, min_no_ev=-1.0, max_no_price=0.70)
        event = _event([_slot(55, 59, price_no=0.75, tid_no="tok_abc")])
        rejects: list[dict] = []
        evaluate_no_signals(event, _forecast(75.0), cfg, rejects=rejects)
        assert len(rejects) == 1
        r = rejects[0]
        assert r["slot_label"] == "55°F to 59°F"
        assert r["token_id_no"] == "tok_abc"
        assert r["price_no"] == 0.75
        assert r["reason"] == "PRICE_TOO_HIGH"

    def test_accepted_slot_not_in_rejects(self):
        """Slots that generate signals do not appear in rejects list."""
        # price 0.60 keeps market-model gap < 0.50 → passes divergence guard
        cfg = StrategyConfig(no_distance_threshold_f=3, min_no_ev=-1.0, max_no_price=0.95)
        event = _event([_slot(60, 64, price_no=0.60)])
        rejects: list[dict] = []
        sigs = evaluate_no_signals(event, _forecast(75.0), cfg, rejects=rejects)
        assert len(sigs) == 1
        # Accepted slot should NOT have been recorded as a reject
        assert all(r.get("token_id_no") != "no_1" for r in rejects)
