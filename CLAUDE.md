# Project: Polymarket Weather Trading Bot

## Security
- Never read, delete, or modify .env files or private keys without explicit user confirmation
- Never hardcode secrets in source code
- Always preserve original values before any credential changes

## Code Quality
- After implementing any fix or feature, do a self-audit checking for: cascading bugs, hardcoded test values, incorrect data types, edge cases with None values, SQL query correctness
- Do NOT wait for the user to request an audit — do it proactively
- Run `python -m pytest tests/ --ignore=tests/dry_run_offline.py --ignore=tests/run_backtest_offline.py -q` after every code change
- Verify all financial calculations against known reference values

## Trading Bot Development
- SQL queries must have proper GROUP BY, time windows, and NULL handling
- All currency values must be floats, not strings
- Never leave dry-run/test data in production databases
- Settlement detection: ONLY trigger on Gamma API `closed=true`, never on partial price resolution
- Position sizing: track city exposure cumulatively across all events in a rebalance cycle
- Skip already-held token IDs to prevent duplicate positions

## Workflow
- Always `git pull` and verify latest code before making changes
- Use `python` (not `python3`) — this project uses a venv with python3.11
- VPS deployment: `cd /opt/weather-bot && git pull && docker compose up -d --build`
- After VPS deploy, verify with: `curl -s http://198.23.134.31:5001/api/status | python3 -m json.tool`

## Architecture
- Entry point: `src/main.py` (--paper / --dry-run / live modes)
- Web dashboard: Flask on port 5001 (src/web/)
- Scheduler: APScheduler — rebalance every 60min, settlement check every 15min
- Database: SQLite at data/bot.db (positions, orders, daily_pnl, settlements, decision_log, edge_history)
- Forecast chain: NWS → Ensemble(GFS/ICON/ECMWF) → Single model → Cache

## Known Pitfalls
- Gamma API returns outcomePrices/clobTokenIds as JSON strings, not lists — must json.loads()
- Market titles end with "?" — regex must handle trailing question mark
- Individual slots can resolve to 0/1 before the event closes (early slot confirmation) — this is NOT settlement
- NWS always returns Fahrenheit for US cities, confidence is hardcoded ±3.0°F
- Paper mode: CLOB returns empty prices, use Gamma prices as fallback for unrealized P&L
