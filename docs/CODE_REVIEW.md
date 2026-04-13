# Code Review Report — Polymarket Weather Trading Bot

**Reviewer**: Independent Code Auditor (Claude)
**Date**: 2026-04-14
**Scope**: Full `src/` module review
**Commit base**: `2f607d9` (claude/determined-mcclintock)

---

## Executive Summary

The codebase is well-structured for a solo-developer trading bot with a clear separation of concerns. The strategy logic, weather data integration, and portfolio management are thoughtfully designed. However, several issues need attention — particularly around **race conditions in concurrent scheduling**, **missing error recovery in financial operations**, and **edge cases in settlement P&L computation**. No critical security vulnerabilities were found, but there are a few hardening opportunities.

**Issue counts**: CRITICAL: 3 | HIGH: 9 | MEDIUM: 14 | LOW: 8

---

## Module Reviews

### 1. strategy/rebalancer.py

#### R-01: Race condition between rebalance and position check (CRITICAL)

**Lines**: 578 (run), 312 (run_position_check)
**Description**: `run()` (60-min rebalance) and `run_position_check()` (15-min settlement check) can execute concurrently via APScheduler. Both read and write to the same `_last_gamma_prices`, `_last_daily_maxes`, and `_cached_forecasts` dicts. Both call `_fetch_observations` and `evaluate_*` functions. Since Python dicts are not thread-safe for concurrent iteration + mutation, and both methods are async (potentially yielding mid-operation), this can cause:
- Position check seeing partially-updated prices from a concurrent rebalance
- Duplicate signal evaluation (both generate locked-win/exit signals)
- Double execution of the same trade signals

**Impact**: Duplicate trades, inconsistent portfolio state, financial loss.
**Fix**: Add an `asyncio.Lock` to serialize rebalance and position check cycles:
```python
self._cycle_lock = asyncio.Lock()

async def run(self):
    async with self._cycle_lock:
        return await self._run_cycle()

async def run_position_check(self):
    async with self._cycle_lock:
        return await self._run_position_check_inner()
```
APScheduler's `max_instances=1` only prevents the *same* job from overlapping with itself, not different jobs from running concurrently.

---

#### R-02: `date.today()` used for circuit breaker and forecast fetch (HIGH)

**Lines**: 603, 679, 144
**Description**: `date.today()` returns the **server's local date** (UTC inside Docker). For US-facing markets, during UTC 00:00–08:00 the server's "today" is already the next day while all US cities are still on the previous day. This means:
- Circuit breaker checks `daily_pnl` for the wrong date (line 603)
- Dashboard forecast fetch (line 679) targets tomorrow's date for West Coast cities
- Backfill `today` parameter (line 144) misaligns with market dates

**Impact**: Circuit breaker may not fire when it should; forecast fetches may miss same-day data.
**Fix**: Use `datetime.now(timezone.utc).date()` consistently, or better yet, use the local date of the earliest-timezone configured city for financial operations.

---

#### R-03: `_recent_exits` dict grows unboundedly (MEDIUM)

**Lines**: 81, 550, 922
**Description**: `self._recent_exits: dict[str, datetime]` stores exit timestamps for cooldown enforcement. Entries are never cleaned up — after weeks of operation, thousands of stale token_ids accumulate.

**Impact**: Slow dict lookups (minor), memory waste.
**Fix**: Add cleanup in `_run_cycle`:
```python
cutoff = now - timedelta(hours=max_cooldown * 2)
self._recent_exits = {k: v for k, v in self._recent_exits.items() if v > cutoff}
```

---

#### R-04: Position check infers market_date from slot_label via regex (MEDIUM)

**Lines**: 444-462
**Description**: The position check attempts to parse the market date from `slot_label` using `re.search(r'on (\w+ \d+)\??$', ...)`. If the label format changes (e.g., Polymarket uses "Apr 5" instead of "April 5"), parsing silently fails and `days_ahead` defaults to 0. This causes future-market positions to be evaluated with same-day exit logic (tighter thresholds, force-exit triggers).

**Impact**: Premature exits on future-day positions.
**Fix**: Store `market_date` explicitly in the positions table instead of reverse-engineering from the label.

---

#### R-05: `backfill_today_observations` uses `import asyncio` inside method body (LOW)

**Lines**: 143, 151
**Description**: `import asyncio` appears inside the method body at line 143, after already being used implicitly. This is harmless but inconsistent — the module already imports `httpx` at the top level.

---

#### R-06: Duplicate Gamma price fetching logic (MEDIUM)

**Lines**: 355-394 (position check), 58-109 (web/app.py)
**Description**: The Gamma price fetch function is copy-pasted in three locations: `run_position_check`, `web/app.py::_fetch_gamma_prices`, and `_run_cycle`. Each has slightly different error handling and batch sizes. A change in the Gamma API response format would need to be fixed in three places.

**Impact**: Maintenance burden, risk of inconsistent behavior.
**Fix**: Extract into a shared `markets/gamma.py` utility.

---

### 2. strategy/evaluator.py

#### E-01: `evaluate_exit_signals` returns SELL even when `forecast is None` (HIGH)

**Lines**: 510-578
**Description**: When `forecast is None`, `win_prob` stays at 0.0 and `ev` stays at 0.0. The code falls through to the "EV is negative" branch at line 567, generating a SELL signal with `ev=0.0`. An EV of exactly 0 is not negative — it means break-even. Selling at break-even EV incurs taker fees, making it a net loss.

**Impact**: Unnecessary sells (with fee drag) when forecast data is temporarily unavailable.
**Fix**: Change the condition at line 567 to `if ev < 0` or skip when forecast is None:
```python
if forecast is None:
    logger.debug("EXIT skip (no forecast): %s slot %s", event.city, slot.outcome_label)
    continue
```

---

#### E-02: Trim signal uses entry price for EV but doesn't account for taker fee on sell (MEDIUM)

**Lines**: 326-329
**Description**: `evaluate_trim_signals` computes EV as `win_prob * (1 - price) - (1 - win_prob) * price` using entry price. However, selling also incurs a taker fee. The threshold `-config.min_trim_ev` doesn't account for this, so a position showing EV of -0.025 (just past the -0.02 threshold) triggers a sell that costs additional fees.

**Impact**: Marginal trims are net-negative after sell-side fees.
**Fix**: Include the sell-side fee in the trim threshold:
```python
sell_fee = _entry_fee_per_dollar(slot.price_no)
if ev < -(config.min_trim_ev + sell_fee):
```

---

#### E-03: `_slot_distance` returns 0 when both bounds are None (LOW)

**Lines**: 91-92
**Description**: If both `temp_lower_f` and `temp_upper_f` are None, the function falls through to `mid = slot.temp_midpoint_f` which returns 0.0, making `distance = abs(0.0 - forecast)`. This shouldn't happen in practice (discovery filters these out), but it's a defensive gap.

---

#### E-04: `day_ahead_ev_discount` divides instead of multiplying (MEDIUM)

**Lines**: 168
**Description**: `ev_threshold /= (config.day_ahead_ev_discount ** days_ahead)` — with `day_ahead_ev_discount=0.7` and `days_ahead=2`, this computes `ev_threshold / 0.49 = 2.04× threshold`. The intent is to require *higher* EV for future markets, which this achieves. However, the naming is confusing — "discount" suggests the threshold should be *lower*. And if someone sets `day_ahead_ev_discount > 1.0`, the threshold drops, which is the opposite of the safety intent.

**Impact**: Misconfiguration risk.
**Fix**: Add a config validation `assert 0 < day_ahead_ev_discount < 1.0` or rename to `day_ahead_ev_multiplier`.

---

### 3. strategy/sizing.py

#### S-01: Kelly formula uses signal-proportional sizing, not bankroll-proportional (LOW)

**Lines**: 55-65
**Description**: The comment block explains this is intentional (not traditional Kelly), which is good. However, the sizing does NOT cap `kelly_full` — a very high win probability can produce `kelly_full > 1.0`, making `size_usd > slot_cap`. The `min(size_usd, slot_cap)` at line 69 catches this, but the intermediate value is briefly > 100% of slot cap.

**Impact**: None (capped downstream), but confusing to readers.

---

### 4. markets/discovery.py

#### D-01: `_match_city` uses bidirectional substring matching (HIGH)

**Lines**: 74-78
**Description**: `city.name.lower() in event_lower or event_lower in city.name.lower()` can produce false matches. For example, if a configured city is named "Portland" and an event mentions "Portland, Maine" vs "Portland, Oregon" — both match. Or "Dallas" would match "Dallas-Fort Worth" but also a hypothetical "Dallas County" event.

**Impact**: Wrong city matched to event, leading to wrong forecast/station data used for signal evaluation and settlement.
**Fix**: Use stricter matching — require the city name to appear as a complete word or match the full city clause:
```python
if event_lower.startswith(city.name.lower()) or city.name.lower() == event_lower:
```

---

#### D-02: `_parse_date` defaults `year=1900` to current year (MEDIUM)

**Lines**: 63-69
**Description**: When the date format lacks a year (e.g., "April 5"), it defaults to the current year. At year boundaries (Dec 31 → Jan 1), a market for "January 2" parsed on December 31 would be interpreted as the *current* year's Jan 2 (past), not next year's.

**Impact**: Markets near year boundaries could be skipped or misclassified.
**Fix**: If parsed date < today - 180 days, add 1 year.

---

#### D-03: No deduplication of events across paginated Gamma API responses (LOW)

**Lines**: 97-236
**Description**: If the Gamma API returns the same event across pagination boundaries (unlikely but possible with concurrent updates), duplicate events would appear in the result list, leading to duplicate signal evaluation and potentially double-sized positions.

---

#### D-04: `slot_spread` filter checks `abs(1.0 - price_yes - price_no)` (LOW)

**Lines**: 181
**Description**: This checks the *market overround* (how much the book deviates from fair), not the bid-ask spread. A slot with `price_yes=0.60, price_no=0.60` has overround 0.20 (rejected) but the true bid-ask spread could be different. The naming `spread` is misleading.

---

### 5. markets/price_buffer.py

#### PB-01: TWAP `_evict` pops from front of list — O(n) per eviction (LOW)

**Lines**: 188-189
**Description**: `samples.pop(0)` is O(n) for a list. With `TWAP_MAX_SAMPLES=20` this is negligible, but if samples grow larger, `collections.deque` would be O(1).

---

#### PB-02: `cross_validate` silently passes `None` into merged dict (MEDIUM)

**Lines**: 155-157
**Description**: When `clob` is None but `gamma` is also None for a token, `merged[tid] = gamma` assigns `None`. Downstream code calling `apply_batch(merged)` would then call `self.update(tid, None)`, which would fail or produce nonsensical results.

**Impact**: Potential crash if both CLOB and Gamma are missing for a token.
**Fix**: Add `if gamma is not None:` before `merged[tid] = gamma`.

---

### 6. markets/clob_client.py

#### C-01: `get_prices_batch` fetches tokens sequentially (MEDIUM)

**Lines**: 146-160
**Description**: Each token is fetched one-by-one with `await self.get_midpoint(token_id)`. With 50+ tokens, this could take 25+ seconds. This blocks the event loop and delays signal evaluation.

**Impact**: Slow price refresh, stale prices used for decisions.
**Fix**: Use `asyncio.gather` with a semaphore for concurrent but rate-limited fetches.

---

#### C-02: Dry-run mode returns `success=False` (MEDIUM)

**Lines**: 88-89
**Description**: `OrderResult(order_id="dry_run", success=False, message="dry run — no positions recorded")`. Since `executor.py` only records fills when `result.success` is True, dry-run mode correctly prevents position recording. However, the executor logs "Order failed" for every dry-run signal, which is noisy and misleading.

**Impact**: Log noise, confusing dry-run output.
**Fix**: Either special-case dry-run in executor logging, or use a separate `result.dry_run` flag.

---

### 7. weather/forecast.py

#### F-01: Ensemble `predicted_low_f` is hardcoded as `mean - 15` (MEDIUM)

**Lines**: 102
**Description**: `predicted_low_f=ensemble_mean - 15` is a magic number. The low temperature isn't used for strategy decisions currently, but if it's exposed to the dashboard or future strategies, it's misleadingly arbitrary.

**Impact**: Dashboard shows inaccurate low temperatures.
**Fix**: Either fetch actual low from ensemble, or mark as `None` / "estimated".

---

#### F-02: NWS fallback also hardcodes low as `temp_f - 15` (LOW)

**Lines**: 93 (nws.py)
**Description**: Same issue as F-01 but in the NWS module. At least this is consistent, but both are arbitrary.

---

### 8. weather/metar.py

#### M-01: `DailyMaxTracker._maxes` uses `defaultdict(lambda: -999.0)` (LOW)

**Lines**: 140
**Description**: Using -999.0 as a sentinel means `get_max` has to check `if val != -999.0`. A `defaultdict(lambda: None)` with `max()` comparison would be cleaner and avoid magic numbers. The current implementation works correctly though.

---

### 9. weather/historical.py

#### H-01: Cache file written without atomic replacement (MEDIUM)

**Lines**: 257-265
**Description**: `cache_file.write_text(json.dumps(...))` writes directly to the file. If the process crashes mid-write, the cache file is corrupted and the next startup will fail with `json.JSONDecodeError`, silently falling through to the empty-error-handling path.

**Impact**: Corrupted cache leads to unnecessary API calls and potentially missing error distributions for the first cycle.
**Fix**: Write to a temp file, then `os.rename` (atomic on POSIX):
```python
import tempfile
tmp = cache_file.with_suffix('.tmp')
tmp.write_text(json.dumps(data))
tmp.rename(cache_file)
```

---

#### H-02: `_errors` list stores raw errors, no outlier filtering (LOW)

**Lines**: 36-38
**Description**: All historical forecast errors are stored raw. Extreme outliers (e.g., station reporting errors, data gaps) can skew the distribution. The calibrator and evaluator use this directly.

**Impact**: A single bad data point can shift the distribution enough to change trade decisions.
**Fix**: Consider winsorizing extreme values (e.g., clip to ±3σ after first pass).

---

### 10. portfolio/store.py

#### ST-01: No foreign key constraints between tables (LOW)

**Lines**: 11-101
**Description**: `positions.event_id` and `orders.event_id` have no foreign key relationship. `settlements.event_id` can reference non-existent events. This is by design (SQLite FK enforcement is off by default), but it means orphaned records can accumulate.

---

#### ST-02: `get_daily_pnl` accepts string, `PortfolioTracker.get_daily_pnl` converts date to string (MEDIUM)

**Lines**: 297-302 (store), 162-165 (tracker)
**Description**: The store's `get_daily_pnl` takes a date string, but the rebalancer calls `self._portfolio.get_daily_pnl(date.today())` (line 603 in rebalancer.py), passing a `date` object. The tracker wraps it with `.isoformat()`. This works, but the type mismatch between what the rebalancer passes and what the store expects creates a fragile interface.

---

#### ST-03: Settlement P&L update is not atomic (HIGH)

**Lines**: 250-255 (settler.py `_update_realized_pnl`)
**Description**: `_update_realized_pnl` does: `current = await store.get_daily_pnl(date_str)` then `new = (current or 0.0) + pnl` then `await store.upsert_daily_pnl(...)`. If two settlements complete in the same cycle (which they can — the `for event_id` loop processes multiple events), the second one reads the value before the first one's write is committed, causing the first settlement's P&L to be overwritten.

**Impact**: Realized P&L undercounted in daily_pnl table.
**Fix**: Use a SQL `UPDATE SET realized_pnl = realized_pnl + ?` atomic increment instead of read-modify-write.

---

### 11. settlement/settler.py

#### SET-01: Settlement processes ALL positions regardless of status (HIGH)

**Lines**: 95-113
**Description**: The `for pos in positions` loop at line 95 iterates over `event_positions[event_id]`, which was populated from `get_open_positions()` at line 37. However, between line 37 and line 95, positions may have been marked `'settled'` by the idempotency check at lines 56-68. The variable `positions` still holds the old snapshots where `status='open'`, so lines 96-108 compute P&L and update status for positions that were *already* settled by the idempotency block.

The `UPDATE positions SET status = 'settled' WHERE id = ?` at line 104 is harmless (idempotent), but the P&L computation at line 96 and the settlement record insertion at line 118-119 count the same positions twice.

**Impact**: Double-counted P&L in settlement records and daily_pnl.
**Fix**: Re-filter `positions` after the idempotency block:
```python
positions = [p for p in positions if p["status"] == "open"]
```

---

#### SET-02: `_resolve_yes_price` substring matching is ambiguous (HIGH)

**Lines**: 204-213
**Description**: `if slot_label in label or label in slot_label` uses bidirectional substring matching. If slot labels are "82°F to 84°F" and "82°F to 84°F?", the match works. But if two slots share a substring (e.g., "80°F" and "80°F to 82°F"), the first match wins, potentially returning the wrong settlement price.

**Impact**: Incorrect P&L computation — a winning position could be recorded as a loss, or vice versa.
**Fix**: Match on token_id instead of label. The settled market data contains `clobTokenIds` which can be matched directly to position token_ids:
```python
# In _fetch_settlement_outcome, return {token_id: resolved_price} instead of {question: yes_price}
```

---

#### SET-03: Settlement uses `date.today()` (not UTC-aware) for P&L date (MEDIUM)

**Lines**: 122
**Description**: `await _update_realized_pnl(store, date.today().isoformat(), total_pnl)` — same issue as R-02. In Docker/UTC, P&L is recorded under the next day's date for late-night (US time) settlements.

---

### 12. web/app.py

#### W-01: `_run_async` can deadlock if called from the background event loop (HIGH)

**Lines**: 33-37
**Description**: `_run_async` uses `asyncio.run_coroutine_threadsafe(coro, loop)` to run async code from Flask's synchronous handlers. If any coroutine awaited inside `_run_async` calls another function that itself tries to use `_run_async` (or the same background loop), it will deadlock — the future waits for the loop, but the loop is blocked waiting for the outer coroutine.

Currently this doesn't happen because all async calls are one-level deep, but it's a latent risk if the store or rebalancer methods are refactored.

**Impact**: Full dashboard hang requiring process restart.
**Fix**: Document the invariant clearly, or switch to an async web framework (e.g., Quart, which is Flask-compatible).

---

#### W-02: `/api/trigger` exposes rebalance execution to unauthenticated requests (HIGH)

**Lines**: 687-701
**Description**: The `/api/trigger` POST endpoint runs a full rebalance cycle, which in live mode places real orders spending real money. There is no authentication, rate limiting, or CSRF protection. Anyone who can reach port 5001 can trigger unlimited trade execution.

**Impact**: Unauthorized trade execution, potential fund drain.
**Fix**: Add at minimum:
- API key authentication (check `X-API-Key` header against config)
- Rate limiting (max 1 trigger per 5 minutes)
- Disable in live mode unless explicitly opted in

---

#### W-03: Dashboard double-counts realized P&L (MEDIUM)

**Lines**: 218-220
**Description**: `total_realized = (daily_pnl_val or 0.0) + sum(p["realized_pnl"] for p in closed_pos ...)`. The `daily_pnl_val` already includes settlement P&L (updated by `_update_realized_pnl`), and `closed_pos` realized_pnl includes positions closed by both settlement AND manual exit. If a position was settled (P&L in daily_pnl) and also has `realized_pnl` set, it's counted twice.

**Impact**: Overstated P&L on dashboard.
**Fix**: Use only one source — either aggregate from positions table or from daily_pnl, not both.

---

#### W-04: `_utc_to_beijing` hardcodes UTC+8 offset (LOW)

**Lines**: 152-171
**Description**: The function name and implementation assume Beijing time (UTC+8). This is a user-specific preference that should be configurable. If the bot is used by someone in a different timezone, all dashboard times are wrong.

**Fix**: Make the timezone offset configurable in `AppConfig` or use the browser's local timezone via JavaScript.

---

### 13. config.py

#### CFG-01: `load_config` passes raw YAML dict as `**kwargs` to dataclass (MEDIUM)

**Lines**: 157-171
**Description**: `StrategyConfig(**strategy_raw)` will crash with `TypeError` if the YAML file contains an unknown key. This makes config file upgrades brittle — adding a new field to the code without updating all deployed config files is safe (defaults kick in), but removing a field or typo-ing one in YAML causes a hard crash on startup.

**Impact**: Bot fails to start after config changes.
**Fix**: Filter unknown keys:
```python
valid_fields = {f.name for f in fields(StrategyConfig)}
strategy_raw = {k: v for k, v in raw.get("strategy", {}).items() if k in valid_fields}
```

---

#### CFG-02: No validation on config values (MEDIUM)

**Lines**: 23-48
**Description**: `StrategyConfig` accepts any values without validation. Setting `kelly_fraction=5.0`, `max_total_exposure_usd=-100`, or `daily_loss_limit_usd=0` would cause unpredictable behavior. The `calibration_confidence` clamping (mentioned in CLAUDE.md) is done in the calibrator, not at config load time.

**Impact**: Misconfiguration leads to unexpected trading behavior.
**Fix**: Add a `validate()` method or use `__post_init__` to check ranges.

---

### 14. execution/executor.py

#### EX-01: SELL signal uses `signal.price` (current market price) for shares calculation (MEDIUM)

**Lines**: 43
**Description**: `shares = size_usd / price` computes shares from the current market price. For SELL signals, `signal.suggested_size_usd` is 0 (not set by exit/trim evaluators), so `shares = 0 / price = 0`. The CLOB order is placed with `size=0`, which either fails or does nothing.

Looking more carefully: for SELL, the executor sends `signal.price` and `shares` to `place_limit_order`. In paper mode, this records a fill of 0 shares. In live mode, the py-clob-client likely rejects size=0 orders.

**Impact**: SELL/EXIT/TRIM signals are **not actually executed** in live mode because share size is 0.
**Fix**: For SELL signals, look up the held position's shares from the portfolio:
```python
if signal.side == Side.SELL:
    positions = await self._portfolio.get_open_positions_for_event(
        event_id=signal.event.event_id, strategy=signal.strategy)
    for pos in positions:
        if pos["token_id"] == signal.token_id:
            shares = pos["shares"]
            size_usd = shares * price
            break
```

---

### 15. scheduler/jobs.py

#### J-01: Initial rebalance runs before METAR backfill completes (LOW)

**Lines**: 55-61
**Description**: The startup rebalance is scheduled 5 seconds after `setup_scheduler()` returns. However, `backfill_today_observations()` is called before `setup_scheduler()` in `main.py` (line 81), so by the time the 5-second timer fires, backfill should be done. This is actually fine — listing for completeness.

---

### 16. weather/settlement.py

#### WS-01: `validate_station_config` type hint says `list[dict]` but receives `list[CityConfig]` (LOW)

**Lines**: 144
**Description**: The parameter type hint is `cities: list[dict]` but callers pass `list[CityConfig]`. The function handles both via `isinstance(city_cfg, dict)` check, but the type hint is misleading.

**Fix**: `cities: list[CityConfig | dict]`

---

### 17. backtest/engine.py

#### BT-01: Backtest doesn't account for net_pnl in `BacktestResult` fields (LOW)

**Lines**: 337-339
**Description**: `daily_pnls = [r.gross_pnl for r in day_results]` — the `max_daily_loss` and `max_daily_profit` fields use gross P&L, not net. The `net_pnl` and `net_roi_pct` fields in `BacktestResult` are always 0.0 because they're never set from `_run_day` results.

**Impact**: Backtest report understates the impact of fees.
**Fix**: Compute net metrics:
```python
total_fees = sum(r.fees_paid for r in day_results)
net_pnl = gross_pnl - total_fees
net_roi_pct = round(net_pnl / total_risked * 100, 2) if total_risked > 0 else 0
```

---

### 18. alerts.py

#### A-01: `asyncio.create_task` without awaiting or storing reference (MEDIUM)

**Lines**: 31
**Description**: `asyncio.create_task(self._send_webhook(...))` creates a fire-and-forget task. If the task raises an exception, Python >= 3.11 will log an "unhandled exception in task" warning. The task reference is immediately garbage-collected, which can cause the task to be cancelled before completion.

**Impact**: Webhook alerts silently lost; noisy warning logs.
**Fix**: Store a reference and add error handling:
```python
task = asyncio.create_task(self._send_webhook(level, message))
task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
```

---

## Cross-Cutting Concerns

### CC-01: No graceful degradation for SQLite connection failures (HIGH)

The `Store` class uses a single `aiosqlite.Connection` with `timeout=30.0`. If the database file is locked by another process (e.g., a backup script), all operations block for up to 30 seconds. There's no retry logic, connection pooling, or fallback. A 30-second block during a rebalance cycle means stale prices are used for all subsequent decisions.

**Fix**: Add retry with backoff on `sqlite3.OperationalError`, or use WAL mode:
```python
await self._db.execute("PRAGMA journal_mode=WAL")
```

### CC-02: Private attribute access on `ForecastErrorDistribution` (LOW)

Multiple modules access `error_dist._count`, `error_dist._errors`, etc. These are implementation details. If the class is refactored, all callers break.

**Fix**: Add public properties: `@property count`, `@property errors`.

### CC-03: No request timeout differentiation (LOW)

All HTTP clients use the same timeout (15-30s). The Gamma API, NWS, Open-Meteo, and aviationweather.gov have very different response time profiles. A blanket 30s timeout means slow APIs hold up the entire rebalance cycle.

**Fix**: Configure per-API timeouts based on observed latency profiles.

---

## Summary Table

| ID | Module | Severity | Summary |
|----|--------|----------|---------|
| R-01 | rebalancer | **CRITICAL** | Race condition between rebalance and position check |
| SET-01 | settler | **CRITICAL** | Double-counted settlement P&L |
| EX-01 | executor | **CRITICAL** | SELL signals send 0 shares — exits never execute |
| E-01 | evaluator | HIGH | SELL on ev=0 when forecast unavailable |
| D-01 | discovery | HIGH | Ambiguous city substring matching |
| SET-02 | settler | HIGH | Ambiguous slot label matching for settlement |
| W-01 | web/app | HIGH | Background event loop deadlock risk |
| W-02 | web/app | HIGH | Unauthenticated `/api/trigger` endpoint |
| CC-01 | store | HIGH | No SQLite degradation handling |
| R-02 | rebalancer | HIGH | `date.today()` timezone issue |
| ST-03 | store | HIGH | Non-atomic P&L update race |
| W-03 | web/app | HIGH | Double-counted realized P&L on dashboard |
| R-03 | rebalancer | MEDIUM | Unbounded `_recent_exits` growth |
| R-04 | rebalancer | MEDIUM | Fragile market_date parsing from label |
| R-06 | rebalancer | MEDIUM | Duplicated Gamma fetch logic |
| E-02 | evaluator | MEDIUM | Trim doesn't account for sell-side fee |
| E-04 | evaluator | MEDIUM | Confusing `day_ahead_ev_discount` semantics |
| D-02 | discovery | MEDIUM | Year-boundary date parsing bug |
| PB-02 | price_buffer | MEDIUM | None passed through cross_validate |
| C-01 | clob_client | MEDIUM | Sequential token price fetching |
| C-02 | clob_client | MEDIUM | Dry-run logs as "failed" |
| F-01 | forecast | MEDIUM | Hardcoded predicted_low_f = mean-15 |
| H-01 | historical | MEDIUM | Non-atomic cache file write |
| CFG-01 | config | MEDIUM | Unknown YAML keys crash startup |
| CFG-02 | config | MEDIUM | No config value validation |
| SET-03 | settler | MEDIUM | `date.today()` in settlement P&L |
| ST-02 | store | MEDIUM | Type mismatch in get_daily_pnl interface |
| A-01 | alerts | MEDIUM | Fire-and-forget task reference lost |
| E-03 | evaluator | LOW | Both bounds None edge case |
| S-01 | sizing | LOW | kelly_full can exceed 1.0 briefly |
| D-03 | discovery | LOW | No event deduplication |
| D-04 | discovery | LOW | Misleading "spread" metric |
| PB-01 | price_buffer | LOW | O(n) list pop(0) |
| M-01 | metar | LOW | Magic -999.0 sentinel |
| R-05 | rebalancer | LOW | Inline import |
| W-04 | web/app | LOW | Hardcoded Beijing timezone |
| WS-01 | settlement | LOW | Wrong type hint |
| ST-01 | store | LOW | No foreign key constraints |
| H-02 | historical | LOW | No outlier filtering in error dist |
| BT-01 | backtest | LOW | Net P&L not computed in backtest |
| CC-02 | cross-cutting | LOW | Private attribute access |
| CC-03 | cross-cutting | LOW | Uniform HTTP timeouts |

---

## Priority Recommendations

### Immediate (before next deploy)
1. **EX-01**: Fix SELL signal share computation — exits are currently broken
2. **R-01**: Add cycle lock to prevent concurrent rebalance + position check
3. **SET-01**: Fix double-counted settlement P&L

### Short-term (this week)
4. **W-02**: Add authentication to `/api/trigger`
5. **ST-03**: Use atomic SQL increment for P&L updates
6. **SET-02**: Match settlements by token_id, not label substring
7. **E-01**: Don't generate SELL on forecast=None (ev=0)
8. **CC-01**: Enable WAL mode for SQLite

### Medium-term
9. **R-02/SET-03**: Standardize timezone handling (always city-local for financial dates)
10. **D-01**: Strengthen city matching logic
11. **R-06**: Extract shared Gamma fetch utility
12. **CFG-01/CFG-02**: Add config validation
