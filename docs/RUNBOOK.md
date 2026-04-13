# Runbook — Polymarket Weather Trading Bot

## 1. Quick Reference

| Task | Command |
|------|---------|
| Deploy to VPS | `cd /opt/weather-bot && git pull && docker compose up -d --build` |
| Health check | `curl -s http://198.23.134.31:5001/api/status \| python3 -m json.tool` |
| View live logs | `docker logs -f weather-bot` |
| Restart bot | `docker compose restart bot` |
| Smoke test | `.venv/bin/python tests/smoke_dry_run.py` |
| Run tests | `.venv/bin/python -m pytest tests/ --ignore=tests/dry_run_offline.py --ignore=tests/run_backtest_offline.py -q` |

---

## 2. Docker Deployment

### 2.1 First-Time Setup

```bash
# SSH to VPS
sshpass -p '<password>' ssh root@198.23.134.31

# Clone repo and configure
cd /opt
git clone <repo-url> weather-bot
cd weather-bot

# Create .env with secrets
cat > .env << 'EOF'
POLYMARKET_API_KEY=<your-key>
ETH_PRIVATE_KEY=<your-private-key>
ALERT_WEBHOOK_URL=<optional-webhook>
EOF

# Create data directory
mkdir -p data/history

# Build and start (paper mode by default)
docker compose up -d --build
```

### 2.2 Deploy Update

```bash
cd /opt/weather-bot
git pull
docker compose up -d --build
```

This rebuilds the image with the latest code and performs a rolling restart. SQLite data and forecast caches in `data/` are preserved via the volume mount.

### 2.3 Verify Deployment

```bash
# Check container is running
docker ps | grep weather-bot

# Health check via API
curl -s http://198.23.134.31:5001/api/status | python3 -m json.tool

# Expected output (paper mode):
# {
#   "mode": "paper",
#   "exposure": 0.0,
#   "unrealized": 0.0,
#   "active_events": 0,
#   "signal_count": 0,
#   "last_run": null,
#   "last_error": null,
#   "trends": {}
# }
```

After the startup job fires (5 seconds after container start), `active_events` and `last_run` will be populated.

---

## 3. Viewing Logs

### 3.1 Live Log Stream

```bash
docker logs -f weather-bot
```

### 3.2 Last N Lines

```bash
docker logs --tail 200 weather-bot
```

### 3.3 Logs from a Time Window

```bash
docker logs --since 1h weather-bot     # last hour
docker logs --since 2026-04-13 weather-bot  # since date
```

### 3.4 Log Rotation

Docker is configured with `json-file` logging, `max-size: 50m`, `max-file: 3`. Logs rotate automatically; up to 150 MB retained.

### 3.5 Key Log Patterns

| Pattern | Meaning |
|---------|---------|
| `Rebalance cycle started` | Full hourly rebalance beginning |
| `Discovered N markets` | Market discovery completed |
| `BUY NO ... ev=X win_prob=Y size=$Z` | Signal executed |
| `SELL ... exit_reason=...` | Exit signal executed |
| `Settlement detected: event_id=... pnl=` | Market settled |
| `Daily loss limit reached` | Circuit breaker tripped |
| `Failed to fetch NWS forecast` | NWS API down, falling back to ensemble |
| `METAR fetch failed for ICAO` | METAR unavailable for a city |

---

## 4. Restart Procedures

### 4.1 Normal Restart

```bash
docker compose restart bot
```

The container has `restart: unless-stopped`, so it automatically restarts on crash.

### 4.2 Full Stop and Start

```bash
docker compose down
docker compose up -d
```

### 4.3 Rebuild After Code Change

```bash
docker compose up -d --build
```

### 4.4 After Config Change (`config.yaml`)

The config file is baked into the image at build time. Always rebuild after editing:

```bash
docker compose up -d --build
```

### 4.5 After `.env` Change

`.env` is mounted read-only at runtime. A simple restart is sufficient (no rebuild needed):

```bash
docker compose restart bot
```

---

## 5. Switching Between Paper and Live Trading

### 5.1 Current Mode

Check the API:
```bash
curl -s http://198.23.134.31:5001/api/status | python3 -c "import sys,json; d=json.load(sys.stdin); print('mode:', d['mode'])"
```

Or check `docker-compose.yml`:
```bash
grep CMD Dockerfile
# CMD ["python", "-m", "src.main", "--paper", "-v"]   # paper mode
# CMD ["python", "-m", "src.main", "-v"]               # live mode
```

### 5.2 Switch to Live Trading

1. Ensure `.env` contains valid `POLYMARKET_API_KEY` and `ETH_PRIVATE_KEY`.
2. Edit `Dockerfile`, change the `CMD` line:

```dockerfile
# From:
CMD ["python", "-m", "src.main", "--paper", "-v"]
# To:
CMD ["python", "-m", "src.main", "-v"]
```

3. Rebuild and restart:
```bash
docker compose up -d --build
```

4. Verify:
```bash
curl -s http://198.23.134.31:5001/api/status | python3 -m json.tool
# "mode": "live"
```

### 5.3 Switch Back to Paper

Reverse the `CMD` change, rebuild, and restart.

---

## 6. Troubleshooting

### 6.1 Bot Not Starting

```bash
docker logs weather-bot --tail 50
```

Common causes:
- Missing `.env` file or missing required keys
- Port 5001 already in use: `lsof -i :5001`
- SQLite database locked: another process accessing `data/bot.db`

### 6.2 No Markets Discovered

```bash
curl -s http://198.23.134.31:5001/api/status | python3 -c "import sys,json; d=json.load(sys.stdin); print('active_events:', d['active_events'])"
```

If `active_events: 0` more than 15 minutes after startup:
- Check Gamma API is reachable: `curl -s "https://gamma-api.polymarket.com/events?tag_slug=weather&active=true&closed=false&limit=5" | python3 -m json.tool`
- Check logs for `discover_weather_markets` errors
- Verify `max_days_ahead` isn't filtering all events (no markets settling within N days)
- Check `min_market_volume` (500 USD) isn't filtering everything

### 6.3 No Signals Generated

The bot can run without generating signals if:
- All slots are within the distance threshold (forecast too close to slot boundaries)
- EV is below `min_no_ev` for all slots
- Daily loss circuit breaker is active (check `daily_loss_remaining` on dashboard)
- All events already have max positions open (`max_positions_per_event`)

Check the decision log on the dashboard (`/` page) — SKIP entries will show the reason.

### 6.4 Positions Page Shows `-` for P&L

The `/positions` page fetches prices from Gamma API on load. If Gamma is slow or the rebalancer cache is cold (bot just started):

1. Wait 15 minutes for the first position check to run and populate the cache
2. Or trigger a manual rebalance: `curl -s -X POST http://198.23.134.31:5001/api/trigger`

### 6.5 METAR Data Missing

If a city shows no temperature observations on `/temperatures`:
- Check METAR station is reachable: `curl -s "https://aviationweather.gov/api/data/metar?ids=KLGA&format=json"`
- Verify the ICAO code in `config.yaml` is correct
- METAR refresh runs at :57 and :03 — observations appear within a few minutes

### 6.6 Settlement Not Detected

Settlements only trigger when `closed=true` in the Gamma API response. If a market appears resolved but P&L is not updated:
- Individual slot prices resolving to 0/1 is NOT settlement
- Wait for the Gamma event to show `closed=true` (can take hours after last trading)
- Manually trigger settlement check: `curl -s -X POST http://198.23.134.31:5001/api/trigger`
- Check `settlements` table: `sqlite3 data/bot.db "SELECT * FROM settlements ORDER BY settled_at DESC LIMIT 10;"`

### 6.7 Database Lock Errors

The SQLite connection uses `timeout=30.0`. If lock errors persist:
- Check no other process is accessing `data/bot.db` directly
- Restart the container: `docker compose restart bot`
- If corruption is suspected: backup and reinitialize (see §7)

### 6.8 Gamma API HTTP 422

Caused by comma-joined token IDs in query params. The bot uses repeated params:
```
?clob_token_ids=id1&clob_token_ids=id2   ✓
?clob_token_ids=id1,id2                   ✗ → 422
```

If you see 422 errors in logs on `/api/markets` calls, update to use httpx's list-of-tuples format.

### 6.9 Strategy Variant Shows Wrong Exposure

Exposure caps are enforced independently per variant (A/B/C/D). Total combined exposure across all variants can reach up to 4× `max_total_exposure_usd`. This is by design — each variant is an independent portfolio.

---

## 7. Data Backup and Recovery

### 7.1 Backup Database

```bash
# On VPS
cp /opt/weather-bot/data/bot.db /opt/weather-bot/data/bot.db.backup.$(date +%Y%m%d)

# Copy to local machine
scp root@198.23.134.31:/opt/weather-bot/data/bot.db ./bot_backup.db
```

### 7.2 Backup Forecast Cache

```bash
# The history cache takes 2-5 minutes to rebuild from scratch
# Back up if you want to avoid that delay
tar -czf history_cache.tar.gz /opt/weather-bot/data/history/
```

### 7.3 Restore Database

```bash
# Stop the bot first to avoid lock conflicts
docker compose stop bot

# Restore
cp /opt/weather-bot/data/bot.db.backup.20260413 /opt/weather-bot/data/bot.db

# Restart
docker compose start bot
```

### 7.4 Reinitialize Database (Fresh Start)

```bash
docker compose stop bot
rm /opt/weather-bot/data/bot.db
docker compose start bot
# Bot will create a fresh bot.db on startup
```

This clears all positions, P&L history, and settlements. Forecast error cache (`data/history/`) is preserved.

---

## 8. Smoke Test (One Cycle, Real APIs, No Orders)

Run before deploying to verify the full cycle completes without errors:

```bash
cd /opt/weather-bot
.venv/bin/python tests/smoke_dry_run.py
```

This runs one complete rebalance cycle (market discovery, forecasts, METAR, signal generation) using real APIs but without placing any orders. Exits with code 0 on success.

---

## 9. Running Tests

```bash
# Standard test suite (excludes slow offline tests)
.venv/bin/python -m pytest tests/ \
    --ignore=tests/dry_run_offline.py \
    --ignore=tests/run_backtest_offline.py \
    -q

# Single test file
.venv/bin/python -m pytest tests/test_strategy.py -v

# Single test
.venv/bin/python -m pytest tests/test_locked_win.py::test_locked_win_guaranteed -v
```

---

## 10. Manual Operations

### 10.1 Trigger Immediate Rebalance

```bash
curl -s -X POST http://198.23.134.31:5001/api/trigger | python3 -m json.tool
# {"ok": true, "signals": 3}
```

Note: Rebalance can take 30–60 seconds (NWS and Gamma API calls). The endpoint has a 120-second timeout. If the rebalance is still running, the response includes `"note": "running in background"`.

### 10.2 Query Database Directly

```bash
# Open SQLite shell (stop bot first to avoid lock conflicts, or use read-only mode)
sqlite3 data/bot.db

# Open positions
SELECT city, slot_label, strategy, entry_price, size_usd, buy_reason
FROM positions WHERE status='open' ORDER BY created_at DESC;

# Recent settlements
SELECT city, strategy, winning_outcome, pnl, settled_at
FROM settlements ORDER BY settled_at DESC LIMIT 10;

# Today's P&L
SELECT * FROM daily_pnl WHERE date=date('now');

# Decision log
SELECT cycle_at, city, signal_type, action, reason, ev, strategy
FROM decision_log ORDER BY cycle_at DESC LIMIT 20;
```

### 10.3 Wipe Test Data (Dry-Run Positions)

If dry-run positions accidentally polluted the database:

```bash
sqlite3 data/bot.db "DELETE FROM positions WHERE buy_reason LIKE '%dry%' OR buy_reason LIKE '%test%';"
```

---

## 11. Monitoring Checklist

Daily checks:
- [ ] `docker ps` — container running
- [ ] `/api/status` — `last_run` within last 70 minutes
- [ ] `/` dashboard — `last_error: null`
- [ ] Daily loss meter — not close to $50 limit
- [ ] Active events count > 0 during trading hours

Weekly checks:
- [ ] Log rotation not consuming disk: `du -sh /var/lib/docker/containers/`
- [ ] Database size reasonable: `du -sh /opt/weather-bot/data/bot.db`
- [ ] Forecast cache fresh: `ls -la /opt/weather-bot/data/history/` (files should be <7 days old)
