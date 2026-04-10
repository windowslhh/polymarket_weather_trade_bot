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

## Strategy Design (as of 2026-04-11)
- **Pure NO trading**: Only BUY NO signals — no YES, no LADDER
- **4 strategy variants** (A-D) testing different dimensions:
  - A = Conservative far-distance, B = Locked-win aggressor, C = Close-range with high EV gate, D = Quick exit
- **Signal types**: NO (forecast-based entry), LOCKED (daily_max > slot upper → guaranteed win), EXIT (3-layer hybrid), TRIM (EV decay)
- **Auto-calibrated distance**: Per-city threshold from historical forecast error distribution (calibrator.py)
- **Locked-win signals**: When observed daily max exceeds slot upper bound, NO is guaranteed → full Kelly sizing
- **Hybrid exit**: Layer 1 (locked-win protection) → Layer 2 (EV-based hold/sell) → Layer 3 (pre-settlement force exit)
- **Exit cooldown**: After EXIT, same token_id blocked for `exit_cooldown_hours` to prevent BUY→EXIT churn
- **15-minute position check**: Lightweight cycle (METAR only, no market discovery) for urgent locked-win and exit signals
- **TradeSignal.is_locked_win**: Formal bool field — do NOT use private `_is_locked_win` attribute
- **TradeSignal.reason**: Always set before execution — executor reads `signal.strategy` and `signal.reason` directly (no getattr)

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
