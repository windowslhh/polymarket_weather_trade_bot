# Plan: M2 Unified GATE_MATRIX + D1 discovery price filter + O1 alignment alarm

**Status**: planned, 2026-04-20
**Predecessor**: PR #5 (merged `21573b0`) — M1 guardrails + L6 startup alignment check
**Target PR**: PR-B

## Motivation

PR #5 shipped `_price_divergence()` as a shared helper because Bug #1 was a
copy-paste miss between `evaluate_no_signals` and `evaluate_locked_win_signals`.
That helper fixed one concrete gate but didn't fix the structural problem: **each
`evaluate_*` function still encodes its own gate order inline, and the next
similar miss (forgetting to add a new gate to one branch) will recur**.

M2 replaces inline gate ordering with a declarative matrix that is testable,
auditable, and cheap to extend. D1 and O1 are two small follow-ups that ride
along in the same PR because they touch adjacent code.

## Scope

### M2 — Unified GATE_MATRIX

**New files**:
- `src/strategy/gates.py` — `Gate` protocol + ~10 concrete gates + `GATE_MATRIX`

**Modified**:
- `src/strategy/evaluator.py` — each `evaluate_*` becomes a thin wrapper that
  walks `GATE_MATRIX[signal_kind]` and short-circuits on first rejection
- Individual gate unit tests in `tests/test_gates.py`

**Design sketch**:
```python
# src/strategy/gates.py
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

class SignalKind(Enum):
    FORECAST_NO = "forecast_no"
    LOCKED_WIN = "locked_win"
    TRIM = "trim"
    EXIT = "exit"

@dataclass
class EntryContext:
    """All inputs any gate might need. Gates read what they care about."""
    slot: TempSlot
    event: WeatherMarketEvent
    config: StrategyConfig
    forecast: Forecast | None
    error_dist: ForecastErrorDistribution | None
    daily_max_f: float | None
    daily_max_final: bool
    local_hour: int | None
    hours_to_settlement: float | None
    trend: TrendState | None
    days_ahead: int
    held_token_ids: set[str]
    entry_prices: dict[str, float]
    entry_ev_map: dict[str, float]
    locked_win_token_ids: set[str]
    # Computed lazily by gates that need them:
    _win_prob: float | None = None
    _ev: float | None = None

@dataclass
class RejectReason:
    code: str  # DECISION_LOG_REASON
    extra: dict

class Gate(Protocol):
    def evaluates(self, ctx: EntryContext) -> RejectReason | None:
        """Return None to pass; RejectReason to block."""
        ...

# Concrete gates (each ~10-30 lines):
class HeldTokenGate: ...                  # skip already-held tokens
class DailyMaxAboveLowerGate: ...         # wu_round(max) >= slot.lower
class DailyMaxInSlotGate: ...             # wu_round(max) ∈ [L, U]
class DailyMaxBelowUpperGate: ...         # post-peak, max < upper
class DistanceGate: ...                   # distance < threshold
class PriceBoundsGate: ...                # 0 < price < 1
class PriceFloorGate: ...                 # price < min_no_price
class PriceCeilingGate: ...               # price > max_no_price
class EvGate: ...                         # ev < ev_threshold
class PriceDivergenceGate: ...            # |win_prob - price| > threshold
# Locked-win specific:
class LockedWinConditionGate: ...         # the Condition A/B state machine
class LockedWinPriceCapGate: ...          # price > locked_win_max_price
class EvPositiveGate: ...                 # ev <= 0
# TRIM specific:
class AbsoluteEvGate: ...                 # ev < -min_trim_ev_absolute
class RelativeEvDecayGate: ...            # ev < entry_ev × (1 - ratio)
class PriceStopGate: ...                  # price <= entry × (1 - price_ratio)
# EXIT specific:
class LockedWinProtectionGate: ...        # never exit locked wins
class ForceExitGate: ...                  # hours_to_settle < threshold

GATE_MATRIX: dict[SignalKind, list[Gate]] = {
    SignalKind.FORECAST_NO: [
        HeldTokenGate(), DailyMaxAboveLowerGate(), DailyMaxInSlotGate(),
        DailyMaxBelowUpperGate(), DistanceGate(), PriceBoundsGate(),
        PriceFloorGate(), PriceCeilingGate(), EvGate(),
        PriceDivergenceGate(),
    ],
    SignalKind.LOCKED_WIN: [
        HeldTokenGate(), PriceBoundsGate(), PriceFloorGate(),
        LockedWinConditionGate(), LockedWinPriceCapGate(),
        EvPositiveGate(), PriceDivergenceGate(),
    ],
    SignalKind.TRIM: [AbsoluteEvGate(), RelativeEvDecayGate(), PriceStopGate()],
    SignalKind.EXIT: [
        LockedWinProtectionGate(), DistanceGate(), ForceExitGate(),
    ],
}
```

**Evaluator becomes**:
```python
def evaluate_no_signals(event, forecast, config, ...) -> list[TradeSignal]:
    signals = []
    for slot in event.slots:
        ctx = EntryContext(slot=slot, event=event, config=config, ...)
        rejected = False
        for gate in GATE_MATRIX[SignalKind.FORECAST_NO]:
            if (reason := gate.evaluates(ctx)) is not None:
                _reject(slot, reason.code, **reason.extra)
                rejected = True
                break
        if not rejected:
            signals.append(_build_signal_from_ctx(ctx))
    return signals
```

**Acceptance criteria**:
1. All existing `test_strategy.py` / `test_locked_win.py` / `test_trim_signals.py`
   / `test_daily_max_guard.py` integration tests pass **unchanged**.
2. New `tests/test_gates.py` covers each gate in isolation with ≥2 cases
   (pass + reject).
3. No behavioral change in production: run backtest script vs pre-M2 and
   confirm signal counts match within ±1%.
4. `evaluate_no_signals` + `evaluate_locked_win_signals` lose their inline
   gate code — become ≤30 lines each.

**Risk**:
- Refactor touches the hottest code path. Mitigate with coverage check
  (integration tests are extensive) and pre/post backtest compare.
- Gate ordering matters (e.g. `HeldTokenGate` must come first for silent
  skip). Document ordering invariants in a module docstring.

### D1 — Discovery filter 0-price slots

**Why**: PR#5 Problem 8 documented that `price_no > 0` in TRIM price-stop is
a defensive hack because Gamma returns 0 for illiquid slots. Clean source:
drop them in discovery.

**Location**: `src/markets/discovery.py`, after `_parse_temp_bounds` returns
and `price_no` is parsed. Add:
```python
if price_no <= 0.0 or price_no >= 1.0:
    logger.debug("Skipping slot %s with invalid NO price %.4f", label, price_no)
    continue  # don't add to slots list
```

**Test**: add to `test_markets_discovery.py` (or wherever slot parsing lives).

**Secondary cleanup**: after D1 lands, remove the `slot.price_no > 0`
defensive check in `evaluator.py::PriceStopGate` (and update the comment
that mentions "future data-layer cleanup").

### O1 — Alignment alarm on bulk UNRESOLVED

**Why**: today if Polymarket changes `resolutionSource` URL format and every
city stops matching, `check_station_alignment` logs 30 UNRESOLVED warnings
+ "OK to proceed". Operator misses the signal.

**Change**: `src/main.py` alignment block, after collecting issues:
```python
unresolved = [i for i in alignment_issues if i.kind == "UNRESOLVED"]
total_cities = len(config.cities)
if total_cities > 0 and len(unresolved) / total_cities > 0.8:
    logger.error(
        "CRITICAL ALIGNMENT ANOMALY: %d/%d cities have UNRESOLVED events (>80%%). "
        "Polymarket's resolutionSource URL format may have changed. "
        "Investigate before trading — see extract_settlement_icao regex.",
        len(unresolved), total_cities,
    )
    # Do NOT sys.exit here — transient Gamma weirdness shouldn't block deploys,
    # but the ERROR log + alerter hook should surface loudly.
```

**Test**: add `test_alignment_bulk_unresolved_alarm` to
`test_historical.py::TestStationAlignmentLive`.

**Optional**: tie into `src/alerts.py` so webhook also fires (currently only
on trading errors). Scope decision — defer if it expands the PR.

## Out of scope for this PR

- M3 (METAR-based calibration) — separate PR for clean revert
- M4 (cross-strategy budget sharing) — product decision needed first
- Removing `StrategyConfig.min_trim_ev` legacy field — PR#5 already noted it's back-compat only

## Hand-off notes for fresh session

- PR #5 (merged `21573b0`) landed the 3-commit stack: data layer ICAO fix
  + M1 guardrails + review feedback. Read `CLAUDE.md` for current state.
- VPS is running the merged code as of 2026-04-20 14:41 UTC. DB was wiped
  during deploy (`data/bot.db.before-pr5-merge-20260420-1439` is the backup).
- The worktree is `gallant-beaver-1ec8fe` on branch `claude/gallant-beaver-1ec8fe`.
  PR-B should branch off the same merge commit (`21573b0`).
- Start by reading:
  1. This file
  2. `CLAUDE.md` (especially Strategy Design section)
  3. `src/strategy/evaluator.py` — the main refactor target
  4. `tests/test_strategy.py` + `test_locked_win.py` + `test_trim_signals.py`
     to understand integration-level expectations
