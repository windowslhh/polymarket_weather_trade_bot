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
from src.strategy.market_state import (
    MarketState,
    STATE_REJECT_REASONS,
    classify_market,
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


# Per-cycle dedup so an UNKNOWN / RESOLVED_WINNER slot doesn't append a
# decision_log REJECT row on every gate-evaluation pass within the same
# rebalance.  Keyed by ``(token_id, reason_code)``.  The set is intended
# to be cleared by the caller between cycles; if not, it bounds at the
# total number of held tokens × 4 reasons → tiny memory footprint.
_state_reject_seen: set[tuple[str, str]] = set()


def reset_state_reject_dedup() -> None:
    """Clear the per-cycle dedup set.  Call once at the top of a cycle."""
    _state_reject_seen.clear()


def _check_market_state(
    slot,
    market_states: dict[str, MarketState] | None,
    rejects: list[dict] | None,
) -> MarketState:
    """Return the slot's MarketState, appending a (deduped) decision_log
    REJECT row when the slot is non-OPEN.

    When ``market_states`` is None — the legacy path used by tests and
    older callers — every slot is treated as OPEN: no behaviour change.
    """
    if market_states is None:
        return MarketState.OPEN
    state = market_states.get(slot.token_id_no, MarketState.UNKNOWN)
    if state is MarketState.OPEN:
        return state
    reason = STATE_REJECT_REASONS[state]
    key = (slot.token_id_no, reason)
    if key not in _state_reject_seen:
        _state_reject_seen.add(key)
        if rejects is not None:
            rejects.append({
                "slot_label": slot.outcome_label,
                "token_id_no": slot.token_id_no,
                "price_no": slot.price_no,
                "reason": reason,
            })
        if state is MarketState.UNKNOWN:
            logger.warning(
                "Slot %s [%s]: market state UNKNOWN (no Gamma data) — skipping",
                slot.outcome_label, slot.token_id_no[:16] + "...",
            )
        else:
            logger.debug(
                "Slot %s: skipping (state=%s)", slot.outcome_label, state.value,
            )
    return state


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
    market_states: dict[str, MarketState] | None = None,
) -> list[TradeSignal]:
    """Thin wrapper over ``GATE_MATRIX[SignalKind.FORECAST_NO]``.

    Pre-loop setup (EV threshold, post-peak confidence) is stashed on the
    ``GateContext`` for gates to read; each gate caches derived values
    (``distance``, ``win_prob``, ``ev``) so later gates reuse them.
    """
    # FIX-22: event and forecast dates must match — Bug #1 (Houston 2026-04-17)
    # was caused by routing today's forecast into D+1/D+2 evaluators.  Every
    # evaluator enforces the invariant so a regression fails fast instead of
    # shipping wrong trades.  Uses `if … raise` (not assert) so the guard
    # still fires under `python -O`.
    if forecast.forecast_date != event.market_date:
        raise AssertionError(
            f"forecast.forecast_date={forecast.forecast_date} != "
            f"event.market_date={event.market_date} (city={event.city})"
        )

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
        # Pre-gate market lifecycle filter: a closed market can't take
        # new BUY orders, so don't bother running the entry gates on it.
        if _check_market_state(slot, market_states, rejects) is not MarketState.OPEN:
            continue
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
    market_states: dict[str, MarketState] | None = None,
) -> list[TradeSignal]:
    """Thin wrapper over ``GATE_MATRIX[SignalKind.LOCKED_WIN]``.

    ``LockedWinDetectionGate`` sets ``ctx.lock_reason`` / ``is_below_lock``
    mid-chain so the wrapper can build the signal once all gates pass.

    FIX-22 note: this evaluator takes no Forecast so there is no
    forecast_date/market_date mismatch to assert.  The equivalent
    invariant — "daily_max is for today's event only" — is enforced by
    the days_ahead early-return below and by the caller slicing
    daily_max by city-local date before it ever reaches us.  See
    rebalancer._route_forecasts for the caller contract.
    """
    if daily_max_f is None or days_ahead > 0 or not config.enable_locked_wins:
        return []

    held = frozenset(held_token_ids or ())
    signals: list[TradeSignal] = []
    for slot in event.slots:
        # Same lifecycle short-circuit as evaluate_no_signals — a closed
        # market won't accept a new locked-win BUY either.  Locked-win
        # has no decision_log REJECT sink so we just skip silently here.
        if _check_market_state(slot, market_states, rejects=None) is not MarketState.OPEN:
            continue
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
    market_states: dict[str, MarketState] | None = None,
) -> list[TradeSignal]:
    """Thin wrapper over ``GATE_MATRIX[SignalKind.TRIM]``.

    The first gate (``TrimLockedWinGuardGate``) silently filters out
    locked-win and locked-win-like slots; the remaining three gates
    each represent one OR-branch of the trim rule — any firing
    produces a SELL signal.
    """
    # FIX-22: forecast date must match event date (see evaluate_no_signals).
    # `if … raise` so the guard survives `python -O`.
    if forecast.forecast_date != event.market_date:
        raise AssertionError(
            f"forecast.forecast_date={forecast.forecast_date} != "
            f"event.market_date={event.market_date} (city={event.city})"
        )

    signals: list[TradeSignal] = []
    ep = dict(entry_prices or {})
    ev_map = dict(entry_ev_map or {})
    locked_ids = frozenset(locked_win_token_ids or ())

    gates = GATE_MATRIX[SignalKind.TRIM]
    prefilter = gates[0]
    trigger_gates = gates[1:]

    for slot in held_no_slots:
        # Lifecycle short-circuit: a held position whose market has
        # resolved (winner or loser) doesn't need a TRIM — the settler
        # owns it from here.  RESOLVING / UNKNOWN also skip so we don't
        # send a SELL into a closed book and clog the retry loop.
        if _check_market_state(slot, market_states, rejects=None) is not MarketState.OPEN:
            continue
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
        # FIX-14: the entry-side gates bake in the taker fee (see
        # gates.py::entry_fee_per_dollar); TRIM's own EV calculation did
        # not, so a held position would look slightly more attractive
        # post-entry than it did at entry — enough to keep a bleeding
        # position alive past the relative-decay gate.  Subtract the same
        # per-dollar fee so TRIM's EV is comparable to the entry_ev we
        # stored on the position row.
        ev = (
            win_prob * (1.0 - price_for_ev)
            - (1.0 - win_prob) * price_for_ev
            - entry_fee_per_dollar(price_for_ev)
        )
        ctx.win_prob = win_prob
        ctx.ev = ev

        trigger: GateResult | None = None
        for gate in trigger_gates:
            trigger = gate.check(ctx)
            if trigger is not None:
                break
        if trigger is None:
            continue
        signal = TradeSignal(
            token_type=TokenType.NO, side=Side.SELL, slot=slot, event=event,
            expected_value=ev, estimated_win_prob=win_prob,
        )
        # Embed the actual trigger in ``signal.reason`` so downstream
        # consumers (decision_log, positions.exit_reason, dashboard)
        # see which gate fired instead of a hard-coded "EV decayed"
        # string.  Rebalancer only prefixes ``[{strategy}]``.
        signal.reason = _format_trim_reason(trigger, ctx)
        signals.append(signal)
        _log_trim_trigger(trigger, ctx)

    return signals


def _format_trim_reason(trigger: GateResult, ctx: GateContext) -> str:
    """Compact, grep-friendly TRIM reason for persistence.

    Format: ``TRIM [<trigger>]: <key diagnostics>`` — e.g.
      * ``TRIM [price_stop]: 0.710→0.474 (ratio=0.25)``
      * ``TRIM [absolute]: ev=-0.150 < -0.030``
      * ``TRIM [relative]: ev=0.010 < gate=0.020 (entry_ev=0.080)``

    Uses hard-bracket access on ``trigger.extra`` so a contract break
    (gate → formatter) raises ``KeyError`` at the source line instead
    of surfacing later as ``TypeError`` from ``{None:.3f}``.
    """
    ev = ctx.ev or 0.0
    cfg = ctx.config
    extra = trigger.extra
    if trigger.code == "price_stop":
        return (
            f"TRIM [price_stop]: "
            f"{extra['entry_price']:.3f}→{extra['live_price']:.3f} "
            f"(ratio={cfg.trim_price_stop_ratio})"
        )
    if trigger.code == "absolute":
        return f"TRIM [absolute]: ev={ev:.3f} < {-cfg.min_trim_ev_absolute:.3f}"
    if trigger.code == "relative":
        return (
            f"TRIM [relative]: ev={ev:.3f} < gate={extra['gate_ev']:.3f} "
            f"(entry_ev={extra['entry_ev']:.3f})"
        )
    # Fallback for future trigger kinds.
    return f"TRIM [{trigger.code}]: ev={ev:.3f}"


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
    market_states: dict[str, MarketState] | None = None,
) -> list[TradeSignal]:
    """Three-layer hybrid exit logic for held NO positions.

    Only Layer-1 locked-win protection is delegated to the matrix
    (``SignalKind.EXIT_PREFILTER``).  Layer 2 (EV-based hold vs sell)
    and Layer 3 (pre-settlement force exit) stay inline because they
    interleave — see ``CLAUDE.md`` 'Hybrid exit' entry.
    """
    # FIX-22: when a forecast is supplied it must be for the event's date.
    # `forecast` is optional here because exits can be driven by observation
    # alone; only enforce the match when it's actually provided.  Use
    # `if … raise` so `python -O` does not strip the guard.
    if forecast is not None and forecast.forecast_date != event.market_date:
        raise AssertionError(
            f"forecast.forecast_date={forecast.forecast_date} != "
            f"event.market_date={event.market_date} (city={event.city})"
        )

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
        # Lifecycle short-circuit (same rationale as TRIM).  A SELL on a
        # closed market is rejected by Polymarket — wasted retry.  The
        # settler will pick up the position via per-market detection.
        if _check_market_state(slot, market_states, rejects=None) is not MarketState.OPEN:
            continue
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
