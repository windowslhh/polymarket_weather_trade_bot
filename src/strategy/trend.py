"""Forecast trend detection across rebalance cycles.

Tracks how forecasts change over time to classify the market regime
into STABLE, BREAKOUT_UP, BREAKOUT_DOWN, or SETTLING states.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)

# Thresholds for trend classification
BREAKOUT_THRESHOLD_F = 3.0  # °F change to classify as breakout
STABLE_THRESHOLD_F = 1.0    # °F change below which is stable
MAX_HISTORY = 24            # max forecasts to keep per city


class TrendState(str, Enum):
    STABLE = "STABLE"            # forecast barely moving
    BREAKOUT_UP = "BREAKOUT_UP"  # forecast rising sharply
    BREAKOUT_DOWN = "BREAKOUT_DOWN"  # forecast falling sharply
    SETTLING = "SETTLING"        # approaching settlement, forecast converging


class ForecastTrend:
    """Track forecast history and detect trend states per city."""

    def __init__(self) -> None:
        self._history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)

    def update(self, city: str, predicted_high_f: float, timestamp: datetime | None = None) -> None:
        """Record a new forecast observation."""
        ts = timestamp or datetime.now(timezone.utc)
        history = self._history[city]
        history.append((ts, predicted_high_f))
        if len(history) > MAX_HISTORY:
            self._history[city] = history[-MAX_HISTORY:]

    def get_trend(self, city: str, hours_to_settlement: float | None = None) -> TrendState:
        """Classify the current trend state for a city.

        Uses the last 3 forecasts to determine direction and magnitude.
        """
        history = self._history.get(city, [])

        # Near settlement with stable forecast → SETTLING
        if hours_to_settlement is not None and hours_to_settlement <= 6:
            if len(history) >= 2:
                recent_delta = abs(history[-1][1] - history[-2][1])
                if recent_delta < STABLE_THRESHOLD_F:
                    return TrendState.SETTLING

        if len(history) < 2:
            return TrendState.STABLE

        # Look at the cumulative change over last 3 readings
        lookback = history[-min(3, len(history)):]
        total_delta = lookback[-1][1] - lookback[0][1]

        if abs(total_delta) < STABLE_THRESHOLD_F:
            return TrendState.STABLE
        elif total_delta >= BREAKOUT_THRESHOLD_F:
            return TrendState.BREAKOUT_UP
        elif total_delta <= -BREAKOUT_THRESHOLD_F:
            return TrendState.BREAKOUT_DOWN
        else:
            return TrendState.STABLE

    def get_delta(self, city: str) -> float:
        """Get the forecast change between the last two readings."""
        history = self._history.get(city, [])
        if len(history) < 2:
            return 0.0
        return history[-1][1] - history[-2][1]

    def get_history(self, city: str) -> list[tuple[datetime, float]]:
        """Get full forecast history for a city."""
        return list(self._history.get(city, []))
