# Plan: M4 Cross-strategy city budget (fixes Bug #2)

**Status**: planned, 2026-04-20. **Requires a product decision before
engineering starts** — see "Decision required" below.
**Predecessor**: M2 (PR-B) strongly recommended — unified GATE_MATRIX
makes per-strategy differences cleaner, which matters if we keep 4.
**Target PR**: PR-D (after a decision is made)

## Motivation (Bug #2)

Current state: `StrategyConfig.max_exposure_per_city_usd` (default $50,
$25-$30 per strategy variant via `get_strategy_variants`) is enforced
**independently per strategy** in `src/strategy/rebalancer.py`.

A/B/C/D all share the same signal source (`evaluate_no_signals`,
`evaluate_locked_win_signals`). A single bad signal — e.g. a locked-win
built on a stale daily_max — produces 4 entries simultaneously, each up
to its own per-strategy cap. Real cross-strategy exposure is **4× the
configured per-city limit**.

This is what happened at Houston on 2026-04-17 before the ICAO fix: the
wrong-station locked-win fired across all 4 strategies, putting ~$120
on a single mis-signaled market where config nominally allowed ~$50.

The Houston blow-up is mitigated now by PR #5's PRICE_DIVERGENCE gate
and station-alignment check — but the underlying amplification risk
remains, and any future signal error will hit 4× instead of 1×.

## Decision required (before coding)

The 4-strategy setup exists for **A/B/C/D observability** — testing
different kelly fractions, EV thresholds, force-exit windows. Sharing
a budget pool fundamentally changes that experiment.

Options:

### (a) Priority order A > B > C > D
Simple. But B/C/D only fire when A's budget is exhausted, killing their
independent signal comparison. **Not recommended.**

### (b) Proportional first-pass + leftover reallocation
Each strategy gets 1/4 of the cap in round 1; anything unspent gets
redistributed in round 2. Preserves some independence but complicates
sizing (current half-Kelly assumes full per-strategy budget available).

### (c) Shadow mode — pick 1 live strategy, run others as shadow
Only **one** strategy (e.g. B, the current "locked aggressor") actually
builds positions. A/C/D compute signals and log them to the decision_log
but never hit the executor. Real exposure reverts to intended (1× cap).
Observability preserved via the log.

### (d) Drop to 2 strategies
If A/B/C/D were hypothesis-testing and the experiment has concluded,
consolidate to whichever two variants genuinely differ in signal logic
(e.g. "forecast-aggressive" and "locked-aggressive") — and share budget
across just those two. Per-city cap at $50 means 2× amplification max,
tolerable.

### (e) Multiplicatively size-adjust per strategy
Keep 4 strategies but scale each one's per-slot sizing by 1/4 so the
aggregate stays within cap. Feasible but breaks the half-Kelly intent
— sizing is no longer tied to EV / conviction but to "there are 3
other strategies running."

**My recommendation**: **(c) Shadow mode**. Cleanest implementation,
preserves all observability, eliminates the amplification risk in one
step. If the experiment later wants to promote a shadow strategy to
live, it's a config flag flip.

This is a product call. Do not ship engineering until the decision is
confirmed.

## Scope (assumes option c — adjust if decision differs)

### New module

`src/portfolio/city_budget.py` — tracks cumulative exposure per city
across all strategies in a rebalance cycle. Thin stateless helper.

```python
class CityBudget:
    """Tracks total per-city USD exposure across all strategy variants
    within one rebalance cycle. Prevents 4× amplification when the same
    signal fires in multiple strategies."""

    def __init__(self, per_city_cap: float, thin_liquidity_cap: float,
                 thin_cities: frozenset[str]) -> None: ...

    def current(self, city: str) -> float:
        """Current reserved (USD) for this city this cycle."""

    def try_reserve(self, city: str, strategy: str, usd: float) -> bool:
        """True if reservation fits within per-city cap; tracks it on success."""

    def release_on_exit(self, city: str, strategy: str, usd: float) -> None: ...
```

### Config change

`StrategyConfig` gains:
```python
live_strategy: str = "B"  # only this variant actually builds positions
# Others compute signals for decision_log / backtest but don't execute
```

### Rebalancer change

`src/strategy/rebalancer.py::rebalance_once`:
1. Build `city_budget` at start of cycle
2. For each (strategy, event, signal):
   a. Compute size via existing Kelly logic
   b. If `strategy != config.live_strategy` → log signal, **skip
      execution**
   c. Else: `city_budget.try_reserve(city, strategy, size_usd)` — if
      reservation fails, skip (log as CITY_BUDGET_EXHAUSTED reject)
3. On EXIT signals for the live strategy, `release_on_exit`

### Schema change

`positions` table already has a `strategy` column. Add a
`decision_log_reason` = `CITY_BUDGET_EXHAUSTED` for shadow strategies
so dashboard can show "this would have traded under A/B/C/D if live."

### Dashboard

`src/web/app.py` — add a "shadow signals" panel showing the top
rejected signals per strategy per city. Helps see whether the chosen
live strategy is actually the best.

## Acceptance criteria

1. `city_budget` unit tests cover: reserve-within-cap, reject-over-cap,
   release-decreases-current, thin-liquidity cities use reduced cap
2. Integration test: 2 strategies both try to enter the same city,
   only the live one gets a position, shadow logs `SHADOW_ONLY` reason
3. Backtest: re-run a known historical day (e.g. 2026-04-15) with
   `live_strategy="B"` and confirm:
   - B's trades are identical to current (single-strategy view of B)
   - A/C/D show signals in decision_log but zero positions
4. Dashboard shows both live positions and shadow signal counts

## Risks

- Consolidating to 1 live strategy loses the 4-way comparison if we
  hadn't yet decided which strategy is best. Before shipping, verify
  we have enough historical data to pick a winner; otherwise run
  shadow-mode in paper mode for N weeks first
- If the chosen live strategy (e.g. B) happens to be the one that
  would have missed a profitable trade A caught, we'd never know
  without the shadow log — so the shadow-signal dashboard is critical
- Position release on EXIT requires strategy-tag tracking on every
  exit path — audit that positions.strategy is always set correctly
  (spot-check: `src/execution/executor.py::fill` currently reads
  `signal.strategy`, looks OK, but verify)

## Out of scope

- Changing the strategy variants themselves (`get_strategy_variants`
  stays as-is; this is only about *execution gating*)
- New `StrategyKind` enum — strategy is still a string identifier
- Rewriting the rebalancer — only adding pre-execution budget check

## Hand-off notes

- **Do not start coding** until the option (a-e) decision is made
- Once decided, M4 is 1-2 days of focused work
- Recommended to do this AFTER M2 (GATE_MATRIX) because adding
  `CITY_BUDGET_EXHAUSTED` is cleaner as a new gate entry rather than
  another inline check in the rebalancer
- Best tested on a day where signal load was high (multi-city
  locked-wins) — check the `decision_log` for days with 4+ entries
  per (city, slot_label)
