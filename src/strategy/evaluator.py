"""Core strategy logic: evaluate temperature slots and generate trade signals.

Uses empirical forecast error distributions (from historical data) when available,
falling back to normal distribution approximation otherwise.

Post-peak optimization: after a city's peak temperature window (~17:00 local),
daily_max is essentially final. The evaluator uses it as a near-certain reference
with tight confidence, boosting NO probabilities for slots above the observed max.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from src.config import StrategyConfig
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.weather.historical import ForecastErrorDistribution
from src.strategy.temperature import is_daily_max_final, wu_round
from src.strategy.trend import TrendState
from src.weather.models import Forecast, Observation

logger = logging.getLogger(__name__)

# Polymarket taker fee for the Weather category (as of 2026).
# Weather markets charge 1.25% base rate, probability-weighted so the fee is
# highest at 50/50 and decreases toward 0 or 1.  Matches the backtest engine.
# Formula: fee_per_dollar = TAKER_FEE_RATE * 2 * price * (1 - price)
# (peaks at price=0.50: 0.625% per dollar; at price=0.70: 0.525% per dollar)
# Makers pay 0%; we assume all our orders execute as taker (aggressive limits).
TAKER_FEE_RATE: float = 0.0125  # 1.25%

# Hard ceiling on NO price for locked-win entries.  Above this, the implied
# margin (1 - price) is so thin that paper→live slippage (typically ≥1 tick =
# 0.001) and any incremental fee swing wipe out the entire EV.  Empirically
# (2026-04-17 production cycle) every locked-win signal Fix 2 produced was
# stuck in 0.997-0.9985 with EV ≈ 0.0008 — technically positive but
# unrecoverable in live execution.  Acts as a *hard* gate alongside the
# `ev > 0` safety net (two filters; either rejection blocks the entry).
# See docs/fixes/2026-04-17-lockedwin-price-cap-rollback.md.
LOCKED_WIN_MAX_PRICE: float = 0.95


def _entry_fee_per_dollar(price: float) -> float:
    """Compute Polymarket taker fee per dollar invested at *price*.

    Probability-weighted formula: fee is highest at 50/50 and falls toward
    price extremes.  Only applied on entry — settlement is automatic (no exit
    fee when the position resolves to $1).  For early exits (SELL orders) the
    same formula applies but is not captured here since the exit decision is
    whether to *hold* vs sell (hold EV does not incur an additional fee).
    """
    return TAKER_FEE_RATE * 2.0 * price * (1.0 - price)


# Post-peak confidence intervals: how much the daily max can still
# rise after a given local hour.  After 17:00, ±1.5°F; during peak
# (14-17), ±3°F.  Before 14:00, no adjustment (forecast only).
_POST_PEAK_CONFIDENCE_F = 1.5
_PEAK_WINDOW_CONFIDENCE_F = 3.0
_PEAK_START_HOUR = 14
_POST_PEAK_HOUR = 17


def _estimate_no_win_probability_normal(
    distance_f: float,
    confidence_interval_f: float,
) -> float:
    """Fallback: estimate NO win probability using normal distribution.

    Only used when no empirical distribution is available.
    """
    sigma = max(confidence_interval_f, 1.0)
    z = distance_f / sigma
    cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
    return min(cdf, 0.99)


def _slot_distance(slot: TempSlot, forecast_high_f: float) -> float:
    """Calculate the minimum distance from the slot to the forecast high.

    For open-ended slots:
    - "≥X°F" (upper=None): NO wins when actual < X. Distance = how far forecast
      is below X. Returns 0 when forecast >= X (YES likely wins, no NO edge).
    - "below X°F" (lower=None): NO wins when actual >= X. Distance = how far
      forecast is above X. Returns 0 when forecast <= X (YES likely wins).
    """
    if slot.temp_lower_f is not None and slot.temp_upper_f is not None:
        if slot.temp_lower_f <= forecast_high_f <= slot.temp_upper_f:
            return 0.0
        return min(abs(forecast_high_f - slot.temp_lower_f), abs(forecast_high_f - slot.temp_upper_f))
    if slot.temp_upper_f is None and slot.temp_lower_f is not None:
        # "≥X°F" slot: YES wins when actual >= X → NO wins when actual < X
        if forecast_high_f >= slot.temp_lower_f:
            return 0.0  # forecast at/above threshold → YES likely wins, no NO edge
        return slot.temp_lower_f - forecast_high_f
    if slot.temp_lower_f is None and slot.temp_upper_f is not None:
        # "below X°F" slot: YES wins when actual < X → NO wins when actual >= X
        if forecast_high_f <= slot.temp_upper_f:
            return 0.0  # forecast at/below threshold → YES likely wins, no NO edge
        return forecast_high_f - slot.temp_upper_f
    mid = slot.temp_midpoint_f
    return abs(mid - forecast_high_f)


def _estimate_no_win_prob(
    slot: TempSlot,
    forecast: Forecast,
    error_dist: ForecastErrorDistribution | None,
) -> float:
    """Estimate NO win probability using empirical distribution if available."""
    if error_dist is not None and error_dist._count >= 30:
        return error_dist.prob_no_wins(
            slot.temp_lower_f, slot.temp_upper_f, forecast.predicted_high_f,
        )
    # Fallback to normal approximation
    distance = _slot_distance(slot, forecast.predicted_high_f)
    return _estimate_no_win_probability_normal(distance, forecast.confidence_interval_f)


def _post_peak_confidence(local_hour: int) -> float | None:
    """Return the confidence interval to use for post-peak adjustment.

    Returns None if before peak window (no adjustment needed).
    """
    if local_hour >= _POST_PEAK_HOUR:
        return _POST_PEAK_CONFIDENCE_F
    if local_hour >= _PEAK_START_HOUR:
        return _PEAK_WINDOW_CONFIDENCE_F
    return None


def _observed_no_win_prob(
    slot: TempSlot,
    daily_max_f: float,
    confidence_f: float,
) -> float:
    """Estimate NO win probability using observed daily_max as reference.

    Used post-peak when daily_max is essentially final.
    """
    distance = _slot_distance(slot, daily_max_f)
    return _estimate_no_win_probability_normal(distance, confidence_f)


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
    """Phase 4: Generate BUY NO signals for slots far from forecast.

    When an empirical error distribution is provided, uses it for accurate
    probability estimation. Otherwise falls back to normal approximation.

    Post-peak boost (local_hour >= 14, same-day only):
    When daily_max is available and the peak temperature window has passed,
    use observed daily_max as a near-final reference to boost NO probability
    for slots above the observed max.

    Trend state adjusts behavior:
    - SETTLING: tighter EV threshold (only high-confidence trades)
    - BREAKOUT_UP/DOWN: boost signals on the opposite side of the breakout

    If ``rejects`` is provided, every slot that fails a filter gate appends
    a dict describing the rejection reason (used by the rebalancer to
    write sampled REJECT entries to decision_log for observability).  See
    docs/fixes/2026-04-16-strategy-p0-fixes.md#fix-3.
    """
    signals: list[TradeSignal] = []

    def _reject(slot: TempSlot, reason: str, **extra) -> None:
        if rejects is None:
            return
        entry = {
            "slot_label": slot.outcome_label,
            "token_id_no": slot.token_id_no,
            "price_no": slot.price_no,
            "reason": reason,
        }
        entry.update(extra)
        rejects.append(entry)

    # Block new entries when market is close to settlement
    if hours_to_settlement is not None and hours_to_settlement < config.force_exit_hours:
        logger.debug(
            "Blocking new NO entries for %s: %.1fh to settlement (< %.1fh gate)",
            event.city, hours_to_settlement, config.force_exit_hours,
        )
        return signals

    # Adjust EV threshold based on trend and days ahead
    ev_threshold = config.min_no_ev
    if trend == TrendState.SETTLING:
        ev_threshold = config.min_no_ev * 1.5
    # Require higher EV for future markets (forecast less reliable)
    if days_ahead > 0:
        ev_threshold /= (config.day_ahead_ev_discount ** days_ahead)

    # Post-peak: determine if we can use daily_max as near-final reference
    peak_conf = None
    if days_ahead == 0 and daily_max_f is not None and local_hour is not None:
        peak_conf = _post_peak_confidence(local_hour)

    for slot in event.slots:
        # Skip already-held slots (not a rejection — silent)
        if held_token_ids and slot.token_id_no in held_token_ids:
            continue

        # For "≥X°F" slots (upper=None): when wu_round(daily_max) >= X, YES is a
        # guaranteed winner → NO is a guaranteed loser. Block immediately.
        # (evaluate_locked_win_signals already skips these; mirror the guard here.)
        if (
            days_ahead == 0
            and daily_max_f is not None
            and slot.temp_upper_f is None
            and slot.temp_lower_f is not None
            and wu_round(daily_max_f) >= int(slot.temp_lower_f)
        ):
            _reject(slot, "DAILY_MAX_ABOVE_LOWER", daily_max_f=daily_max_f)
            continue

        # For range slots [L, U]: when wu_round(daily_max) is inside the range,
        # the actual high has entered the slot → YES is almost certainly winning
        # → NO is almost certainly a loser. Block immediately.
        if (
            days_ahead == 0
            and daily_max_f is not None
            and slot.temp_lower_f is not None
            and slot.temp_upper_f is not None
            and int(slot.temp_lower_f) <= wu_round(daily_max_f) <= int(slot.temp_upper_f)
        ):
            _reject(slot, "DAILY_MAX_IN_SLOT", daily_max_f=daily_max_f)
            continue

        # For "below X°F" slots (lower=None, upper=X): post-peak, if
        # wu_round(daily_max) < X then the daily max is still inside this
        # slot's range (-∞, X) → YES is likely winning → NO is a loser.
        # Only checked post-peak because earlier in the day the temperature
        # may still rise above X, making NO viable.
        if (
            peak_conf is not None
            and daily_max_f is not None
            and slot.temp_lower_f is None
            and slot.temp_upper_f is not None
            and wu_round(daily_max_f) < int(slot.temp_upper_f)
        ):
            _reject(slot, "DAILY_MAX_BELOW_UPPER", daily_max_f=daily_max_f)
            continue

        # Bias-corrected reference temperature for the distance pre-filter.
        # When the empirical distribution shows a systematic bias (e.g. forecasts
        # run +2°F hot), the expected actual temperature is lower than the raw
        # forecast.  Using the bias-corrected value prevents the distance filter
        # from accepting lower slots that are actually close to the expected actual
        # (false pass) and from rejecting upper slots that are actually far from
        # the expected actual (false block).
        #
        # The probability calculation (_estimate_no_win_prob) is NOT adjusted
        # here — it uses the full empirical error distribution via prob_no_wins(),
        # which already implicitly captures the bias.
        bias_corrected_f = forecast.predicted_high_f
        if error_dist is not None and error_dist._count >= 30:
            bias_corrected_f = forecast.predicted_high_f - error_dist.mean

        distance = _slot_distance(slot, bias_corrected_f)

        # Post-peak: also compute distance from observed daily_max and take the
        # smaller value (conservative).  When forecast is stale/wrong but the
        # actual temperature is near the slot, obs_distance catches it.
        # Skip when daily_max already exceeds slot upper bound — NO is safe
        # (temp can't fall back), so obs_distance would be misleadingly small.
        if peak_conf is not None and daily_max_f is not None:
            if slot.temp_upper_f is None or daily_max_f <= slot.temp_upper_f:
                obs_distance = _slot_distance(slot, daily_max_f)
                distance = min(distance, obs_distance)

        if distance < config.no_distance_threshold_f:
            _reject(slot, "DIST_TOO_CLOSE", distance_f=distance,
                    threshold_f=config.no_distance_threshold_f)
            continue

        if slot.price_no <= 0 or slot.price_no >= 1:
            _reject(slot, "PRICE_INVALID")
            continue

        # Skip extremely cheap NO — poor liquidity and inflated odds
        if slot.price_no < config.min_no_price:
            _reject(slot, "PRICE_TOO_LOW", min_no_price=config.min_no_price)
            continue

        # Skip overpriced NO — risk/reward too asymmetric at high prices
        if slot.price_no > config.max_no_price:
            _reject(slot, "PRICE_TOO_HIGH", max_no_price=config.max_no_price)
            continue

        win_prob = _estimate_no_win_prob(slot, forecast, error_dist)

        # Post-peak boost: use observed daily_max with tight confidence
        # Take the more favorable probability (forecast vs observed)
        if peak_conf is not None:
            obs_prob = _observed_no_win_prob(slot, daily_max_f, peak_conf)
            if obs_prob > win_prob:
                logger.debug(
                    "Post-peak boost %s slot %s: %.3f → %.3f (hour=%d, max=%.1f)",
                    event.city, slot.outcome_label, win_prob, obs_prob,
                    local_hour, daily_max_f,
                )
                win_prob = obs_prob

        # Trend-based probability boost for breakout direction
        if trend == TrendState.BREAKOUT_UP and slot.temp_lower_f is not None:
            # Forecast rising → lower slots become safer NO bets
            if slot.temp_upper_f is not None and slot.temp_upper_f < forecast.predicted_high_f:
                win_prob = min(win_prob * 1.05, 0.99)
        elif trend == TrendState.BREAKOUT_DOWN and slot.temp_upper_f is not None:
            # Forecast falling → upper slots become safer NO bets
            if slot.temp_lower_f is not None and slot.temp_lower_f > forecast.predicted_high_f:
                win_prob = min(win_prob * 1.05, 0.99)

        # EV = win_prob * profit_if_win - (1 - win_prob) * cost_if_lose - entry_fee
        # Entry taker fee is probability-weighted; deducted only once at trade entry.
        ev = (win_prob * (1.0 - slot.price_no)
              - (1.0 - win_prob) * slot.price_no
              - _entry_fee_per_dollar(slot.price_no))

        if ev < ev_threshold:
            _reject(slot, "EV_BELOW_GATE",
                    expected_value=ev, win_prob=win_prob,
                    ev_threshold=ev_threshold)
            continue

        # Price divergence guard: when model and market disagree by >50pp,
        # the model input (forecast) is likely stale or wrong.
        market_implied_no_win = slot.price_no
        if abs(win_prob - market_implied_no_win) > 0.50:
            logger.warning(
                "PRICE DIVERGENCE: %s slot %s — model=%.1f%% vs market=%.1f%%, skipping",
                event.city, slot.outcome_label, win_prob * 100, market_implied_no_win * 100,
            )
            _reject(slot, "PRICE_DIVERGENCE",
                    win_prob=win_prob, market_implied=market_implied_no_win)
            continue

        signals.append(TradeSignal(
            token_type=TokenType.NO,
            side=Side.BUY,
            slot=slot,
            event=event,
            expected_value=ev,
            estimated_win_prob=win_prob,
        ))

    using = "empirical" if (error_dist and error_dist._count >= 30) else "normal"
    trend_label = trend.value if trend else "none"
    peak_label = f", post-peak(h={local_hour})" if peak_conf else ""
    logger.debug(
        "City %s date %s: %d NO signals from %d slots (prob: %s, trend: %s%s)",
        event.city, event.market_date, len(signals), len(event.slots),
        using, trend_label, peak_label,
    )
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
    """Generate SELL signals for held NO positions whose EV has decayed.

    Unlike exit signals (which trigger on temperature proximity), trim signals
    fire when the expected value drops due to forecast changes.  A slot is
    trimmed when EITHER:
      - Relative gate: current EV < entry_ev × (1 - trim_ev_decay_ratio), OR
      - Absolute gate: current EV < -min_trim_ev_absolute

    The relative gate protects high-EV entries from being trimmed on small
    noise (e.g. entry_ev=+0.08 → only trim once EV drops below +0.02 at
    ratio=0.75).  The absolute gate catches hard reversals regardless of
    how rich the original entry was.  See docs/fixes/2026-04-16-strategy-p0-fixes.md#fix-4.

    NEVER trims locked-win positions — these are guaranteed winners where the
    forecast-based EV is misleading (daily_max already exceeded slot upper).
    """
    signals: list[TradeSignal] = []
    locked_ids = locked_win_token_ids or set()
    ep = entry_prices or {}
    ev_map = entry_ev_map or {}

    for slot in held_no_slots:
        # NEVER trim locked wins — daily_max already exceeded slot upper,
        # NO is guaranteed to win.  Forecast-based EV is misleading here
        # because it doesn't account for the observed daily maximum.
        if slot.token_id_no in locked_ids:
            logger.debug(
                "TRIM skip (locked win): %s slot %s",
                event.city, slot.outcome_label,
            )
            continue

        # Also protect slots where wu_round(daily_max) currently exceeds upper bound
        # by at least locked_win_margin_f (locked-win condition is true NOW,
        # even if not bought as locked win).  Must match the same margin
        # threshold as locked_win_signals to avoid a dead zone where the
        # position is neither locked-win-protected nor trimmable.
        if daily_max_f is not None and slot.temp_upper_f is not None:
            gap = wu_round(daily_max_f) - int(slot.temp_upper_f)
            if gap >= config.locked_win_margin_f:
                logger.debug(
                    "TRIM skip (daily_max %.1f > upper %.1f + margin %d): %s slot %s",
                    daily_max_f, slot.temp_upper_f, config.locked_win_margin_f,
                    event.city, slot.outcome_label,
                )
                continue

        win_prob = _estimate_no_win_prob(slot, forecast, error_dist)

        # Use ENTRY price for EV calculation, not current market price.
        # When the market moves in our favor (NO price rises because market
        # agrees NO will win), current-price EV goes negative even though
        # holding to settlement is still highly profitable at our entry cost.
        # Entry-price EV answers: "is this position still a good hold?"
        price_for_ev = ep.get(slot.token_id_no, slot.price_no)
        ev = win_prob * (1.0 - price_for_ev) - (1.0 - win_prob) * price_for_ev

        # Trim when either gate fires (see fix 4 rationale in module docstring):
        #   1. Relative: current EV < entry_ev × (1 - trim_ev_decay_ratio).
        #      Only active when we have a positive entry_ev (legacy positions
        #      recorded before the migration fall back to the absolute gate).
        #   2. Absolute: current EV < -min_trim_ev_absolute (hard reversal).
        entry_ev = ev_map.get(slot.token_id_no)
        absolute_triggered = ev < -config.min_trim_ev_absolute
        relative_triggered = False
        relative_gate_ev: float | None = None
        if entry_ev is not None and entry_ev > 0:
            relative_gate_ev = entry_ev * (1.0 - config.trim_ev_decay_ratio)
            relative_triggered = ev < relative_gate_ev

        if absolute_triggered or relative_triggered:
            signals.append(TradeSignal(
                token_type=TokenType.NO,
                side=Side.SELL,
                slot=slot,
                event=event,
                expected_value=ev,
                estimated_win_prob=win_prob,
            ))
            trigger = "absolute" if absolute_triggered else "relative"
            logger.info(
                "TRIM signal [%s]: %s slot %s EV=%.4f (entry_ev=%s, rel_gate=%s, abs_gate=%.4f, "
                "win_prob=%.2f, entry_price=%.3f)",
                trigger, event.city, slot.outcome_label, ev,
                f"{entry_ev:.4f}" if entry_ev is not None else "None",
                f"{relative_gate_ev:.4f}" if relative_gate_ev is not None else "n/a",
                -config.min_trim_ev_absolute, win_prob, price_for_ev,
            )

    return signals


def evaluate_locked_win_signals(
    event: WeatherMarketEvent,
    daily_max_f: float | None,
    config: StrategyConfig,
    held_token_ids: set[str] | None = None,
    days_ahead: int = 0,
    *,
    daily_max_final: bool = False,
) -> list[TradeSignal]:
    """Generate BUY NO signals for slots where NO is guaranteed to win.

    Two asymmetric conditions with different time requirements:

    Condition A (below-slot): wu_round(daily_max) > slot.upper + margin
        The actual high already exceeded this range → NO wins.
        No time gate needed: daily_max is monotonically increasing by definition,
        so once it exceeds the upper bound it can never fall back below it.

    Condition B (above-slot): wu_round(daily_max) < slot.lower - margin
        The daily max is finalized below this range → NO wins.
        Requires daily_max_final=True: the afternoon peak (14:00–17:00) could
        still push the observed max up into or above the slot's lower bound.

    Rules:
    - Only same-day markets (days_ahead == 0)
    - daily_max_f must exist
    - Condition A fires any time of day; Condition B requires daily_max_final=True
    - "≥X°F" slot (upper=None): if daily_max >= X then YES wins → NO loses → SKIP
    - Skip already-held tokens
    - Skip if price_no <= 0 or >= 1
    - Safety margin: wu_round(daily_max) must differ from slot boundary by
      at least config.locked_win_margin_f degrees
    """
    if daily_max_f is None or days_ahead > 0:
        return []

    if not config.enable_locked_wins:
        return []

    rounded_max = wu_round(daily_max_f)
    margin = config.locked_win_margin_f

    signals: list[TradeSignal] = []
    for slot in event.slots:
        # Skip already held
        if held_token_ids and slot.token_id_no in held_token_ids:
            continue

        if slot.price_no <= 0 or slot.price_no >= 1:
            continue

        if slot.price_no < config.min_no_price:
            continue

        is_locked = False
        lock_reason = ""
        # Below-slot locks (daily max already above upper) are bounded
        # certainties — temperature cannot fall.  Above-slot locks rely
        # on daily_max being final, which carries residual uncertainty
        # (late-afternoon spike).  Track which kind we have so we can
        # pick a tighter win_prob for below-slot locks.
        is_below_lock = False

        if slot.temp_upper_f is not None and slot.temp_lower_f is not None:
            # Range slot [L, U]
            upper_int = int(slot.temp_upper_f)
            lower_int = int(slot.temp_lower_f)
            # Condition A: daily max exceeded this range (below-slot lock) — no time gate
            if rounded_max > upper_int and (rounded_max - upper_int) >= margin:
                is_locked = True
                is_below_lock = True
                lock_reason = (
                    f"LOCKED WIN (below): wu_round({daily_max_f:.1f})={rounded_max} "
                    f"> upper {upper_int} + margin {margin}"
                )
            # Condition B: daily max finalized below this range (above-slot lock) — needs final
            elif (daily_max_final
                  and rounded_max < lower_int
                  and (lower_int - rounded_max) >= margin):
                is_locked = True
                lock_reason = (
                    f"LOCKED WIN (above): wu_round({daily_max_f:.1f})={rounded_max} "
                    f"< lower {lower_int} - margin {margin}"
                )

        elif slot.temp_lower_f is None and slot.temp_upper_f is not None:
            # "Below X°F" slot (lower=None, upper=X)
            upper_int = int(slot.temp_upper_f)
            # Condition A: daily max exceeded this range — no time gate
            if rounded_max > upper_int and (rounded_max - upper_int) >= margin:
                is_locked = True
                is_below_lock = True
                lock_reason = (
                    f"LOCKED WIN (below): wu_round({daily_max_f:.1f})={rounded_max} "
                    f"> upper {upper_int} + margin {margin}"
                )
            # Condition B not applicable: "Below X" has no lower bound to be above

        elif slot.temp_upper_f is None and slot.temp_lower_f is not None:
            # "≥X°F" slot (lower=X, upper=None)
            lower_int = int(slot.temp_lower_f)
            # If daily_max >= X, YES wins → NO loses → skip entirely
            if rounded_max >= lower_int:
                continue
            # Condition B: daily max finalized below this threshold — needs final
            if daily_max_final and (lower_int - rounded_max) >= margin:
                is_locked = True
                lock_reason = (
                    f"LOCKED WIN (above): wu_round({daily_max_f:.1f})={rounded_max} "
                    f"< lower {lower_int} - margin {margin}"
                )

        if not is_locked:
            continue

        # Hard ceiling: reject locked-win entries above LOCKED_WIN_MAX_PRICE
        # (0.95).  This is a *partial rollback* of Fix 2 — we keep Fix 2's
        # below/above-lock win_prob split (0.999 vs 0.99) but reinstate the
        # price cap because production data (2026-04-17) showed that without
        # it, every locked-win fired at 0.997-0.9985 where the technical
        # +EV (≈$0.0008/share) is smaller than paper→live slippage (≥1 tick).
        # The `ev > 0` check below remains as a safety net — both gates
        # must pass.  See docs/fixes/2026-04-17-lockedwin-price-cap-rollback.md.
        if slot.price_no > LOCKED_WIN_MAX_PRICE:
            logger.debug(
                "LOCKED WIN skip (price %.4f > LOCKED_WIN_MAX_PRICE %.2f): "
                "%s slot %s — margin too thin for live execution",
                slot.price_no, LOCKED_WIN_MAX_PRICE,
                event.city, slot.outcome_label,
            )
            continue

        # Below-slot locks: temperature can only rise, so NO is a bounded
        # certainty.  Historical wu_round error is ≤ 0.5°F and margin ≥ 2
        # guarantees ≥ 1.5°F real cushion → loss probability < 0.1%.
        # Above-slot locks use 0.99 because the afternoon peak could still
        # surge up into the slot (daily_max_final is best-effort).
        win_prob = 0.999 if is_below_lock else 0.99
        ev = (win_prob * (1.0 - slot.price_no)
              - (1.0 - win_prob) * slot.price_no
              - _entry_fee_per_dollar(slot.price_no))

        if ev <= 0:
            continue

        signal = TradeSignal(
            token_type=TokenType.NO,
            side=Side.BUY,
            slot=slot,
            event=event,
            expected_value=ev,
            estimated_win_prob=win_prob,
            is_locked_win=True,
            reason=lock_reason,
        )
        signals.append(signal)

        logger.info(
            "%s: %s slot %s, EV=%.4f",
            lock_reason, event.city, slot.outcome_label, ev,
        )

    return signals


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

    Layer 1 — Locked-win protection:
        If daily_max > slot.upper_bound, NO is a guaranteed winner → never exit.

    Layer 2 — EV-based exit:
        When temperature approaches the slot (distance < exit_threshold),
        re-compute current EV using the closer of daily_max and forecast.
        If EV is still positive → HOLD; if negative → SELL.
        Post-peak: use tighter confidence on daily_max, boosting hold probability.

    Layer 3 — Pre-settlement force exit:
        If within force_exit_hours of settlement AND distance < exit_threshold
        → force SELL regardless of EV (avoid resolution risk).

    Only applies to same-day markets (days_ahead == 0).
    """
    if observation is None or daily_max_f is None:
        return []

    if days_ahead > 0:
        return []

    # Compute exit distance threshold based on trend
    # Use tighter multipliers to avoid premature exits on NO positions.
    # NO wins when temp does NOT land in the slot, so even 3-4°F distance
    # is still a safe position — only exit when truly threatened.
    exit_distance = config.no_distance_threshold_f * 0.25
    if trend == TrendState.STABLE:
        exit_distance = config.no_distance_threshold_f * 0.3
    elif trend in (TrendState.BREAKOUT_UP, TrendState.BREAKOUT_DOWN):
        exit_distance = config.no_distance_threshold_f * 0.2

    # Post-peak: determine confidence for observed daily_max
    peak_conf = None
    if local_hour is not None:
        peak_conf = _post_peak_confidence(local_hour)

    signals: list[TradeSignal] = []
    for slot in held_no_slots:
        # ── Layer 1: Locked-win protection ──
        # If wu_round(daily_max) already exceeded the slot's upper bound
        # by at least locked_win_margin_f, NO wins for certain. Never exit
        # a guaranteed winner.  Without the margin check, positions in the
        # dead zone (exceeded by <margin) would be protected from exit
        # despite not qualifying as locked wins.
        if slot.temp_upper_f is not None:
            gap = wu_round(daily_max_f) - int(slot.temp_upper_f)
            if gap >= config.locked_win_margin_f:
                logger.debug(
                    "EXIT skip (locked win): %s slot %s — daily max %.1f > upper %.1f + margin %d",
                    event.city, slot.outcome_label, daily_max_f, slot.temp_upper_f,
                    config.locked_win_margin_f,
                )
                continue

        distance = _slot_distance(slot, daily_max_f)

        if distance >= exit_distance:
            continue  # still far enough, no exit needed

        # Without a forecast we cannot compute EV, so we cannot tell whether
        # the position is genuinely threatened or just near a slot boundary.
        # Holding is safer than selling blind — skip until forecast is available.
        if forecast is None:
            logger.debug(
                "EXIT skip (no forecast): %s slot %s — cannot evaluate EV, holding",
                event.city, slot.outcome_label,
            )
            continue

        # ── Layer 2: EV-based exit ──
        # Re-compute current EV using the more conservative reference
        # (the closer of daily_max and forecast) for distance/probability.
        win_prob = 0.0
        ev = 0.0
        if forecast is not None:
            # Use daily_max as the reference since it represents the worst case
            # (daily max can only go up, so it's the tighter bound)
            win_prob = _estimate_no_win_prob(slot, forecast, error_dist)

            # Also compute probability using daily_max as reference point
            dist_to_max = _slot_distance(slot, daily_max_f)
            # Post-peak: use tighter confidence (daily_max is near final)
            max_confidence = (peak_conf if peak_conf is not None
                              else forecast.confidence_interval_f)
            wp_from_max = _estimate_no_win_probability_normal(
                dist_to_max, max_confidence,
            )

            if peak_conf is not None:
                # Post-peak: take the MORE favorable probability (daily_max is reliable)
                win_prob = max(win_prob, wp_from_max)
                logger.debug(
                    "EXIT post-peak (h=%d): %s slot %s — wp_forecast=%.3f, wp_observed=%.3f → %.3f",
                    local_hour, event.city, slot.outcome_label,
                    _estimate_no_win_prob(slot, forecast, error_dist), wp_from_max, win_prob,
                )
            else:
                # Pre-peak: take the lower (more conservative) win probability
                win_prob = min(win_prob, wp_from_max)

            ev = win_prob * (1.0 - slot.price_no) - (1.0 - win_prob) * slot.price_no

            if ev >= 0:
                # Positive EV → hold (unless Layer 3 overrides)
                # ── Layer 3: Pre-settlement force exit ──
                if (hours_to_settlement is not None
                        and 0 <= hours_to_settlement <= config.force_exit_hours
                        and distance < exit_distance):
                    signals.append(TradeSignal(
                        token_type=TokenType.NO,
                        side=Side.SELL,
                        slot=slot,
                        event=event,
                        expected_value=ev,
                        estimated_win_prob=win_prob,
                    ))
                    logger.info(
                        "FORCE EXIT: %s slot %s — %.1fh to settlement, distance %.1f°F, EV=%.4f",
                        event.city, slot.outcome_label, hours_to_settlement, distance, ev,
                    )
                else:
                    logger.debug(
                        "EXIT hold (positive EV): %s slot %s — distance %.1f, EV=%.4f",
                        event.city, slot.outcome_label, distance, ev,
                    )
                continue

        # EV is negative (or no forecast available) → sell
        signals.append(TradeSignal(
            token_type=TokenType.NO,
            side=Side.SELL,
            slot=slot,
            event=event,
            expected_value=ev,
            estimated_win_prob=win_prob,
        ))
        logger.info(
            "EXIT signal: %s slot %s — daily max %.1f°F, distance %.1f°F, EV=%.4f",
            event.city, slot.outcome_label, daily_max_f, distance, ev,
        )

    return signals
