# Project: Polymarket Weather Trading Bot

## Security
- Never read, delete, or modify .env files or private keys without explicit user confirmation
- Never hardcode secrets in source code
- Always preserve original values before any credential changes

## Code Quality
- After implementing any fix or feature, do a self-audit checking for: cascading bugs, hardcoded test values, incorrect data types, edge cases with None values, SQL query correctness
- Do NOT wait for the user to request an audit — do it proactively
- Run `.venv/bin/python -m pytest tests/ --ignore=tests/dry_run_offline.py --ignore=tests/run_backtest_offline.py -q` after every code change
- Verify all financial calculations against known reference values

## Trading Bot Development
- SQL queries must have proper GROUP BY, time windows, and NULL handling
- All currency values must be floats, not strings
- Never leave dry-run/test data in production databases
- Settlement detection: ONLY trigger on Gamma API `closed=true`, never on partial price resolution
- Position sizing: track city exposure cumulatively across all events in a rebalance cycle
- Skip already-held token IDs to prevent duplicate positions

## Strategy Design (as of 2026-04-24, FIX-17 retune)
- **Pure NO trading**: Only BUY NO signals — no YES, no LADDER
- **3 strategy variants** (B / C / D') after FIX-17 dropped A:
  - B = Locked-win aggressor (kelly=0.5, locked-kelly=1.0, `max_exposure_per_city_usd=20`)
  - C = Close-range with high EV gate (`max_no_price=0.75`, `min_no_ev=0.06`, `max_exposure_per_city_usd=25`)
  - D' = Quick-exit whitelisted (`min_no_ev=0.08`, `max_exposure_per_city_usd=10`, `city_whitelist={Los Angeles, Seattle, Denver}`)
- **Global defaults changed by FIX-17**: `daily_loss_limit_usd=75` (was 50), `locked_win_max_price=0.90` (was 0.95)
- **Signal types**: NO (forecast-based entry), LOCKED (daily_max > slot upper → guaranteed win), EXIT (3-layer hybrid), TRIM (EV decay)
- **Auto-calibrated distance**: Per-city threshold from historical forecast error distribution (calibrator.py)
- **Locked-win signals**: When observed daily max exceeds slot upper bound by ≥ `locked_win_margin_f`, NO is guaranteed → full Kelly sizing. **Three gates apply**: (1) hard price cap `StrategyConfig.locked_win_max_price` (default **0.90** after FIX-17, previously 0.95; tuneable via `config.yaml` without redeploy) blocks fee-dominated entries outright; (2) `ev > 0` safety net using win_prob 0.999 for below-slot locks / 0.99 for above-slot locks (Fix 2 split); (3) **PRICE_DIVERGENCE gate** (Bug #1 fix, 2026-04-18): reject when `|win_prob - market_price_no| > StrategyConfig.price_divergence_threshold` (default 0.50). Gate 3 means the **effective lower bound** on `price_no` for locked-win entries is `max(min_no_price, win_prob - threshold)` — with defaults that's ~0.499 for below-slot locks and ~0.489 for above-slot locks, *not* `min_no_price=0.20`. This is intentional: a "locked" signal with market price 0.30 means one of our inputs is wrong, and the divergence gate is the only defense against trading on stale/wrong-station daily_max. Do NOT try to "fix" the raised floor by relaxing the gate — that's what produced the Houston 04-17 blow-up. The cap was removed in Fix 2 (035d353) → **reinstated at 0.95 on 2026-04-17** after production showed 17/17 entries clustered at 0.997-0.9985 with EV ≈ $0.0008/share (paper→live slippage ≥1 tick = 0.001 ate the entire margin) → **tightened to 0.90 on 2026-04-24 (FIX-17)** since post-rollback data still saw EV ≈ 0 at 0.93+. See `docs/fixes/2026-04-17-lockedwin-price-cap-rollback.md`.
- **TRIM triple-gate** (fix 4 + Bug #3, 2026-04-18): a held NO is trimmed when ANY of (a) absolute: `current_ev < -min_trim_ev_absolute`, (b) relative: `current_ev < entry_ev × (1 - trim_ev_decay_ratio)`, or (c) price stop: `current_price_no <= entry_price_no × (1 - trim_price_stop_ratio)` fires. Rich entries get noise protection; hard EV reversals trip (a); market-price collapse with stale-EV-inputs trips (c). Chicago 80-81 TRIMs at 95% loss on 2026-04-15 motivated adding (c). Disable (c) by setting `trim_price_stop_ratio >= 1.0`.
- **Thin-liquidity per-city cap** (fix 5): Miami / San Francisco / Tampa / Orlando get `max_exposure_per_city_usd × thin_liquidity_exposure_ratio` (default 0.5) because their Gamma volume is a fraction of other cities — prevents MTM blow-ups.
- **Hybrid exit**: Layer 1 (locked-win protection) → Layer 2 (EV-based hold/sell) → Layer 3 (pre-settlement force exit)
- **Exit cooldown**: After EXIT, same token_id blocked for `exit_cooldown_hours` to prevent BUY→EXIT churn
- **15-minute position check**: Lightweight cycle (METAR only, no market discovery) for urgent locked-win and exit signals
- **TradeSignal.is_locked_win**: Formal bool field — do NOT use private `_is_locked_win` attribute
- **TradeSignal.reason**: Always set before execution — executor reads `signal.strategy` and `signal.reason` directly (no getattr)
- **decision_log REJECT sampling** (fix 3): up to 3 rejections per (strategy, event) are persisted with reason code (DAILY_MAX_ABOVE_LOWER / DAILY_MAX_IN_SLOT / DAILY_MAX_BELOW_UPPER / DIST_TOO_CLOSE / PRICE_INVALID / PRICE_TOO_LOW / PRICE_TOO_HIGH / EV_BELOW_GATE / PRICE_DIVERGENCE) for post-hoc "why nothing traded?" debugging.
- **Gate matrix (M2, 2026-04-20)**: Per-`SignalKind` gate ordering lives declaratively in `src/strategy/gates.py::GATE_MATRIX`. Each gate is a small class with a `check(ctx) -> GateResult | None` method; `evaluator.py` is a thin walker that iterates the matrix and short-circuits on first fire. To add a cross-cutting guard (new decision-log reason code, shared invariant), register one gate class and list it in the matrix entries that need it — both entry branches (FORECAST_NO + LOCKED_WIN) pick it up together, closing Bug #1's class of "added to one branch, forgotten on the other" regressions.

## Workflow
- Always `git pull` and verify latest code before making changes
- Use `.venv/bin/python` — this project uses a venv with python3.11
- VPS deployment: `cd /opt/weather-bot && git pull && docker compose up -d --build`
- After VPS deploy, verify with: `curl -s http://198.23.134.31:5001/api/status | python3 -m json.tool`
- Smoke test: `.venv/bin/python tests/smoke_dry_run.py` (one cycle, real APIs, no orders)

## Architecture
- Entry point: `src/main.py` (--paper / --dry-run / live modes)
- Web dashboard: Flask on port 5001 (src/web/)
- Scheduler: APScheduler — rebalance every 60min, settlement+position check every 15min
- Database: SQLite at data/bot.db (positions, orders, daily_pnl, settlements, decision_log, edge_history)
  - positions table: buy_reason / exit_reason columns track decision reasoning
  - settlements: unique index on (event_id, strategy) — INSERT OR IGNORE, no pre-SELECT needed
  - DB connection: timeout=30.0 to prevent lock contention hangs
- Forecast chain: NWS → Ensemble(GFS/ICON/ECMWF) → Single model → Cache
- Portfolio access: Rebalancer calls `self._portfolio.X()` delegate methods — never `self._portfolio._store.X()`
- Sizing: half-Kelly (normal) / full-Kelly (locked wins), variable `net_odds` = (1-price)/price

## Known Pitfalls
- Gamma API returns outcomePrices/clobTokenIds as JSON strings, not lists — must json.loads()
- Market titles end with "?" — regex must handle trailing question mark
- Individual slots can resolve to 0/1 before the event closes (early slot confirmation) — this is NOT settlement
- NWS always returns Fahrenheit for US cities, confidence is hardcoded ±3.0°F
- Paper mode: CLOB returns empty prices, use Gamma prices as fallback for unrealized P&L
- DailyMaxTracker uses UTC dates internally — tests must use `datetime.now(timezone.utc).date()`, not `date.today()`
- Calibrator confidence must be in [0.5, 0.99] — values outside are clamped automatically
- Locked-win price cap (`StrategyConfig.locked_win_max_price`, defined in `src/config.py`, surfaced in `config.yaml`):
  - Cap removed by Fix 2 (035d353) — let `ev > 0` gate filter alone.
  - Reinstated at 0.95 on 2026-04-17 after production showed paper→live slippage exceeding the razor-thin EV at 0.997+. Hard cap runs *in addition to* the `ev > 0` safety net; Fix 2's below/above-lock win_prob split (0.999 / 0.99) is preserved within [min_no_price, cap].
  - **Tightened to 0.90 on 2026-04-24 (FIX-17)** — post-rollback data still saw EV ≈ 0 at 0.93+. Current default is 0.90; 0.95 remains the documented historical value.
- `StrategyConfig.min_trim_ev` is legacy (fix 4) — `min_trim_ev_absolute` and `trim_ev_decay_ratio` are the active gates; legacy field retained only for older YAML configs
- Settlement ICAO drift: three common misconfigurations — Houston→KHOU (not KIAH), Dallas→KDAL (not KDFW), Denver→KBKF (not KDEN). `check_station_alignment()` runs on every startup and `sys.exit(2)`s on MISMATCH; bypass with `--skip-station-check` only in emergencies. See `src/weather/settlement.py:SETTLEMENT_STATIONS` for the full ground-truth list.
- Position-check cycle bypasses D1's discovery filter: the 15-min cycle builds `held_no_slots` from `rebalancer._last_gamma_prices` directly, so a cold-start Gamma 0 price can reach the strategy layer. `PriceStopGate` keeps an explicit `live_price <= 0 → skip` defensive guard for exactly this case — do NOT remove it without either reworking the 15-min cycle to re-use the discovery-filtered slots or confirming `evaluate_trim_signals` is never called from position-check.
- Decision_log REJECT sampling codes (section above) include `PRICE_INVALID` for completeness; after D1 discovery pre-filters zero/one NO prices, `PriceBoundsGate` is a defensive dead-code guard for the FORECAST_NO path and will not fire in practice.
