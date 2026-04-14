"""Temperature rounding and daily-max finality helpers.

Polymarket weather markets settle on whole-degree Fahrenheit values as
reported by Weather Underground (WU).  WU uses **half-up rounding**
(not Python's default banker's rounding), so 54.5°F → 55, not 54.

All slot-boundary comparisons must go through wu_round() so that the
bot's internal logic matches the settlement precision.
"""
from __future__ import annotations

import math
from datetime import datetime


def wu_round(temp_f: float) -> int:
    """Mimic Weather Underground whole-degree rounding (half-up, not banker's).

    Examples:
        54.4 → 54
        54.5 → 55  (half-up, NOT banker's 54)
        55.5 → 56
        -0.5 → 0
    """
    return math.floor(temp_f + 0.5)


def is_daily_max_final(
    local_now: datetime,
    observations: list[tuple[str, float]],
    *,
    post_peak_hour: int = 17,
    stability_window_minutes: int = 60,
) -> bool:
    """Determine whether today's daily maximum temperature is final.

    The daily max is considered final when:
    1. We are past the peak temperature window (local hour >= post_peak_hour), AND
    2. The max temperature has been stable (no new high) for at least
       stability_window_minutes.

    Before post_peak_hour, even if temperature has been stable, a new high
    could still arrive — so we never declare final.

    Args:
        local_now: Current time in the city's local timezone.
        observations: List of (iso_timestamp, temp_f) from DailyMaxTracker.
        post_peak_hour: Hour (0-23) after which peak window is considered over.
        stability_window_minutes: Minutes without a new high to confirm stability.

    Returns:
        True if the daily max can be treated as final.
    """
    if local_now.hour < post_peak_hour:
        return False

    if not observations:
        return False

    # Find the time of the last new-high observation
    max_temp = max(t for _, t in observations)
    last_high_time: datetime | None = None
    for ts_str, temp in observations:
        if temp >= max_temp:
            try:
                last_high_time = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                continue

    if last_high_time is None:
        return False

    # Ensure last_high_time is timezone-aware for comparison
    if last_high_time.tzinfo is not None and local_now.tzinfo is not None:
        elapsed_minutes = (local_now - last_high_time).total_seconds() / 60.0
    else:
        # Can't compare naive vs aware — be conservative
        return False

    return elapsed_minutes >= stability_window_minutes


def slot_contains_degree(
    slot_lower_f: float | None,
    slot_upper_f: float | None,
    degree: int,
) -> bool:
    """Check if an integer degree falls within a slot's range.

    Slot semantics (matching Polymarket):
    - Range [L, U]: degree is in slot if L <= degree <= U
    - "Below X" (lower=None, upper=X): degree < X  (exclusive upper)
    - "≥X" (lower=X, upper=None): degree >= X

    Note: For "Below X" slots, the upper bound is exclusive per Polymarket
    rules. A daily max that rounds to exactly X does NOT fall in the
    "Below X" slot — it falls in the next slot up.
    """
    if slot_lower_f is not None and slot_upper_f is not None:
        return int(slot_lower_f) <= degree <= int(slot_upper_f)
    if slot_lower_f is None and slot_upper_f is not None:
        # "Below X" — X is exclusive upper
        return degree < int(slot_upper_f)
    if slot_lower_f is not None and slot_upper_f is None:
        # "≥X" — X is inclusive lower
        return degree >= int(slot_lower_f)
    return False
