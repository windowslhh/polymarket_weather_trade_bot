"""Core strategy logic: evaluate temperature slots and generate trade signals.

Uses empirical forecast error distributions (from historical data) when available,
falling back to normal distribution approximation otherwise.

Post-peak optimization: after a city's peak temperature window (~17:00 local),
daily_max is essentially final. The evaluator uses it as a near-certain reference
with tight confidence, boosting NO probabilities for slots above the observed max.

M2 refactor (2026-04-20): the per-gate logic moved into
``src/strategy/gates.py``; this module is now a thin wrapper that walks
``GATE_MATRIX[kind]`` for each slot.  That change exists specifically
to prevent a repeat of Bug #1 (Houston 2026-04-17), where a new gate
was added to the NO branch but forgotten on the locked-win branch.
See ``docs/plans/m2-gate-matrix.md``.
"""
from __future__ import annotations

import logging

from src.config import StrategyConfig
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.strategy.gates import (
    GATE_MATRIX,
    GateContext,
    GateResult,
    SignalKind,
    TAKER_FEE_RATE,
    _estimate_no_win_prob as _estimate_no_win_prob_impl,
    _estimate_no_win_probability_normal as _estimate_no_win_probability_normal_impl,
    _PEAK_START_HOUR,
    _PEAK_WINDOW_CONFIDENCE_F,
    _POST_PEAK_CONFIDENCE_F,
    _POST_PEAK_HOUR,
    _slot_distance as _slot_distance_impl,
    entry_fee_per_dollar,
    post_peak_confidence,
)
from src.strategy.trend import TrendState
from src.weather.historical import ForecastErrorDistribution
from src.weather.models import Forecast, Observation

logger = logging.getLogger(__name__)

# TAKER_FEE_RATE + entry_fee_per_dollar are now canonically defined in
# src/strategy/gates.py and imported above.  Private aliases preserved
# here for test modules that still import the pre-M2 underscored name.
_entry_fee_per_dollar = entry_fee_per_dollar


# Re-export distance / probability helpers so existing tests keep working.
_slot_distance = _slot_distance_impl
_estimate_no_win_probability_normal = _estimate_no_win_probability_normal_impl
_estimate_no_win_prob = _estimate_no_win_prob_impl
_post_peak_confidence = post_peak_confidence


def _log_entry_rejection(
    kind: SignalKind,
    reject: GateResult,
    ctx: GateContext,
) -> None:
    """Per-branch logger for gate rejections that require more than a
    decision_log entry (e.g. PRICE_DIVERGENCE needs a WARN; LOCKED_WIN
    price-cap / ev-non-positive need DEBUG lines with slot context)."""
    slot = ctx.slot
    event = ctx.event
    if reject.code == "PRICE_DIVERGENCE":
        label = "LOCKED" if kind is SignalKind.LOCKED_WIN else "NO"
        logger.warning(
            "PRICE DIVERGENCE [%s]: %s slot %s — model=%.1f%% vs market=%.1f%% "
            "(gap=%.2f > %.2f), skipping",
            label, event.city, slot.outcome_label,
            (ctx.win_prob or 0) * 100, slot.price_no * 100,
            reject.extra.get("gap", 0.0), reject.extra.get("threshold", 0.0),
        )
    elif reject.code == "LOCKED_WIN_PRICE_CAP":
        logger.debug(
            "LOCKED WIN skip (price %.4f > locked_win_max_price %.2f): %s slot %s — "
            "margin too thin for live execution",
            slot.price_no, ctx.config.locked_win_max_price, event.city, slot.outcome_label,
        )
    elif reject.code == "LOCKED_WIN_EV_NONPOSITIVE":
        logger.debug(
            "LOCKED WIN skip (ev=%.5f, price=%.4f, win_prob=%.3f): %s slot %s — "
            "fee/odds wipe out positive EV",
            ctx.ev or 0.0, slot.price_no, ctx.win_prob or 0.0,
            event.city, slot.outcome_label,
        )


def _append_reject(slot: TempSlot, reject: GateResult, sink: list[dict] | None) -> None:
    if sink is None or reject.silent:
        return
    entry = {
        "slot_label": slot.outcome_label,
        "token_id_no": slot.token_id_no,
        "price_no": slot.price_no,
        "reason": reject.code,
    }
    entry.update(reject.extra)
    sink.append(entry)


def _run_gate_chain(kind: SignalKind, ctx: GateContext) -> GateResult | None:
    for gate in GATE_MATRIX[kind]:
        result = gate.check(ctx)
        if result is not None:
            return result
    return None


def evaluate_no_signals(
    event: WeatherMarketEvent,
    forecast: Forecast,
    config: StrategyConfig,
    error_dist: ForecastErrorDistribution | None = None,
    trend: TrendState | None = None,
    held_token_ids: set[str] | None = None,
    days_ahead: int = 0,
    daily_max_f: float | None = None,
    local_hour: int | None = None,
    hours_to_settlement: float | None = None,
    rejects: list[dict] | None = None,
) -> list[TradeSignal]:
    """Thin wrapper over ``GATE_MATRIX[SignalKind.FORECAST_NO]``.

    Pre-loop setup (EV threshold, post-peak confidence) is stashed on the
    ``GateContext`` for gates to read; each gate caches derived values
    (``distance``, ``win_prob``, ``ev``) so later gates reuse them.
    """
    if hours_to_settlement is not None and hours_to_settlement < config.force_exit_hours:
        logger.debug("Blocking new NO entries for %s: %.1fh to settlement (< %.1fh gate)",
                     event.city, hours_to_settlement, config.force_exit_hours)
        return []

    ev_threshold = config.min_no_ev * (1.5 if trend == TrendState.SETTLING else 1.0)
    if days_ahead > 0:
        ev_threshold /= (config.day_ahead_ev_discount ** days_ahead)
    peak_conf = (post_peak_confidence(local_hour)
                 if days_ahead == 0 and daily_max_f is not None and local_hour is not None
                 else None)

    signals: list[TradeSignal] = []
    held = frozenset(held_token_ids or ())
    for slot in event.slots:
        ctx = GateContext(
            slot=slot, event=event, config=config, forecast=forecast, error_dist=error_dist,
            daily_max_f=daily_max_f, local_hour=local_hour, hours_to_settlement=hours_to_settlement,
            days_ahead=days_ahead, trend=trend, held_token_ids=held,
            peak_conf=peak_conf, ev_threshold=ev_threshold,
        )
        reject = _run_gate_chain(SignalKind.FORECAST_NO, ctx)
        if reject is not None:
            _log_entry_rejection(SignalKind.FORECAST_NO, reject, ctx)
            _append_reject(slot, reject, rejects)
            continue
        signals.append(TradeSignal(
            token_type=TokenType.NO, side=Side.BUY, slot=slot, event=event,
            expected_value=ctx.ev, estimated_win_prob=ctx.win_prob,
        ))

    _log_no_summary(event, signals, error_dist, trend, peak_conf, local_hour)
    return signals


def _log_no_summary(event, signals, error_dist, trend, peak_conf, local_hour):
    using = "empirical" if (error_dist and error_dist._count >= 30) else "normal"
    trend_label = trend.value if trend else "none"
    peak_label = f", post-peak(h={local_hour})" if peak_conf else ""
    logger.debug("City %s date %s: %d NO signals from %d slots (prob: %s, trend: %s%s)",
                 event.city, event.market_date, len(signals), len(event.slots),
                 using, trend_label, peak_label)


def evaluate_locked_win_signals(
    event: WeatherMarketEvent,
    daily_max_f: float | None,
    config: StrategyConfig,
    held_token_ids: set[str] | None = None,
    days_ahead: int = 0,
    *,
    daily_max_final: bool = False,
) -> list[TradeSignal]:
    """Thin wrapper over ``GATE_MATRIX[SignalKind.LOCKED_WIN]``.

    ``LockedWinDetectionGate`` sets ``ctx.lock_reason`` / ``is_below_lock``
    mid-chain so the wrapper can build the signal once all gates pass.
    """
    if daily_max_f is None or days_ahead > 0 or not config.enable_locked_wins:
        return []

    held = frozenset(held_token_ids or ())
    signals: list[TradeSignal] = []
    for slot in event.slots:
        ctx = GateContext(
            slot=slot, event=event, config=config, daily_max_f=daily_max_f,
            daily_max_final=daily_max_final, days_ahead=days_ahead, held_token_ids=held,
        )
        reject = _run_gate_chain(SignalKind.LOCKED_WIN, ctx)
        if reject is not None:
            _log_entry_rejection(SignalKind.LOCKED_WIN, reject, ctx)
            continue
        signals.append(TradeSignal(
            token_type=TokenType.NO, side=Side.BUY, slot=slot, event=event,
            expected_value=ctx.ev, estimated_win_prob=ctx.win_prob,
            is_locked_win=True, reason=ctx.lock_reason,
        ))
        logger.info("%s: %s slot %s, EV=%.4f", ctx.lock_reason, event.city, slot.outcome_label, ctx.ev)
    return signals


def evaluate_trim_signals(
    event: WeatherMarketEvent,
    forecast: Forecast,
    held_no_slots: list[TempSlot],
    config: StrategyConfig,
    error_dist: ForecastErrorDistribution | None = None,
    entry_prices: dict[str, float] | None = None,
    locked_win_token_ids: set[str] | None = None,
    daily_max_f: float | None = None,
    entry_ev_map: dict[str, float] | None = None,
) -> list[TradeSignal]:
    """Thin wrapper over ``GATE_MATRIX[SignalKind.TRIM]``.

    The first gate (``TrimLockedWinGuardGate``) silently filters out
    locked-win and locked-win-like slots; the remaining three gates
    each represent one OR-branch of the trim rule — any firing
    produces a SELL signal.
    """
    signals: list[TradeSignal] = []
    ep = dict(entry_prices or {})
    ev_map = dict(entry_ev_map or {})
    locked_ids = frozenset(locked_win_token_ids or ())

    gates = GATE_MATRIX[SignalKind.TRIM]
    prefilter = gates[0]
    trigger_gates = gates[1:]

    for slot in held_no_slots:
        ctx = GateContext(
            slot=slot, event=event, config=config,
            forecast=forecast, error_dist=error_dist, daily_max_f=daily_max_f,
            locked_win_token_ids=locked_ids, entry_prices=ep, entry_ev_map=ev_map,
        )

        prefilter_result = prefilter.check(ctx)
        if prefilter_result is not None:
            _log_trim_prefilter(prefilter_result, ctx)
            continue

        win_prob = _estimate_no_win_prob(slot, forecast, error_dist)
        price_for_ev = ep.get(slot.token_id_no, slot.price_no)
        ev = win_prob * (1.0 - price_for_ev) - (1.0 - win_prob) * price_for_ev
        ctx.win_prob = win_prob
        ctx.ev = ev

        trigger: GateResult | None = None
        for gate in trigger_gates:
            trigger = gate.check(ctx)
            if trigger is not None:
                break
        if trigger is None:
            continue
        signals.append(TradeSignal(
            token_type=TokenType.NO, side=Side.SELL, slot=slot, event=event,
            expected_value=ev, estimated_win_prob=win_prob,
        ))
        _log_trim_trigger(trigger, ctx)

    return signals


def _log_trim_prefilter(result: GateResult, ctx: GateContext) -> None:
    slot = ctx.slot
    if result.code == "TRIM_SKIP_LOCKED":
        logger.debug("TRIM skip (locked win): %s slot %s", ctx.event.city, slot.outcome_label)
    elif result.code == "TRIM_SKIP_LOCKED_LIKE":
        logger.debug(
            "TRIM skip (daily_max %.1f > upper %.1f + margin %d): %s slot %s",
            ctx.daily_max_f, slot.temp_upper_f, ctx.config.locked_win_margin_f,
            ctx.event.city, slot.outcome_label,
        )


def _log_trim_trigger(trigger: GateResult, ctx: GateContext) -> None:
    slot = ctx.slot
    cfg = ctx.config
    entry_ev = ctx.entry_ev_map.get(slot.token_id_no)
    entry_price_no = ctx.entry_prices.get(slot.token_id_no)
    rel_gate_ev = (entry_ev * (1.0 - cfg.trim_ev_decay_ratio)
                   if entry_ev is not None and entry_ev > 0 else None)
    price_stop_threshold = (
        entry_price_no * (1.0 - cfg.trim_price_stop_ratio)
        if entry_price_no is not None and entry_price_no > 0
        and 0 < cfg.trim_price_stop_ratio < 1.0 else None
    )
    logger.info(
        "TRIM signal [%s]: %s slot %s EV=%.4f (entry_ev=%s, rel_gate=%s, abs_gate=%.4f, "
        "entry_price=%s, price_stop=%s, live_price=%.3f, win_prob=%.2f)",
        trigger.code, ctx.event.city, slot.outcome_label, ctx.ev,
        f"{entry_ev:.4f}" if entry_ev is not None else "None",
        f"{rel_gate_ev:.4f}" if rel_gate_ev is not None else "n/a",
        -cfg.min_trim_ev_absolute,
        f"{entry_price_no:.3f}" if entry_price_no is not None else "None",
        f"{price_stop_threshold:.3f}" if price_stop_threshold is not None else "n/a",
        slot.price_no, ctx.win_prob,
    )


def evaluate_exit_signals(
    event: WeatherMarketEvent,
    observation: Observation | None,
    daily_max_f: float | None,
    held_no_slots: list[TempSlot],
    config: StrategyConfig,
    trend: TrendState | None = None,
    days_ahead: int = 0,
    forecast: Forecast | None = None,
    error_dist: ForecastErrorDistribution | None = None,
    hours_to_settlement: float | None = None,
    local_hour: int | None = None,
) -> list[TradeSignal]:
    """Three-layer hybrid exit logic for held NO positions.

    Only Layer-1 locked-win protection is delegated to the matrix
    (``SignalKind.EXIT_PREFILTER``).  Layer 2 (EV-based hold vs sell)
    and Layer 3 (pre-settlement force exit) stay inline because they
    interleave — see ``CLAUDE.md`` 'Hybrid exit' entry.
    """
    if observation is None or daily_max_f is None or days_ahead > 0:
        return []

    exit_distance = config.no_distance_threshold_f * 0.25
    if trend == TrendState.STABLE:
        exit_distance = config.no_distance_threshold_f * 0.3
    elif trend in (TrendState.BREAKOUT_UP, TrendState.BREAKOUT_DOWN):
        exit_distance = config.no_distance_threshold_f * 0.2

    peak_conf = post_peak_confidence(local_hour) if local_hour is not None else None

    signals: list[TradeSignal] = []
    for slot in held_no_slots:
        ctx = GateContext(
            slot=slot, event=event, config=config, forecast=forecast, error_dist=error_dist,
            daily_max_f=daily_max_f, local_hour=local_hour,
            hours_to_settlement=hours_to_settlement, days_ahead=days_ahead,
            trend=trend, peak_conf=peak_conf,
        )
        prefilter = _run_gate_chain(SignalKind.EXIT_PREFILTER, ctx)
        if prefilter is not None:
            if prefilter.code == "EXIT_SKIP_LOCKED":
                logger.debug(
                    "EXIT skip (locked win): %s slot %s — daily max %.1f > upper %.1f + margin %d",
                    event.city, slot.outcome_label, daily_max_f, slot.temp_upper_f,
                    config.locked_win_margin_f,
                )
            continue

        distance = _slot_distance(slot, daily_max_f)
        if distance >= exit_distance:
            continue
        if forecast is None:
            logger.debug("EXIT skip (no forecast): %s slot %s — cannot evaluate EV, holding",
                         event.city, slot.outcome_label)
            continue

        # Layer 2: EV-based hold vs sell.
        wp_forecast = _estimate_no_win_prob(slot, forecast, error_dist)
        dist_to_max = _slot_distance(slot, daily_max_f)
        max_confidence = peak_conf if peak_conf is not None else forecast.confidence_interval_f
        wp_from_max = _estimate_no_win_probability_normal(dist_to_max, max_confidence)
        win_prob = (max(wp_forecast, wp_from_max) if peak_conf is not None
                    else min(wp_forecast, wp_from_max))
        if peak_conf is not None:
            logger.debug(
                "EXIT post-peak (h=%s): %s slot %s — wp_forecast=%.3f, wp_observed=%.3f → %.3f",
                local_hour, event.city, slot.outcome_label,
                wp_forecast, wp_from_max, win_prob,
            )
        ev = win_prob * (1.0 - slot.price_no) - (1.0 - win_prob) * slot.price_no

        if ev >= 0:
            # Layer 3: force exit window
            if (hours_to_settlement is not None
                    and 0 <= hours_to_settlement <= config.force_exit_hours
                    and distance < exit_distance):
                signals.append(TradeSignal(
                    token_type=TokenType.NO, side=Side.SELL, slot=slot, event=event,
                    expected_value=ev, estimated_win_prob=win_prob,
                ))
                logger.info("FORCE EXIT: %s slot %s — %.1fh to settlement, distance %.1f°F, EV=%.4f",
                            event.city, slot.outcome_label, hours_to_settlement, distance, ev)
            else:
                logger.debug("EXIT hold (positive EV): %s slot %s — distance %.1f, EV=%.4f",
                             event.city, slot.outcome_label, distance, ev)
            continue

        signals.append(TradeSignal(
            token_type=TokenType.NO, side=Side.SELL, slot=slot, event=event,
            expected_value=ev, estimated_win_prob=win_prob,
        ))
        logger.info("EXIT signal: %s slot %s — daily max %.1f°F, distance %.1f°F, EV=%.4f",
                    event.city, slot.outcome_label, daily_max_f, distance, ev)

    return signals
