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

## Strategy Design (as of 2026-04-16)
- **Pure NO trading**: Only BUY NO signals — no YES, no LADDER
- **4 strategy variants** (A-D) testing different dimensions:
  - A = Conservative far-distance (kelly=0.5, locked-kelly=0.5), B = Locked-win aggressor (kelly=0.6, locked-kelly=1.0 — larger forecast-entry size than A too so B ≠ A even without locked-win fires), C = Close-range with high EV gate, D = Quick exit
- **Signal types**: NO (forecast-based entry), LOCKED (daily_max > slot upper → guaranteed win), EXIT (3-layer hybrid), TRIM (EV decay)
- **Auto-calibrated distance**: Per-city threshold from historical forecast error distribution (calibrator.py)
- **Locked-win signals**: When observed daily max exceeds slot upper bound by ≥ `locked_win_margin_f`, NO is guaranteed → full Kelly sizing. **Two gates apply**: (1) hard price cap `LOCKED_WIN_MAX_PRICE = 0.95` blocks fee-dominated entries outright; (2) `ev > 0` safety net using win_prob 0.999 for below-slot locks / 0.99 for above-slot locks (Fix 2 split). The 0.95 cap was removed in Fix 2 (035d353) then **reinstated 2026-04-17** after production showed 17/17 entries clustered at 0.997-0.9985 with EV ≈ $0.0008/share — paper→live slippage (≥1 tick = 0.001) was eating the entire margin. See `docs/fixes/2026-04-17-lockedwin-price-cap-rollback.md`.
- **TRIM dual-gate** (fix 4): a held NO is trimmed when EITHER the absolute gate (`current_ev < -min_trim_ev_absolute`) OR the relative gate (`current_ev < entry_ev × (1 - trim_ev_decay_ratio)`) fires. Rich entries get protection from noise; hard reversals still trip the absolute floor.
- **Thin-liquidity per-city cap** (fix 5): Miami / San Francisco / Tampa / Orlando get `max_exposure_per_city_usd × thin_liquidity_exposure_ratio` (default 0.5) because their Gamma volume is a fraction of other cities — prevents MTM blow-ups.
- **Hybrid exit**: Layer 1 (locked-win protection) → Layer 2 (EV-based hold/sell) → Layer 3 (pre-settlement force exit)
- **Exit cooldown**: After EXIT, same token_id blocked for `exit_cooldown_hours` to prevent BUY→EXIT churn
- **15-minute position check**: Lightweight cycle (METAR only, no market discovery) for urgent locked-win and exit signals
- **TradeSignal.is_locked_win**: Formal bool field — do NOT use private `_is_locked_win` attribute
- **TradeSignal.reason**: Always set before execution — executor reads `signal.strategy` and `signal.reason` directly (no getattr)
- **decision_log REJECT sampling** (fix 3): up to 3 rejections per (strategy, event) are persisted with reason code (DAILY_MAX_ABOVE_LOWER / DAILY_MAX_IN_SLOT / DAILY_MAX_BELOW_UPPER / DIST_TOO_CLOSE / PRICE_INVALID / PRICE_TOO_LOW / PRICE_TOO_HIGH / EV_BELOW_GATE / PRICE_DIVERGENCE) for post-hoc "why nothing traded?" debugging.

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
- Locked-win 0.95 price cap: removed by Fix 2 (035d353), **reinstated 2026-04-17** as `LOCKED_WIN_MAX_PRICE` constant in `src/strategy/evaluator.py`. The Fix 2 rationale (let `ev > 0` gate filter alone) failed in production — paper→live slippage exceeded the razor-thin EV at 0.997+. Hard cap now runs *in addition to* the `ev > 0` safety net; Fix 2's below/above-lock win_prob split (0.999 / 0.99) is preserved within [min_no_price, 0.95].
- `StrategyConfig.min_trim_ev` is legacy (fix 4) — `min_trim_ev_absolute` and `trim_ev_decay_ratio` are the active gates; legacy field retained only for older YAML configs
