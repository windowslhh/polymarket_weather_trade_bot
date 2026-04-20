"""Declarative gate matrix for strategy signal evaluation.

Before M2 each `evaluate_*` function encoded its gate order inline, and
the next similar bug to Bug #1 (Houston 2026-04-17: price-divergence
gate added to the NO branch but forgotten on the locked-win branch)
would recur.  This module replaces that inline ordering with a single
declarative matrix so shared gates can be added in one place and every
branch that cares picks them up for free.

Design:

* A ``Gate`` is any object with a ``check(ctx) -> GateResult | None``
  method.  ``None`` means the gate is satisfied; a ``GateResult`` means
  the slot is blocked or — for TRIM — that a trigger fired.
* A ``GateContext`` bundles every input any gate might need.  Each gate
  reads only the fields it cares about and, where appropriate, caches
  its intermediate result on ``ctx`` so downstream gates in the same
  pass can reuse it (``ctx.distance``, ``ctx.win_prob``, ``ctx.ev`` …).
* ``GATE_MATRIX`` maps a ``SignalKind`` to the ordered list of gates for
  that branch.  Per-kind wrapper functions in ``evaluator.py`` walk the
  list; the wrapper interprets a ``GateResult`` as "reject" for entry
  kinds (NO, LOCKED_WIN, EXIT pre-filter) and as "fire" for TRIM
  trigger gates.

Ordering is still load-bearing — e.g. ``HeldTokenGate`` must run before
anything that would produce a decision-log reject entry, otherwise
already-held slots would spam the observability channel.  Keep that in
mind when editing ``GATE_MATRIX``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from src.config import StrategyConfig
from src.markets.models import TempSlot, WeatherMarketEvent
from src.strategy.temperature import wu_round
from src.strategy.trend import TrendState
from src.weather.historical import ForecastErrorDistribution
from src.weather.models import Forecast


class SignalKind(Enum):
    FORECAST_NO = "forecast_no"
    LOCKED_WIN = "locked_win"
    TRIM = "trim"
    EXIT_PREFILTER = "exit_prefilter"


@dataclass
class GateContext:
    """All inputs a gate may read.

    Fields are optional so a single dataclass covers NO / LOCKED_WIN /
    TRIM / EXIT.  Each gate checks only what it needs; unused fields are
    ignored.  Gates may populate derived fields (``distance``,
    ``win_prob``, ``ev`` …) so later gates in the same pass don't have
    to recompute.
    """
    slot: TempSlot
    event: WeatherMarketEvent
    config: StrategyConfig

    # Inputs (set by the wrapper before gate execution)
    forecast: Forecast | None = None
    error_dist: ForecastErrorDistribution | None = None
    daily_max_f: float | None = None
    daily_max_final: bool = False
    local_hour: int | None = None
    hours_to_settlement: float | None = None
    days_ahead: int = 0
    trend: TrendState | None = None
    held_token_ids: frozenset[str] = field(default_factory=frozenset)
    locked_win_token_ids: frozenset[str] = field(default_factory=frozenset)
    entry_prices: dict[str, float] = field(default_factory=dict)
    entry_ev_map: dict[str, float] = field(default_factory=dict)
    # Pre-computed per-event scalars (peak_conf / ev_threshold) for gates
    # that would otherwise recompute them per slot.
    peak_conf: float | None = None
    ev_threshold: float | None = None

    # Derived per-slot state (filled by gates; later gates read back).
    distance: float | None = None
    win_prob: float | None = None
    ev: float | None = None
    # Locked-win intermediate state
    is_locked: bool = False
    is_below_lock: bool = False
    lock_reason: str = ""


@dataclass
class GateResult:
    """Return value of ``Gate.check``.

    ``code`` — the decision-log reason for rejects (``PRICE_TOO_HIGH``,
    ``EV_BELOW_GATE`` …) or the trigger label for TRIM gates
    (``absolute`` / ``relative`` / ``price_stop``).  ``extra`` carries
    the metadata the decision_log sampler wants; it is ignored for
    silent rejections.  ``silent`` suppresses any logging — used for
    gates whose "rejection" is really just "not applicable to this
    slot" (e.g. ``HeldTokenGate``).
    """
    code: str
    extra: dict = field(default_factory=dict)
    silent: bool = False


class Gate(Protocol):
    def check(self, ctx: GateContext) -> GateResult | None: ...


# ──────────────────────────────────────────────────────────────────────
# Shared primitives — fee / win-prob helpers
# ──────────────────────────────────────────────────────────────────────

# Polymarket taker fee for the Weather category (as of 2026).  Matches
# the module constant in evaluator.py; duplicated here so gates.py has
# no circular import back to evaluator.py.
_TAKER_FEE_RATE: float = 0.0125


def _entry_fee_per_dollar(price: float) -> float:
    return _TAKER_FEE_RATE * 2.0 * price * (1.0 - price)


# ──────────────────────────────────────────────────────────────────────
# Shared entry-block gates (reused across NO + LOCKED_WIN)
# ──────────────────────────────────────────────────────────────────────


class HeldTokenGate:
    """Silent skip when the slot's NO token is already held."""

    def check(self, ctx: GateContext) -> GateResult | None:
        if ctx.held_token_ids and ctx.slot.token_id_no in ctx.held_token_ids:
            return GateResult(code="HELD", silent=True)
        return None


class PriceBoundsGate:
    """Reject slots with a clearly invalid NO price (0 or 1)."""

    def check(self, ctx: GateContext) -> GateResult | None:
        p = ctx.slot.price_no
        if p <= 0 or p >= 1:
            return GateResult(code="PRICE_INVALID")
        return None


class PriceFloorGate:
    """Reject NO below ``min_no_price`` (poor liquidity / inflated odds)."""

    def check(self, ctx: GateContext) -> GateResult | None:
        if ctx.slot.price_no < ctx.config.min_no_price:
            return GateResult(
                code="PRICE_TOO_LOW",
                extra={"min_no_price": ctx.config.min_no_price},
            )
        return None


class PriceCeilingGate:
    """Reject NO above ``max_no_price`` (risk/reward too asymmetric)."""

    def check(self, ctx: GateContext) -> GateResult | None:
        if ctx.slot.price_no > ctx.config.max_no_price:
            return GateResult(
                code="PRICE_TOO_HIGH",
                extra={"max_no_price": ctx.config.max_no_price},
            )
        return None


class DailyMaxAboveLowerGate:
    """≥X slot: when ``wu_round(daily_max) >= X``, YES is guaranteed."""

    def check(self, ctx: GateContext) -> GateResult | None:
        slot = ctx.slot
        if (
            ctx.days_ahead == 0
            and ctx.daily_max_f is not None
            and slot.temp_upper_f is None
            and slot.temp_lower_f is not None
            and wu_round(ctx.daily_max_f) >= int(slot.temp_lower_f)
        ):
            return GateResult(
                code="DAILY_MAX_ABOVE_LOWER",
                extra={"daily_max_f": ctx.daily_max_f},
            )
        return None


class DailyMaxInSlotGate:
    """Range slot [L, U]: block when ``wu_round(daily_max)`` is inside."""

    def check(self, ctx: GateContext) -> GateResult | None:
        slot = ctx.slot
        if (
            ctx.days_ahead == 0
            and ctx.daily_max_f is not None
            and slot.temp_lower_f is not None
            and slot.temp_upper_f is not None
            and int(slot.temp_lower_f)
            <= wu_round(ctx.daily_max_f)
            <= int(slot.temp_upper_f)
        ):
            return GateResult(
                code="DAILY_MAX_IN_SLOT",
                extra={"daily_max_f": ctx.daily_max_f},
            )
        return None


class DailyMaxBelowUpperGate:
    """'Below X' slot post-peak: daily_max still below X → YES likely wins."""

    def check(self, ctx: GateContext) -> GateResult | None:
        slot = ctx.slot
        if (
            ctx.peak_conf is not None
            and ctx.daily_max_f is not None
            and slot.temp_lower_f is None
            and slot.temp_upper_f is not None
            and wu_round(ctx.daily_max_f) < int(slot.temp_upper_f)
        ):
            return GateResult(
                code="DAILY_MAX_BELOW_UPPER",
                extra={"daily_max_f": ctx.daily_max_f},
            )
        return None


class DistanceGate:
    """Distance filter for NO entries.

    Uses the bias-corrected forecast (when an empirical error
    distribution with ≥ 30 samples is available).  Post-peak, the
    minimum of (forecast distance, observed distance) is taken so stale
    forecasts cannot pass a slot the actual temperature is already
    approaching.  Stores the final distance on ``ctx.distance`` for
    downstream gates, though at present no NO gate reads it.
    """

    def check(self, ctx: GateContext) -> GateResult | None:
        assert ctx.forecast is not None  # NO path requires forecast
        slot = ctx.slot

        # Bias-corrected forecast reference
        bias_corrected_f = ctx.forecast.predicted_high_f
        if ctx.error_dist is not None and ctx.error_dist._count >= 30:
            bias_corrected_f = ctx.forecast.predicted_high_f - ctx.error_dist.mean

        distance = _slot_distance(slot, bias_corrected_f)

        # Post-peak observed-distance merge — skipped when daily_max has
        # already exceeded the slot upper bound (NO is safe, obs distance
        # would be misleadingly small).
        if ctx.peak_conf is not None and ctx.daily_max_f is not None:
            if slot.temp_upper_f is None or ctx.daily_max_f <= slot.temp_upper_f:
                obs_distance = _slot_distance(slot, ctx.daily_max_f)
                distance = min(distance, obs_distance)

        ctx.distance = distance

        if distance < ctx.config.no_distance_threshold_f:
            return GateResult(
                code="DIST_TOO_CLOSE",
                extra={
                    "distance_f": distance,
                    "threshold_f": ctx.config.no_distance_threshold_f,
                },
            )
        return None


class EvThresholdGate:
    """Compute win_prob (with post-peak + trend boosts) and EV; reject
    when EV falls short of ``ctx.ev_threshold``.

    Caches ``ctx.win_prob`` and ``ctx.ev`` so ``PriceDivergenceGate``
    can reuse them instead of recomputing.
    """

    def check(self, ctx: GateContext) -> GateResult | None:
        assert ctx.forecast is not None
        assert ctx.ev_threshold is not None

        slot = ctx.slot
        win_prob = _estimate_no_win_prob(slot, ctx.forecast, ctx.error_dist)

        # Post-peak boost via observed daily_max
        if ctx.peak_conf is not None and ctx.daily_max_f is not None:
            obs_prob = _observed_no_win_prob(slot, ctx.daily_max_f, ctx.peak_conf)
            if obs_prob > win_prob:
                win_prob = obs_prob

        # Trend-based boost for breakout direction
        if (
            ctx.trend == TrendState.BREAKOUT_UP
            and slot.temp_lower_f is not None
            and slot.temp_upper_f is not None
            and slot.temp_upper_f < ctx.forecast.predicted_high_f
        ):
            win_prob = min(win_prob * 1.05, 0.99)
        elif (
            ctx.trend == TrendState.BREAKOUT_DOWN
            and slot.temp_upper_f is not None
            and slot.temp_lower_f is not None
            and slot.temp_lower_f > ctx.forecast.predicted_high_f
        ):
            win_prob = min(win_prob * 1.05, 0.99)

        ev = (
            win_prob * (1.0 - slot.price_no)
            - (1.0 - win_prob) * slot.price_no
            - _entry_fee_per_dollar(slot.price_no)
        )
        ctx.win_prob = win_prob
        ctx.ev = ev

        if ev < ctx.ev_threshold:
            return GateResult(
                code="EV_BELOW_GATE",
                extra={
                    "expected_value": ev,
                    "win_prob": win_prob,
                    "ev_threshold": ctx.ev_threshold,
                },
            )
        return None


class PriceDivergenceGate:
    """Reject when ``|win_prob − market_price_no|`` exceeds the configured
    threshold.  Shared by NO and LOCKED_WIN — duplicating this check was
    the root cause of Bug #1 (Houston 2026-04-17)."""

    def check(self, ctx: GateContext) -> GateResult | None:
        assert ctx.win_prob is not None
        threshold = ctx.config.price_divergence_threshold
        gap = abs(ctx.win_prob - ctx.slot.price_no)
        if gap > threshold:
            return GateResult(
                code="PRICE_DIVERGENCE",
                extra={
                    "win_prob": ctx.win_prob,
                    "market_implied": ctx.slot.price_no,
                    "gap": gap,
                    "threshold": threshold,
                },
            )
        return None


# ──────────────────────────────────────────────────────────────────────
# Locked-win gates
# ──────────────────────────────────────────────────────────────────────


class LockedWinDetectionGate:
    """Detect whether the slot qualifies for a locked-win signal.

    Populates ``ctx.is_locked``, ``ctx.is_below_lock``, ``ctx.lock_reason``.
    A silent block is returned when no lock condition fires, including
    the "≥X slot where daily_max ≥ X" case where YES is guaranteed and
    NO should never be purchased.
    """

    def check(self, ctx: GateContext) -> GateResult | None:
        assert ctx.daily_max_f is not None  # wrapper pre-checks
        slot = ctx.slot
        rounded_max = wu_round(ctx.daily_max_f)
        margin = ctx.config.locked_win_margin_f

        is_locked = False
        is_below_lock = False
        lock_reason = ""

        if slot.temp_upper_f is not None and slot.temp_lower_f is not None:
            upper_int = int(slot.temp_upper_f)
            lower_int = int(slot.temp_lower_f)
            if rounded_max > upper_int and (rounded_max - upper_int) >= margin:
                is_locked = True
                is_below_lock = True
                lock_reason = (
                    f"LOCKED WIN (below): wu_round({ctx.daily_max_f:.1f})={rounded_max} "
                    f"> upper {upper_int} + margin {margin}"
                )
            elif (
                ctx.daily_max_final
                and rounded_max < lower_int
                and (lower_int - rounded_max) >= margin
            ):
                is_locked = True
                lock_reason = (
                    f"LOCKED WIN (above): wu_round({ctx.daily_max_f:.1f})={rounded_max} "
                    f"< lower {lower_int} - margin {margin}"
                )
        elif slot.temp_lower_f is None and slot.temp_upper_f is not None:
            upper_int = int(slot.temp_upper_f)
            if rounded_max > upper_int and (rounded_max - upper_int) >= margin:
                is_locked = True
                is_below_lock = True
                lock_reason = (
                    f"LOCKED WIN (below): wu_round({ctx.daily_max_f:.1f})={rounded_max} "
                    f"> upper {upper_int} + margin {margin}"
                )
        elif slot.temp_upper_f is None and slot.temp_lower_f is not None:
            lower_int = int(slot.temp_lower_f)
            if rounded_max >= lower_int:
                return GateResult(code="LOCK_YES_GUARANTEED", silent=True)
            if ctx.daily_max_final and (lower_int - rounded_max) >= margin:
                is_locked = True
                lock_reason = (
                    f"LOCKED WIN (above): wu_round({ctx.daily_max_f:.1f})={rounded_max} "
                    f"< lower {lower_int} - margin {margin}"
                )

        if not is_locked:
            return GateResult(code="NOT_LOCKED", silent=True)

        ctx.is_locked = True
        ctx.is_below_lock = is_below_lock
        ctx.lock_reason = lock_reason
        return None


class LockedWinPriceCapGate:
    """Reject locked-win entries above ``locked_win_max_price``.

    Reinstated 2026-04-17 after production showed every locked-win
    firing at 0.997-0.9985 where paper→live slippage (≥ 1 tick) ate
    the razor-thin EV.  Silent with a debug log — see
    ``docs/fixes/2026-04-17-lockedwin-price-cap-rollback.md``.
    """

    def check(self, ctx: GateContext) -> GateResult | None:
        if ctx.slot.price_no > ctx.config.locked_win_max_price:
            return GateResult(
                code="LOCKED_WIN_PRICE_CAP",
                extra={
                    "price_no": ctx.slot.price_no,
                    "cap": ctx.config.locked_win_max_price,
                },
                silent=True,
            )
        return None


class LockedWinEvPositiveGate:
    """Compute win_prob (0.999 below-lock / 0.99 above-lock) and EV;
    reject silently when EV ≤ 0 (fee wipes the margin)."""

    def check(self, ctx: GateContext) -> GateResult | None:
        slot = ctx.slot
        win_prob = 0.999 if ctx.is_below_lock else 0.99
        ev = (
            win_prob * (1.0 - slot.price_no)
            - (1.0 - win_prob) * slot.price_no
            - _entry_fee_per_dollar(slot.price_no)
        )
        ctx.win_prob = win_prob
        ctx.ev = ev
        if ev <= 0:
            return GateResult(
                code="LOCKED_WIN_EV_NONPOSITIVE",
                extra={"expected_value": ev, "price_no": slot.price_no},
                silent=True,
            )
        return None


# ──────────────────────────────────────────────────────────────────────
# TRIM gates — semantics flipped: any fire means "trim"
# ──────────────────────────────────────────────────────────────────────


class TrimLockedWinGuardGate:
    """Pre-filter: never trim locked-win positions (forecast EV is
    misleading once daily_max has exceeded the slot upper)."""

    def check(self, ctx: GateContext) -> GateResult | None:
        if ctx.slot.token_id_no in ctx.locked_win_token_ids:
            return GateResult(code="TRIM_SKIP_LOCKED", silent=True)
        if (
            ctx.daily_max_f is not None
            and ctx.slot.temp_upper_f is not None
        ):
            gap = wu_round(ctx.daily_max_f) - int(ctx.slot.temp_upper_f)
            if gap >= ctx.config.locked_win_margin_f:
                return GateResult(
                    code="TRIM_SKIP_LOCKED_LIKE",
                    extra={"gap_f": gap},
                    silent=True,
                )
        return None


class AbsoluteEvGate:
    """TRIM trigger: current EV < -min_trim_ev_absolute."""

    def check(self, ctx: GateContext) -> GateResult | None:
        assert ctx.ev is not None
        if ctx.ev < -ctx.config.min_trim_ev_absolute:
            return GateResult(
                code="absolute",
                extra={"ev": ctx.ev, "threshold": -ctx.config.min_trim_ev_absolute},
            )
        return None


class RelativeEvDecayGate:
    """TRIM trigger: current EV < entry_ev × (1 − trim_ev_decay_ratio).

    Inactive when no positive entry_ev is known (legacy pre-migration
    positions) so absolute-only semantics kick in."""

    def check(self, ctx: GateContext) -> GateResult | None:
        assert ctx.ev is not None
        entry_ev = ctx.entry_ev_map.get(ctx.slot.token_id_no)
        if entry_ev is None or entry_ev <= 0:
            return None
        gate_ev = entry_ev * (1.0 - ctx.config.trim_ev_decay_ratio)
        if ctx.ev < gate_ev:
            return GateResult(
                code="relative",
                extra={"ev": ctx.ev, "entry_ev": entry_ev, "gate_ev": gate_ev},
            )
        return None


class PriceStopGate:
    """TRIM trigger: live NO price ≤ entry × (1 − trim_price_stop_ratio).

    Bug #3 (2026-04-18): catches the pathology where EV looks ~0 on
    stale inputs but the market is already rolling over — see
    ``CLAUDE.md`` 'TRIM triple-gate' entry.
    """

    def check(self, ctx: GateContext) -> GateResult | None:
        entry_price = ctx.entry_prices.get(ctx.slot.token_id_no)
        ratio = ctx.config.trim_price_stop_ratio
        if entry_price is None or entry_price <= 0:
            return None
        if not (0 < ratio < 1.0):
            return None
        threshold = entry_price * (1.0 - ratio)
        live_price = ctx.slot.price_no
        if live_price <= 0:
            return None
        if live_price <= threshold:
            return GateResult(
                code="price_stop",
                extra={
                    "entry_price": entry_price,
                    "live_price": live_price,
                    "threshold": threshold,
                },
            )
        return None


# ──────────────────────────────────────────────────────────────────────
# EXIT pre-filter gates
# ──────────────────────────────────────────────────────────────────────


class ExitLockedWinProtectionGate:
    """Layer 1: never exit a slot where daily_max > upper + margin."""

    def check(self, ctx: GateContext) -> GateResult | None:
        slot = ctx.slot
        if slot.temp_upper_f is None or ctx.daily_max_f is None:
            return None
        gap = wu_round(ctx.daily_max_f) - int(slot.temp_upper_f)
        if gap >= ctx.config.locked_win_margin_f:
            return GateResult(
                code="EXIT_SKIP_LOCKED",
                extra={"gap_f": gap},
                silent=True,
            )
        return None


# ──────────────────────────────────────────────────────────────────────
# GATE_MATRIX — the single declarative source
# ──────────────────────────────────────────────────────────────────────

GATE_MATRIX: dict[SignalKind, list[Gate]] = {
    SignalKind.FORECAST_NO: [
        HeldTokenGate(),
        DailyMaxAboveLowerGate(),
        DailyMaxInSlotGate(),
        DailyMaxBelowUpperGate(),
        DistanceGate(),
        PriceBoundsGate(),
        PriceFloorGate(),
        PriceCeilingGate(),
        EvThresholdGate(),
        PriceDivergenceGate(),
    ],
    SignalKind.LOCKED_WIN: [
        HeldTokenGate(),
        PriceBoundsGate(),
        PriceFloorGate(),
        LockedWinDetectionGate(),
        LockedWinPriceCapGate(),
        LockedWinEvPositiveGate(),
        PriceDivergenceGate(),
    ],
    SignalKind.TRIM: [
        TrimLockedWinGuardGate(),  # pre-filter (silent skip)
        AbsoluteEvGate(),          # trigger
        RelativeEvDecayGate(),     # trigger
        PriceStopGate(),           # trigger
    ],
    SignalKind.EXIT_PREFILTER: [
        ExitLockedWinProtectionGate(),
    ],
}


# ──────────────────────────────────────────────────────────────────────
# Private helpers — distance / probability.  Defined at the bottom so
# the gate classes above can reference them without a forward decl.
# ──────────────────────────────────────────────────────────────────────

import math  # noqa: E402  (kept local to this helper block)


_POST_PEAK_CONFIDENCE_F = 1.5
_PEAK_WINDOW_CONFIDENCE_F = 3.0
_PEAK_START_HOUR = 14
_POST_PEAK_HOUR = 17


def post_peak_confidence(local_hour: int) -> float | None:
    """Return the ±°F confidence interval for post-peak observed-max
    adjustments, or None when before the peak window."""
    if local_hour >= _POST_PEAK_HOUR:
        return _POST_PEAK_CONFIDENCE_F
    if local_hour >= _PEAK_START_HOUR:
        return _PEAK_WINDOW_CONFIDENCE_F
    return None


def _estimate_no_win_probability_normal(
    distance_f: float,
    confidence_interval_f: float,
) -> float:
    sigma = max(confidence_interval_f, 1.0)
    z = distance_f / sigma
    cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
    return min(cdf, 0.99)


def _slot_distance(slot: TempSlot, forecast_high_f: float) -> float:
    if slot.temp_lower_f is not None and slot.temp_upper_f is not None:
        if slot.temp_lower_f <= forecast_high_f <= slot.temp_upper_f:
            return 0.0
        return min(
            abs(forecast_high_f - slot.temp_lower_f),
            abs(forecast_high_f - slot.temp_upper_f),
        )
    if slot.temp_upper_f is None and slot.temp_lower_f is not None:
        if forecast_high_f >= slot.temp_lower_f:
            return 0.0
        return slot.temp_lower_f - forecast_high_f
    if slot.temp_lower_f is None and slot.temp_upper_f is not None:
        if forecast_high_f <= slot.temp_upper_f:
            return 0.0
        return forecast_high_f - slot.temp_upper_f
    mid = slot.temp_midpoint_f
    return abs(mid - forecast_high_f)


def _estimate_no_win_prob(
    slot: TempSlot,
    forecast: Forecast,
    error_dist: ForecastErrorDistribution | None,
) -> float:
    if error_dist is not None and error_dist._count >= 30:
        return error_dist.prob_no_wins(
            slot.temp_lower_f, slot.temp_upper_f, forecast.predicted_high_f,
        )
    distance = _slot_distance(slot, forecast.predicted_high_f)
    return _estimate_no_win_probability_normal(distance, forecast.confidence_interval_f)


def _observed_no_win_prob(
    slot: TempSlot,
    daily_max_f: float,
    confidence_f: float,
) -> float:
    distance = _slot_distance(slot, daily_max_f)
    return _estimate_no_win_probability_normal(distance, confidence_f)
