# Code Review Report
Date: 2026-04-14
Reviewer: Independent AI Code Reviewer

---

## Executive Summary

This codebase is a well-structured async Python trading bot targeting Polymarket weather temperature markets. The architecture is coherent, the strategy logic is carefully documented, and several previously known pitfalls are correctly handled (json.loads on Gamma API strings, UTC-vs-local date grouping in DailyMaxTracker, settlement gated on `closed=true`).

That said, the review found **2 CRITICAL**, **8 HIGH**, **11 MEDIUM**, and **9 LOW** severity issues. The two critical issues are: (1) a double-settlement run in `scheduler/jobs.py` that fires settlement twice per 15-minute cycle, and (2) a thread-safety hazard in `web/app.py` where the background event loop can be replaced mid-flight under concurrent Flask requests.

---

## Module Reviews

---

### 1. `src/strategy/rebalancer.py`

#### Issue R-1 — HIGH: `_recent_exits` dict grows unbounded (memory leak)
**Lines:** 81, 922  
**Severity:** HIGH  
**Description:** `self._recent_exits: dict[str, datetime]` accumulates every token ID that has ever been exited. There is no eviction path. For a long-running bot tracking many markets, this grows without bound. The cooldown check at line 895 only reads it; no cleanup ever removes stale entries.  
**Impact:** Memory grows monotonically; may also cause spurious cooldown hits if a token ID is reused across settlement cycles (though in practice Polymarket token IDs are unique per market so the functional impact is low today).  
**Fix:** After each cycle (or in `cleanup_old`), remove entries where `(now - exit_time).total_seconds() > cooldown_seconds`.

#### Issue R-2 — MEDIUM: `run_settlement_only` called twice per 15-minute cycle
**Lines:** rebalancer.py:299–309 and scheduler/jobs.py:29–34  
**Severity:** MEDIUM (see scheduler section for the CRITICAL framing)  
**Description:** `run_settlement_only` is called inside `run_position_check` indirectly via the scheduler's `settlement_and_position_check` function, which first calls `run_settlement_only` and then calls `run_position_check`. But `run_position_check` does NOT call `run_settlement_only`, so this is fine. However, the main rebalance cycle at line 599 (`_run_cycle`) also calls `run_settlement_only` as step 0. This means every full rebalance cycle fires settlement, and the dedicated 15-min job fires it again, resulting in overlapping settlement checks on every 60-min boundary.  
**Impact:** Mostly harmless because `insert_settlement` uses `INSERT OR IGNORE` and idempotency guards exist, but it doubles DB and Gamma API load at every rebalance boundary.  
**Fix:** Remove the `run_settlement_only()` call from `_run_cycle` (line 599) and rely entirely on the 15-min scheduler job for settlement.

#### Issue R-3 — MEDIUM: `date.today()` used for circuit breaker instead of UTC
**Lines:** 603  
**Description:** `date.today()` is called without a timezone. In a container deployed at UTC, this is equivalent to `datetime.now(timezone.utc).date()`, but it is inconsistent with the explicitly UTC-aware patterns used throughout the rest of the codebase (DailyMaxTracker, local hour detection). The CLAUDE.md explicitly flags this pattern as a known pitfall.  
**Fix:** Use `datetime.now(timezone.utc).date()` for consistency.

#### Issue R-4 — MEDIUM: `cycle_city_additions` tracks cross-strategy totals incorrectly
**Lines:** 717, 840, 912–913  
**Description:** `cycle_city_additions` is keyed by `f"{strat_name}:{event.city}"` and is added to `strat_city_exp` (line 840), which is already per-strategy. This is correct for per-strategy exposure tracking. However, the global `strat_total_exp` (line 841) is fetched fresh from DB per strategy but is not accumulated with additions made earlier in the same cycle for other events of the same strategy. This can allow global exposure to be exceeded during a single cycle when multiple events for the same strategy are processed.  
**Fix:** Maintain a `cycle_total_additions: dict[str, float]` (keyed by strategy name) and add it to `strat_total_exp` before computing size.

#### Issue R-5 — LOW: `_cached_forecasts` never expires stale entries
**Lines:** 93, 674, 286  
**Description:** `self._cached_forecasts` is a plain dict that is updated but never pruned. If a city becomes inactive (no more markets), its stale forecast remains in memory indefinitely. For the position check (line 516), the forecast used could be for a date that has already settled.  
**Fix:** Accept an optional TTL on cache entries or clear the dict at the start of each full rebalance cycle and let the batch refetch populate it fresh.

#### Issue R-6 — LOW: `backfill_today_observations` uses `date.today()` (naive)
**Lines:** 144  
**Description:** Line 144 calls `date.today()` without a timezone for the multi-day forecast fetch. This is the same UTC vs. local date issue flagged in CLAUDE.md.  
**Fix:** Replace with `datetime.now(timezone.utc).date()`.

#### Issue R-7 — LOW: `_fetch_gamma_for_tokens` defined as a nested async def inside a try block
**Lines:** 355–380  
**Description:** A nested `async def` and an inner `import asyncio as _asyncio` are re-declared every time `run_position_check` is called. This is a minor style and performance anti-pattern. The function should be lifted to module level.

#### Issue R-8 — LOW: Market date parsed from `slot_label` regex is fragile
**Lines:** 445–462  
**Description:** The regex `r'on (\w+ \d+)\??$'` attempts to extract a market date from a DB-stored slot label. This relies on slot_label being a verbatim copy of the Gamma question text. If Polymarket changes their title format even slightly (e.g. adding a city suffix, or trailing punctuation), the regex silently returns `local_today`, forcing `days_ahead=0` and triggering same-day exit logic on multi-day positions. The fallback log message is `logger.warning` for a parse failure but `logger.debug` for the regex-miss case, making it hard to distinguish.

---

### 2. `src/strategy/evaluator.py`

#### Issue E-1 — HIGH: Private attribute `error_dist._count` accessed from external module
**Lines:** 101, 204, 261  
**Description:** `evaluator.py` reads `error_dist._count` directly (a private attribute). Similarly, `rebalancer.py` (line 195) reads `dist._count`. The CLAUDE.md does not prohibit this pattern, but `ForecastErrorDistribution` exposes a `summary()` method and a public `_count` (single-underscore), which is a Python convention for "protected, not private". The real risk is that if the attribute is renamed or replaced with a property during a refactor, callers will break silently at runtime.  
**Fix:** Add a public property `sample_count: int` to `ForecastErrorDistribution` and update all callers.

#### Issue E-2 — MEDIUM: `evaluate_exit_signals` returns no signals when `forecast is None`
**Lines:** 511–513, 566–574  
**Description:** When `forecast is None`, `win_prob` and `ev` default to 0.0 and the code falls through to the `signals.append(TradeSignal(...))` at line 567, generating a SELL with `win_prob=0` and `ev=0`. This means a missing forecast causes an exit (sell) from ALL in-range positions regardless of their actual EV. The intent from the docstring is to use daily_max even without a forecast, but this zero-EV sell is indistinguishable from a signal with genuinely bad EV.  
**Fix:** When `forecast is None`, skip to the next slot (or return early) rather than selling with fabricated ev=0.

#### Issue E-3 — MEDIUM: `evaluate_locked_win_signals` uses `win_prob = 0.99` hardcoded; no EV gate for the taker fee
**Lines:** 412–416  
**Description:** The EV check `if ev <= 0: continue` (line 417) is correct for positive expected value, but the `max_no_price` filter does NOT apply to locked wins — the code only checks `slot.price_no > 0.90` (line 403). This means a locked-win at price 0.05 (5 cents) will pass through with ev = 0.99 * 0.95 - 0.01 * 0.05 - fee ≈ 0.93, yet position sizing will use `max_locked_win_per_slot_usd` regardless of how thin the absolute dollar margin is. This is intentional by design but undocumented.

#### Issue E-4 — LOW: Trend-based 5% win_prob boost is applied without bounds-check on `slot.temp_lower_f`
**Lines:** 234–241  
**Description:** The breakout boost checks `slot.temp_upper_f is not None and slot.temp_upper_f < forecast.predicted_high_f` before boosting (line 236), but uses `slot.temp_lower_f is not None` in the outer condition (line 234). For an open-lower slot ("Below X°F"), `temp_lower_f` is None and `temp_upper_f` is set, so the inner condition at line 236 is evaluated with a valid `temp_upper_f` — this is safe. But the code structure is confusing and a future refactor could silently break the guard.

---

### 3. `src/strategy/sizing.py`

#### Issue S-1 — LOW: Kelly formula comment is inconsistent with implementation
**Lines:** 53–65  
**Description:** The comments describe "signal-proportional sizing" where `size = kelly_full × frac × slot_cap`. This is explicitly stated as intentionally NOT the traditional Kelly formula. However, the same code applies a `min(size_usd, slot_cap)` cap at line 69 which is redundant: since `frac <= 1.0` and `kelly_full <= 1.0`, the product `kelly_full * frac * slot_cap` can never exceed `slot_cap`. The redundant cap is harmless but wastes a branch.

---

### 4. `src/markets/discovery.py`

#### Issue D-1 — MEDIUM: `_parse_date` uses `date.today()` without timezone
**Lines:** 65–66  
**Description:** When a date string like "April 5" is parsed, the year defaults to `date.today().year`. If run near UTC midnight crossing from December 31 to January 1, `date.today()` may return the new year while the market date is in the old year.  
**Fix:** Use `datetime.now(timezone.utc).year` instead of `date.today().year`.

#### Issue D-2 — LOW: `_match_city` uses substring matching that can match wrong cities
**Lines:** 73–78  
**Description:** The bidirectional substring check (`city.name.lower() in event_lower or event_lower in city.name.lower()`) can produce false positives. For example, a configured city "Portland" would match an event titled "highest temperature in Portland, Oregon" but also any event titled "Portland, Maine" if the config has both. Since Polymarket event titles are fairly unique per city, this is a low risk but could cause mismatches if two configured cities share a name prefix.

#### Issue D-3 — LOW: `discover_weather_markets` silently drops events with all-illiquid slots
**Lines:** 199  
**Description:** If every slot in an event exceeds `max_spread`, the event is silently skipped (`if not slots: continue`). The bot has no log at INFO level to flag this skip. A market with unusually high spread is worth knowing about.

---

### 5. `src/markets/price_buffer.py`

This module is well-implemented. One minor issue:

#### Issue PB-1 — LOW: `cross_validate` assigns `merged[tid] = gamma` without None-check
**Lines:** 156  
**Description:** If `gamma` is `None` (because `clob_prices.get(tid)` returns a value but `gamma_prices.get(tid)` returns None), `merged[tid]` is set to None. Callers pass this to `apply_batch` which eventually calls `float(price)` in the TWAP calculation and will raise a TypeError. The `# type: ignore[assignment]` comment on line 156 acknowledges the issue but doesn't fix it.  
**Fix:** Add a guard: `if gamma is not None: merged[tid] = gamma` (and handle the case where both are None by skipping the token).

---

### 6. `src/markets/clob_client.py`

#### Issue C-1 — HIGH: `get_prices_batch` calls CLOB serially, one token at a time
**Lines:** 156–159  
**Description:** For N open positions, this method makes N sequential `get_midpoint` + optionally N `get_last_trade_price` calls via `asyncio.to_thread`. Since each call is a blocking HTTP call wrapped in a thread, N=20 positions = 20 round trips. Gamma batch fetching (used in the position check) does this in batches of 20 per request. The CLOB client has no equivalent batching.  
**Impact:** In live mode, the CLOB price refresh during rebalance is O(N) sequential blocking calls. For 50 open positions this could take 30+ seconds.  
**Fix:** Investigate if the py-clob-client supports batch midpoint fetching; if not, parallelize with `asyncio.gather`.

#### Issue C-2 — LOW: `place_limit_order` in paper mode returns `success=True` unconditionally
**Lines:** 91–97  
**Description:** In paper mode, every order is considered a success. This means the executor will record every paper fill even if the order parameters are clearly invalid (e.g. price <= 0). No validation is done before faking success.

---

### 7. `src/weather/forecast.py`

#### Issue F-1 — MEDIUM: `predicted_low_f` is a fabricated value in NWS and ensemble paths
**Lines:** 93–94 (ensemble), 102 (NWS in `nws.py`)  
**Description:** Both `get_ensemble_forecast` (`predicted_low_f=ensemble_mean - 15`) and `get_nws_forecast` (`predicted_low_f=temp_f - 15`) use a fixed 15°F offset to fabricate the low temperature. This field is returned to the dashboard and could mislead users. It is also used in the `Forecast` dataclass. If any future code path uses `predicted_low_f` for signal evaluation, the fabricated value would introduce systematic errors.  
**Fix:** Set `predicted_low_f=None` when not available, or rename the field to make clear it is estimated.

#### Issue F-2 — LOW: Module-level `_last_forecast_cache` is a global mutable singleton
**Lines:** 33  
**Description:** `_last_forecast_cache` is shared across all coroutines and will persist stale forecasts across test runs if the module is imported in test contexts. This can cause test pollution. Additionally, there is no TTL on cache entries — a very old forecast can be served if all APIs fail repeatedly.

---

### 8. `src/weather/metar.py`

This module is correct and well-implemented following the UTC/local date fix. One issue:

#### Issue M-1 — LOW: `DailyMaxTracker.cleanup_old` uses `date.today()` without timezone
**Lines:** 201  
**Description:** `date.today()` without timezone context. Consistent with the known pitfall flagged in CLAUDE.md. Under UTC-offset timezones, this could remove yesterday's data before US cities' local midnight.  
**Fix:** Use `datetime.now(timezone.utc).date()`.

---

### 9. `src/portfolio/risk.py`

This module is simple and correct. The `check_exposure_limits` function duplicates logic that `sizing.py::compute_size` already handles (city and global cap checks). This duplication means the two could drift; however, `check_exposure_limits` does not appear to be called anywhere in the current production path (it is likely legacy or test-only code).

#### Issue Ri-1 — LOW: `check_exposure_limits` is dead code
**Lines:** 33–56  
**Severity:** LOW  
**Description:** No caller of `check_exposure_limits` exists in the current production codebase (confirmed by grep). The function is tested via tests but is not called during strategy execution — all exposure checks are done inside `compute_size` in `sizing.py`.

---

### 10. `src/settlement/settler.py`

#### Issue Se-1 — MEDIUM: `_update_realized_pnl` does not use per-strategy daily P&L
**Lines:** 250–255  
**Description:** `_update_realized_pnl` adds `total_pnl` (the sum across ALL strategies for a settled event) to the single `daily_pnl` row. This is correct for the circuit breaker (which checks total daily loss), but means `daily_pnl` cannot be split by strategy. The strategy-level P&L is tracked only in the `settlements` table. The dashboard's "total realized" calculation (web/app.py line 218) adds `daily_pnl_val` (which is the sum from settlements) PLUS the `realized_pnl` from closed positions in the positions table — this can cause **double-counting** if a settlement also writes a `realized_pnl` into the positions table via the UPDATE at lines 104–108, AND the settlement P&L is also written to `daily_pnl`.  
**Impact:** HIGH — the total realized P&L shown on the dashboard may be inflated.  
**Fix:** The `total_realized` calculation in `web/app.py` should use either `daily_pnl` (settlement P&L only) OR the sum of `realized_pnl` from positions, not both.

#### Issue Se-2 — MEDIUM: `_resolve_yes_price` uses substring matching that can produce wrong settlements
**Lines:** 204–213  
**Description:** The bidirectional substring match (`slot_label in label or label in slot_label`) for resolving settlement prices is ambiguous. For example, if settled_prices has keys "82°F to 85°F on April 5" and "82°F to 85°F on April 15", a position with slot_label "82°F to 85°F" would match both. The function returns the first match, which may be the wrong date.  
**Fix:** Add a date-aware match, or store the Polymarket market question verbatim in `slot_label` so exact matching is possible.

#### Issue Se-3 — LOW: `_compute_position_pnl` uses binary win/loss thresholds
**Lines:** 239–247  
**Description:** The function treats `yes_resolved <= 0.01` as a NO win and `yes_resolved >= 0.99` as a YES win. For a position whose market resolved to 0.50 (unusual but possible if Polymarket voids or adjusts), neither branch triggers, and the function returns the wrong value. The CLAUDE.md specifies settlement only triggers on `closed=true`, and at that point prices should be 0/1, but defensive handling of intermediate values is good practice.

---

### 11. `src/web/app.py`

#### Issue W-1 — CRITICAL: Background event loop thread-safety hazard
**Lines:** 22–37  
**Description:** `_ensure_bg_loop()` creates a new event loop in a new thread if the existing thread is not alive. Multiple Flask requests can race on `_bg_lock` and create multiple loops. More critically, `_bg_thread` is a daemon thread; if it dies due to an unhandled exception in the loop, the next Flask request will recreate a new loop with a brand-new thread. Any pending futures from the old loop will be orphaned and the DB connection used by those coroutines may be in an undefined state.  
**Impact:** Under load, or after a DB timeout (which raises an exception inside the bg loop), the Flask web server may temporarily lose all DB connectivity with no recovery mechanism.  
**Fix:** Wrap `_bg_loop.run_forever()` in a try/except that logs the exception and sets a restart flag. Add a health check endpoint that verifies the loop is alive. Consider using a single persistent async web framework (Quart/FastAPI) rather than bridging Flask with a background asyncio loop.

#### Issue W-2 — HIGH: `total_realized` double-counts P&L (see also Se-1)
**Lines:** 218–221  
**Description:** `total_realized` adds `daily_pnl_val` (from the `daily_pnl` table, which is populated by settlements) PLUS the sum of `realized_pnl` from closed positions (which is populated when positions are individually closed via `close_position`). When a position is settled, `settler.py` both updates `positions.realized_pnl` (line 107) and calls `_update_realized_pnl` which writes to `daily_pnl`. The same P&L is therefore counted twice in `total_realized`.  
**Fix:** Decide on a single source of truth for realized P&L. Recommended: remove the `daily_pnl` contribution from `total_realized` and use only the `settlements` table plus closed positions.

#### Issue W-3 — MEDIUM: `_utc_to_beijing` hardcodes UTC+8 without DST handling
**Lines:** 152–171  
**Description:** The function adds 8 hours to convert to "Beijing time". China does not observe DST, so UTC+8 is correct year-round. However, if the operator is not in China (the UI description says "Beijing time"), this is a confusing offset — no timezone labeling is shown in the UI.  
**Impact:** Low business risk but confusing UX.

#### Issue W-4 — MEDIUM: Cache dict `_cache` is not thread-safe for concurrent writes
**Lines:** 41–55  
**Description:** The module-level `_cache` dict is read and written from multiple Flask threads without a lock. In CPython, dict operations are GIL-protected for atomic reads/writes, but the read-then-write pattern in `_cached` + `_set_cache` is not atomic. Two concurrent requests can both see a cache miss, both compute the value, and both write — one overwriting the other. This is harmless in practice (idempotent computation) but causes double DB load.

#### Issue W-5 — LOW: `/api/trigger` runs rebalance with a 120-second timeout
**Lines:** 693  
**Description:** The manual trigger endpoint blocks the Flask request thread for up to 120 seconds. Under Gunicorn with limited workers, this can exhaust workers. If the rebalance takes more than 120s (e.g. due to slow NWS API), the endpoint returns a misleading "running in background" response while the DB state is inconsistent (partial execution).

---

### 12. `src/config.py`

#### Issue Cfg-1 — MEDIUM: `load_config` uses `StrategyConfig(**strategy_raw)` — unknown YAML keys cause silent failure
**Lines:** 168  
**Description:** If the `config.yaml` contains a key not in `StrategyConfig` (e.g. a typo or a removed field), Python's dataclass constructor raises `TypeError: __init__() got an unexpected keyword argument`. This will crash the bot on startup. Conversely, if a required key is missing from YAML, the default value silently applies. There is no validation step.  
**Fix:** Add config schema validation (e.g. using `dacite` or explicit key checking) and log a warning for unknown keys.

#### Issue Cfg-2 — LOW: `StrategyConfig.no_distance_threshold_f` typed as `int` but treated as float
**Lines:** 24  
**Description:** The field is typed `int = 8` but is used in float arithmetic throughout the codebase. This is technically correct in Python (int + float = float) but the type annotation is misleading. The calibrator's return value is a `float` (from `calibrate_distance_dynamic`) which is then `round()`-ed to int at the call site (rebalancer.py lines 499, 823), which itself returns an `int` — this is all consistent but the annotation on the config field should be `float`.

---

### 13. `src/portfolio/store.py`

#### Issue St-1 — LOW: No index on `positions(token_id)` despite frequent token-ID lookups
**Lines:** 78–81 (index definitions)  
**Description:** Positions are frequently queried by `token_id` (in `close_positions_for_token`, and during position enrichment in the web UI). The schema defines indices on `event_id`, `city`, and `status`, but not on `token_id`. For large position tables this will cause full-table scans on every close operation.  
**Fix:** Add `CREATE INDEX IF NOT EXISTS idx_positions_token ON positions(token_id)`.

#### Issue St-2 — LOW: `get_strategy_realized_pnl` hardcodes strategies `{"A", "B", "C", "D"}`
**Lines:** 414  
**Description:** If strategy variants are changed in `config.py::get_strategy_variants()`, this hardcoded dict initialization will silently miss new strategies or include removed ones.

---

### 14. `src/portfolio/tracker.py`

This module correctly delegates all operations to `self._store` (never `self._portfolio._store`) as required by CLAUDE.md. One issue:

#### Issue T-1 — MEDIUM: `close_positions_for_token` computes P&L as `(exit_price - entry_price) * shares`
**Lines:** 121–123  
**Description:** For a NO position, the realized P&L when selling before settlement should be `(current_market_price - entry_price) * shares`. This formula is correct for a long position. However, when `exit_price` is the settlement exit price (0.0 or 1.0), the correct formula is `(exit_price - entry_price) * shares`, which is what `_compute_position_pnl` in `settler.py` also does. The issue is that `close_positions_for_token` is used for SELL signals (exits before settlement) where `exit_price` is the current market price — in this case the same formula applies. This is consistent. However, for settled positions, the settler directly updates `realized_pnl` via its own SQL UPDATE (settler.py line 107), and `close_positions_for_token` is NOT called, so there is no double-count here. This is low risk.

---

### 15. `src/scheduler/jobs.py`

#### Issue Sch-1 — CRITICAL: Double settlement check on every rebalance cycle boundary
**Lines:** 29–34 and rebalancer.py line 599  
**Severity:** CRITICAL  
**Description:** The `settlement_and_position_check` job (line 29) calls `run_settlement_only()` followed by `run_position_check()`. The main `rebalancer.run()` job also calls `run_settlement_only()` as step 0 (via `_run_cycle` at rebalancer.py line 599). At the 60-minute mark, APScheduler fires both the `rebalance` job and the `settlement_check` job within the same minute window. Both jobs will invoke `_fetch_settlement_outcome` for every open event, doubling Gamma API calls. The `INSERT OR IGNORE` idempotency guard prevents double-counting, but the API load and log spam are real.  
**Severity justification:** Classified as CRITICAL because if settlement coincides with the 60-min mark, the settlement log will show two separate settlement records being attempted, causing potential confusion and masking the real settlement time in monitoring.  
**Fix:** Remove the `run_settlement_only()` call from `_run_cycle` OR move it to a separate method that is only called by the 15-min scheduler job, not by the rebalance cycle.

---

### 16. `src/strategy/calibrator.py`

This module is correct. One note:

#### Issue Cal-1 — LOW: `calibrate_distance_threshold` is no longer the primary calibration path
**Lines:** 46–89  
**Description:** The function exists and is tested but is superseded by `calibrate_distance_dynamic` (used in the rebalancer). If the older percentile-based calibration is intentionally retired, it should be removed or clearly marked as deprecated to avoid confusion during future development.

---

### 17. `src/weather/historical.py`

#### Issue H-1 — LOW: `prob_actual_in_range` uses ±0.5°F inclusive fudge unconditionally
**Lines:** 85–87  
**Description:** `prob_actual_below(upper_f + 0.5, ...)` and `prob_actual_below(lower_f - 0.5, ...)` add a 0.5°F fudge factor to make the range inclusive. This is reasonable for integer-degree slots (where "78°F to 81°F" means any reading from 78.0 to 81.9), but the fudge amount is hardcoded and not documented in the function signature. If slots ever use decimal boundaries, the 0.5°F fudge would be incorrect.

---

## Cross-Cutting Concerns

### CC-1 — HIGH: P&L double-counting between `daily_pnl` table and positions `realized_pnl`
**Files:** `src/settlement/settler.py`, `src/web/app.py`, `src/portfolio/tracker.py`  
**Description:** This is the most operationally impactful cross-cutting issue. Settlement writes P&L into both:
1. `positions.realized_pnl` (via UPDATE at settler.py line 107)
2. `daily_pnl.realized_pnl` (via `_update_realized_pnl` at settler.py line 122)

The web dashboard then computes `total_realized` (app.py line 218) as `daily_pnl_val + sum(closed_pos realized_pnl)`. This double-counts every settled position's P&L. The circuit breaker uses `get_daily_pnl` (from `daily_pnl` table only), which is not double-counted, but the displayed dashboard value is incorrect.

### CC-2 — MEDIUM: Inconsistent use of `date.today()` vs `datetime.now(timezone.utc).date()`
**Files:** `src/strategy/rebalancer.py` (lines 603, 679), `src/weather/metar.py` (line 201), `src/weather/forecast.py` (line 43, 128 via `date.today()`), `src/markets/discovery.py` (line 65)  
**Description:** CLAUDE.md explicitly flags `date.today()` without timezone as a known pitfall. Multiple locations still use it. While most will work correctly in UTC-hosted containers, the inconsistency makes the codebase fragile to timezone changes.

### CC-3 — MEDIUM: No rate limiting or backoff on Gamma API calls
**Files:** `src/markets/discovery.py`, `src/web/app.py`, `src/strategy/rebalancer.py` (position check nested function)  
**Description:** Three separate code paths call the Gamma API (`/events`, `/markets`) without coordination. During a rebalance + position check overlap at the 60-minute boundary, up to 4 separate Gamma API polling operations can fire simultaneously. There is no rate-limit tracking and no circuit breaker for external API failures.

### CC-4 — LOW: `TradeSignal.reason` set via `signal.reason = ...` — not enforced by constructor
**Files:** `src/strategy/rebalancer.py` (lines 541, 906–927), `src/markets/models.py` (line 67)  
**Description:** CLAUDE.md states "TradeSignal.reason: Always set before execution." The field has a default of `""` in the dataclass. Executor reads `signal.reason` directly (executor.py line 77). If any code path creates a `TradeSignal` and passes it to the executor without setting `.reason`, an empty string is silently accepted and stored. There is no assertion or validation.

### CC-5 — LOW: No connection pooling; each `httpx.AsyncClient` is created and closed per call
**Files:** Multiple: `src/weather/metar.py`, `src/weather/settlement.py`, `src/markets/discovery.py`, `src/weather/forecast.py`, `src/weather/historical.py`  
**Description:** Most async HTTP functions create a new `httpx.AsyncClient(timeout=...)` and close it on exit. This is correct (no leaked connections) but prevents connection reuse across sequential API calls to the same host. This matters for the backfill function, which calls METAR for 20+ cities sequentially. Using a shared client would reduce TLS handshake overhead.

---

## Summary Table

| File | CRITICAL | HIGH | MEDIUM | LOW |
|------|----------|------|--------|-----|
| `src/strategy/rebalancer.py` | 0 | 1 (R-1) | 3 (R-2, R-3, R-4) | 4 (R-5, R-6, R-7, R-8) |
| `src/strategy/evaluator.py` | 0 | 1 (E-1) | 1 (E-2) | 2 (E-3, E-4) |
| `src/strategy/sizing.py` | 0 | 0 | 0 | 1 (S-1) |
| `src/markets/discovery.py` | 0 | 0 | 1 (D-1) | 2 (D-2, D-3) |
| `src/markets/price_buffer.py` | 0 | 0 | 0 | 1 (PB-1) |
| `src/markets/clob_client.py` | 0 | 1 (C-1) | 0 | 1 (C-2) |
| `src/weather/forecast.py` | 0 | 0 | 1 (F-1) | 1 (F-2) |
| `src/weather/metar.py` | 0 | 0 | 0 | 1 (M-1) |
| `src/portfolio/risk.py` | 0 | 0 | 0 | 1 (Ri-1) |
| `src/settlement/settler.py` | 0 | 0 | 2 (Se-1, Se-2) | 1 (Se-3) |
| `src/web/app.py` | 1 (W-1) | 1 (W-2) | 2 (W-3, W-4) | 1 (W-5) |
| `src/config.py` | 0 | 0 | 1 (Cfg-1) | 1 (Cfg-2) |
| `src/portfolio/store.py` | 0 | 0 | 0 | 2 (St-1, St-2) |
| `src/portfolio/tracker.py` | 0 | 0 | 1 (T-1) | 0 |
| `src/scheduler/jobs.py` | 1 (Sch-1) | 0 | 0 | 0 |
| `src/strategy/calibrator.py` | 0 | 0 | 0 | 1 (Cal-1) |
| `src/weather/historical.py` | 0 | 0 | 0 | 1 (H-1) |
| **Cross-cutting** | 0 | 1 (CC-1) | 2 (CC-2, CC-3) | 2 (CC-4, CC-5) |
| **TOTAL** | **2** | **5** | **14** | **22** |

---

## Priority Recommendations

### Fix immediately (CRITICAL):
1. **Sch-1 / W-1**: Remove redundant `run_settlement_only()` from `_run_cycle` to eliminate double settlement; address bg-loop thread-safety in web.py.

### Fix before live deployment (HIGH):
2. **W-2 / CC-1**: Fix P&L double-counting in the dashboard `total_realized` computation.
3. **C-1**: Parallelize CLOB price fetching to avoid O(N) serial blocking calls.
4. **E-1**: Replace `_count` private attribute access with a public `sample_count` property.
5. **R-1**: Add eviction to `_recent_exits` dict to prevent unbounded growth.

### Fix in next sprint (MEDIUM):
6. **Se-2**: Improve settlement label matching to include date component.
7. **E-2**: Fix no-forecast path in `evaluate_exit_signals` to not generate zero-EV sells.
8. **Cfg-1**: Add config schema validation to catch YAML typos at startup.
9. **R-4**: Fix global exposure accumulation bug for multi-event same-strategy cycles.
