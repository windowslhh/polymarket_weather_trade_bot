# Go-Live Runbook

Operational checklist for: **Day 4 deploy → 24h $50 smoke → $200 live**.

Three independent parts.  Each is a tickable list.  Run top-to-bottom;
do not skip steps.  All commands assume:

- VPS: `root@198.23.134.31`
- Deploy dir: `/opt/weather-bot-new` (note `-new` suffix — FIX-07)
- Web port: `5002`
- Branch under deploy: `claude/agitated-maxwell-60a135`

---

## Part 1 — VPS Deploy Checklist

### 1.1 Pre-flight (laptop side)

- [ ] All Day 3 commits pushed to remote and the review-fix bundle
      `a6ddb89` is the tip:
      `git rev-parse HEAD` matches the deploy target
- [ ] Local pytest green:
      ```
      .venv/bin/python -m pytest tests/ \
          --ignore=tests/dry_run_offline.py \
          --ignore=tests/run_backtest_offline.py -q
      ```
      Expect `974 passed` (or higher)
- [ ] Secret scan clean:
      ```
      grep -rn "logger.*private_key\|logger.*eth_private\|print(.*secret" src/ tests/
      ```
      Expect: no output

### 1.2 SSH + state snapshot

- [ ] SSH in (uses sshpass + memory-stored creds):
      ```
      sshpass -p "<VPS_PASSWORD>" ssh -o StrictHostKeyChecking=no root@198.23.134.31
      ```
- [ ] Confirm target dir exists:
      ```
      ls -la /opt/weather-bot-new/docker-compose.yml
      ```
      Expect: file present
- [ ] Snapshot current container state:
      ```
      cd /opt/weather-bot-new
      docker compose ps
      docker compose logs --tail=50 weather-bot
      ```

### 1.3 Backup current DB

- [ ] ```
      cd /opt/weather-bot-new
      mkdir -p data/backups
      cp data/bot.db data/backups/bot-$(date -u +%Y%m%dT%H%M%S).db
      ls -la data/backups/ | tail -3
      ```
      Expect: new backup file > 0 bytes

### 1.4 Pull new code

- [ ] ```
      cd /opt/weather-bot-new
      git fetch origin
      git checkout claude/agitated-maxwell-60a135
      git pull origin claude/agitated-maxwell-60a135
      git rev-parse HEAD
      ```
      Expect: HEAD matches laptop's commit hash from 1.1

### 1.5 ⚠️ Volume ownership (FIX-M5 first deploy must do)

- [ ] Container runs as uid 1000.  If `data/` is owned by root from an
      earlier root-only deploy, the bot can't write the DB and preflight
      will `sys.exit(2)` in a restart loop:
      ```
      chown -R 1000:1000 /opt/weather-bot-new/data
      ls -la /opt/weather-bot-new/data | head -3
      ```
      Expect: owner column shows `1000  1000` (or a named user with that uid)

### 1.6 .env verification

- [ ] Required vars present (counts must all be 1):
      ```
      cd /opt/weather-bot-new
      grep -c "^TRIGGER_SECRET=" .env
      grep -c "^POLYMARKET_PRIVATE_KEY=\|^ETH_PRIVATE_KEY=" .env
      grep -c "^POLYGON_RPC_URL=" .env
      grep -c "^POLYMARKET_API_KEY=" .env
      grep -c "^POLYMARKET_SECRET=" .env
      grep -c "^POLYMARKET_PASSPHRASE=" .env
      ```
- [ ] `TRIGGER_SECRET` is **not empty** — Blocker #5 fix means an
      empty secret in live mode returns 503 from /api/admin/*:
      ```
      [ "$(grep ^TRIGGER_SECRET= .env | cut -d= -f2-)" != "" ] && echo OK || echo MISSING
      ```
      Expect: `OK`
- [ ] **D-2** (2026-04-26): `DASHBOARD_SECRET` is **not empty** — empty
      secret in live mode means the dashboard returns 503 on every
      page (fail-closed):
      ```
      [ "$(grep ^DASHBOARD_SECRET= .env | cut -d= -f2-)" != "" ] && echo OK || echo MISSING
      ```
      Expect: `OK`.  This is a separate secret from `TRIGGER_SECRET` —
      grant `DASHBOARD_SECRET` to reviewers who need read access; keep
      `TRIGGER_SECRET` to operators who can pause/unpause.  After
      cutover, log in via:
      ```
      # Browser:
      http://198.23.134.31:5002/login?secret=<DASHBOARD_SECRET>
      # → cookie set, subsequent /,/positions,/trades,etc. work

      # CLI:
      curl -H "X-Dashboard-Secret: <DASHBOARD_SECRET>" http://198.23.134.31:5002/api/status
      ```
- [ ] Permissions locked down:
      ```
      chmod 600 /opt/weather-bot-new/.env
      ls -la /opt/weather-bot-new/.env
      ```
      Expect: `-rw------- 1 root root`

### 1.7 Stop old container (graceful)

- [ ] ```
      cd /opt/weather-bot-new
      docker compose down
      ```
- [ ] Verify the shutdown was graceful (FIX-09).  In the logs printed
      by `down`, look for:
      ```
      Executor: waiting up to 30s for N in-flight trade(s) to drain
      ```
      or
      ```
      Executor has 0 in-flight
      ```
      followed by `Bot stopped.`
- [ ] If container died inside 10s with no "waiting" line →
      `stop_grace_period: 90s` did NOT take effect.  Verify the
      docker-compose.yml on disk has it (Blocker #1 fix):
      ```
      grep -A1 stop_grace_period docker-compose.yml
      ```
      and re-run `docker compose down` after correcting.

### 1.8 Rebuild from scratch

- [ ] ```
      cd /opt/weather-bot-new
      docker compose build --no-cache weather-bot
      ```
      Expect: `useradd --uid 1000 ... bot`, `HEALTHCHECK ...
      start-period=120s` lines in the output

### 1.9 Start

- [ ] ```
      docker compose up -d
      ```
- [ ] Wait 120 s for start_period to elapse (preflight + reconciler +
      historical distributions + first forecast batch):
      ```
      sleep 125
      ```

### 1.10 Health verification

- [ ] ```
      docker compose ps
      ```
      Expect: STATUS shows `Up N minutes (healthy)` — **not** `starting`
      and **not** `unhealthy`
- [ ] ```
      curl -s http://localhost:5002/api/status | python3 -m json.tool
      ```
      Expect: HTTP 200, JSON with non-null `last_run` field
- [ ] If `unhealthy` — read the startup log for the failure mode:
      ```
      docker compose logs --tail=200 weather-bot | \
          grep -iE "Preflight|Reconciler|sys.exit|FAIL|CRITICAL"
      ```
      Common causes:
      - `Preflight DB FAIL: db_not_writable` → step 1.5 was skipped
      - `Preflight CLOB FAIL` → wrong creds / no internet
      - `Reconciler MISMATCH` → DB/CLOB diverged; see FIX_NOTES.md runbook
      - `sys.exit(2)` looping → fix root cause; do NOT just restart

### 1.11 Volume ownership double-check (post-start)

- [ ] ```
      docker compose exec weather-bot ls -la /app/data | head -3
      ```
      Expect: `bot.db` owned by uid `1000` (or named `bot`)

### 1.11b Wallet address fingerprint (FIX-2P-8)

> **Important — two addresses, one funded** (Y8, 2026-04-26): On
> Polymarket every account has TWO addresses.  The **signer EOA** is
> the one derived from `ETH_PRIVATE_KEY` (what `c.get_address()`
> prints); it signs every order but **does not hold USDC**.  The
> **proxy wallet** is a contract derived deterministically from the
> signer EOA, displayed on the Polymarket frontend top-right
> ("Login wallet"), and is where USDC is actually deposited.  These
> two addresses are different on purpose; do NOT compare USDC balance
> against the EOA directly — it will always read 0 and trick you into
> thinking the bot is unfunded.

- [ ] Print the EOA address the bot has loaded from `ETH_PRIVATE_KEY`:
      ```
      docker compose exec weather-bot python -c \
        "from src.config import load_config; from py_clob_client.client import ClobClient as C; \
         from py_clob_client.clob_types import ApiCreds; \
         cfg = load_config(); \
         c = C('https://clob.polymarket.com', \
               key=cfg.eth_private_key, chain_id=137, \
               creds=ApiCreds(api_key=cfg.polymarket_api_key, \
                              api_secret=cfg.polymarket_secret, \
                              api_passphrase=cfg.polymarket_passphrase)); \
         print('signer EOA:', c.get_address())"
      ```
      Expect: a `0x…` address.

- [ ] **Cross-check on Polymarket frontend** (Y8): open the Polymarket
      site logged in with the same wallet → top-right avatar shows
      the **Login wallet** address.  This MUST equal the `signer EOA`
      printed above.  A mismatch means the bot is signing as a
      different wallet than the one the operator funded — STOP, do
      NOT flip live; investigate the `.env` key vs the wallet you
      actually use to deposit, restore the correct key, re-run from 1.6.

- [ ] **USDC balance check** (Y8 clarification): the USDC the bot will
      spend lives in the **proxy wallet** derived from the signer EOA,
      NOT in the signer EOA itself.  On the Polymarket frontend the
      USDC balance you see in the header IS the proxy wallet's
      balance — that's the right number to compare against the $250
      gate in step 3.2.  Do NOT paste the signer EOA into Polygonscan
      and conclude "no USDC" — that is expected (and correct).

### 1.12 Graceful-shutdown smoke test

- [ ] ```
      docker compose stop
      ```
- [ ] Read the very last log line for shutdown markers:
      ```
      docker compose logs --tail=40 weather-bot | grep -iE \
          "waiting|shutdown|drained|abandoning|exited"
      ```
      Expect at least one of: `waiting up to 30s for N in-flight`,
      `Executor has 0 in-flight`, `Bot stopped.`.  Do NOT expect any
      `abandoning` line — that means the 90s window was not enough
      and indicates a stuck job.
- [ ] If the stop took less than 10s → stop_grace_period not honoured;
      revisit step 1.7.

### 1.13 Restart for the smoke test window

- [ ] ```
      docker compose up -d
      sleep 125
      docker compose ps
      ```
      Expect: `healthy` again

### 1.14 Deploy exit gate

All five must be true before leaving the VPS:

- [ ] `docker compose ps` shows `healthy`
- [ ] `/api/status` returns 200
- [ ] `data/` owned by uid 1000 (inside container)
- [ ] graceful-stop emitted FIX-09 logs (no abandoning)
- [ ] `.env` is `-rw-------`

---

## Part 2 — $50 Smoke Test Monitoring (24h, Live Small-Money)

### 2.0 Config diff for the smoke window

Before flipping `--paper` off, edit `config.yaml` (commit OR keep as a
runtime override; do not push the change — keep it in the VPS config
only):

```yaml
strategy:
  daily_loss_limit_usd: 25            # tighter than prod 75
  locked_win_max_price: 0.90          # unchanged
  max_total_exposure_usd: 50          # cap aggregate at $50
  # Per-variant city caps (override get_strategy_variants)
  # B: max_exposure_per_city_usd → 8
  # C: max_exposure_per_city_usd → 8
  # D' (D): max_exposure_per_city_usd → 4
```

- [ ] `cat config.yaml | grep -E "daily_loss_limit|max_total_exposure|max_exposure_per_city"`
      shows the four lines above
- [ ] Live mode (drop `--paper`) — confirm in `docker-compose.yml`:
      ```
      grep "command:" docker-compose.yml
      ```
      Expect: `command: ["python", "-m", "src.main", "-v"]` (no `--paper`)
- [ ] `docker compose up -d --build`
- [ ] `sleep 125 && docker compose ps`  → `healthy`

### 2.1 Per-window check (every 2-4h × 24h)

#### Health

- [ ] `docker compose ps` — STATUS still `healthy`
- [ ] `curl -s http://localhost:5002/api/status` — `last_run` within
      the last 15 min (rebalance is hourly + 15-min position-check)
- [ ] No critical errors:
      ```
      docker compose logs --tail=200 weather-bot | grep -iE "ERROR|CRITICAL"
      ```
      Expect: empty, or only known noise (Gamma 422 retries are OK)

#### Data layer

- [ ] Hourly position growth:
      ```
      docker compose exec weather-bot sqlite3 /app/data/bot.db \
        "SELECT COUNT(*) FROM positions WHERE created_at > datetime('now', '-1 hour')"
      ```
      Expect: ≤ 5 (small-money cap)
- [ ] Variant distribution:
      ```
      docker compose exec weather-bot sqlite3 /app/data/bot.db \
        "SELECT strategy, COUNT(*), ROUND(COALESCE(SUM(realized_pnl),0),2) \
         FROM positions GROUP BY strategy"
      ```
      Expect: rows for `B`, `C`, `D` (no new `A` rows after Day 4 deploy
      timestamp); D-only rows must come from cities in
      {Los Angeles, Seattle, Denver}
- [ ] REJECT distribution last hour:
      ```
      docker compose exec weather-bot sqlite3 /app/data/bot.db \
        "SELECT reason, COUNT(*) FROM decision_log \
         WHERE cycle_at > datetime('now', '-1 hour') AND action='SKIP' \
         GROUP BY reason ORDER BY 2 DESC"
      ```
      Expect: known reason codes only (PRICE_DIVERGENCE / DAILY_MAX_*
      / DIST_TOO_CLOSE / CITY_NOT_IN_WHITELIST / etc.)
- [ ] **orders ↔ positions 1:1** (FIX-03 + FIX-05 invariant):
      ```
      docker compose exec weather-bot sqlite3 /app/data/bot.db \
        "SELECT 'pending', COUNT(*) FROM orders WHERE status='pending' UNION ALL \
         SELECT 'filled', COUNT(*) FROM orders WHERE status='filled' AND side='BUY' UNION ALL \
         SELECT 'positions_non_legacy', COUNT(*) FROM positions \
            WHERE source_order_id NOT IN ('legacy','')"
      ```
      Expect: `pending=0` (no orphans persisting > 60 min);
      `filled` BUY count == `positions_non_legacy` count
- [ ] **D+1/D+2 mismatch sanity** (FIX-01):
      ```
      docker compose exec weather-bot sqlite3 /app/data/bot.db \
        "SELECT city, market_date, forecast_date, win_prob, price_no \
         FROM edge_history \
         WHERE market_date != forecast_date \
            OR (win_prob > 0.9 AND price_no < 0.3) \
         ORDER BY cycle_at DESC LIMIT 20"
      ```
      Expect: 0 rows.  Any row = a Bug #1 regression — pause the bot
      via 2.2 and investigate before any further trading.
- [ ] **15-min TRIM activity** (FIX-02):
      ```
      docker compose logs --since 1h weather-bot | grep -iE "TRIM signal|TRIM \[" | tail -20
      ```
      Expect: at least one TRIM log line per held-position cycle that
      moves price stop ratio.  Zero TRIM over 4h with active positions
      = potential FIX-02 regression.

### 2.2 Operational drills (run each ONCE during the 24h)

#### Pause / unpause (FIX-11)

```
TS=$(grep ^TRIGGER_SECRET= /opt/weather-bot-new/.env | cut -d= -f2-)
curl -s -X POST -H "X-Trigger-Secret: $TS" \
    http://localhost:5002/api/admin/pause
```

- [ ] Response: `{"ok": true, "paused": true}`
- [ ] Wait one rebalance cycle (~60 min) or trigger via /api/trigger
- [ ] Logs show `Kill switch engaged ... BUYs suppressed`
- [ ] No new BUY rows in `positions` since pause timestamp
- [ ] TRIM/EXIT signals **still fire** for held positions (closing
      always allowed)

```
curl -s -X POST -H "X-Trigger-Secret: $TS" \
    http://localhost:5002/api/admin/unpause
```

- [ ] Response: `{"ok": true, "paused": false}`
- [ ] Next cycle BUYs resume

#### Reconciler restart (FIX-05)

- [ ] ```
      docker compose restart weather-bot
      sleep 125
      docker compose logs --tail=120 weather-bot | grep -iE "Reconciler|preflight"
      ```
      Expect at least one of these reconciler outcomes per pending
      orders row (or `no pending orders to reconcile` if none):
      `paper_mode_orphan` / `CLOB-filled orphan ... promoted to filled`
      / `is open on CLOB` / `marked failed (cancelled)` /
      `marked failed (unknown)` / `CLOB unreachable ... leaving pending`

#### Webhook delivery (FIX-M3)

- [ ] After the first real trade or operator-triggered rebalance:
      ```
      curl -s -X POST -H "X-Trigger-Secret: $TS" \
          http://localhost:5002/api/trigger
      ```
- [ ] Webhook (Discord/Telegram/Slack) receives the alerter ping
      within 120s
- [ ] Logs do **not** contain `Webhook delivery failed`

### 2.3 24h smoke-window exit gate

All four must be true before considering the smoke window passed:

- [ ] At least one BUY entry happened in the last 4h (across B/C/D')
- [ ] At least one full 15-min `--- Position check done ---` log cycle
      observed end-to-end
- [ ] **Zero** `sys.exit(2)` (preflight) or `sys.exit(3)` (reconciler
      mismatch) restarts during the 24h:
      ```
      docker compose logs weather-bot | grep -E "sys.exit|Reconciler MISMATCH" | wc -l
      ```
      Expect: `0`
- [ ] Aggregate realized + unrealized P&L within ±$10 of $0 (small
      drift expected on a $50 cap — large drift = config wrong)

---

## Part 3 — $200 Live Confirmation Gate

Run this list at the moment of cutover from $50 smoke to $200 live.
**Every checkbox must be ticked before flipping config.**

### 3.1 Code & config

- [ ] Branch tip recorded:
      ```
      git rev-parse HEAD > /tmp/golive_commit.txt && cat /tmp/golive_commit.txt
      ```
- [ ] Local pytest green (full):
      ```
      .venv/bin/python -m pytest tests/ \
          --ignore=tests/dry_run_offline.py \
          --ignore=tests/run_backtest_offline.py -q
      ```
- [ ] Container-side pytest green:
      ```
      docker compose exec weather-bot python -m pytest /app/tests/ \
          --ignore=/app/tests/dry_run_offline.py \
          --ignore=/app/tests/run_backtest_offline.py -q
      ```
- [ ] Secret scan empty:
      ```
      grep -rn "logger.*private_key\|logger.*eth_private\|print(.*secret" src/ tests/
      ```
      Expect: no output
- [ ] `config.yaml` switched to production values:
      - [ ] `daily_loss_limit_usd: 75`
      - [ ] `locked_win_max_price: 0.90`
      - [ ] `max_total_exposure_usd: 200`
      - [ ] B `max_exposure_per_city_usd: 20`
      - [ ] C `max_exposure_per_city_usd: 15`
      - [ ] D' (`D`) `max_exposure_per_city_usd: 10`
- [ ] Variant set is `{B, C, D}` only (no `A`):
      ```
      docker compose exec weather-bot python -c \
        "from src.config import get_strategy_variants; print(sorted(get_strategy_variants().keys()))"
      ```
      Expect: `['B', 'C', 'D']`
- [ ] Live mode confirmed:
      ```
      grep "command:" docker-compose.yml
      ```
      Expect: `command: ["python", "-m", "src.main", "-v"]`  (no `--paper`, no `--dry-run`)

### 3.2 VPS preparation

- [ ] `.env` is `-rw------- 1 root`:
      ```
      ls -la /opt/weather-bot-new/.env
      ```
- [ ] `TRIGGER_SECRET` non-empty:
      ```
      [ "$(grep ^TRIGGER_SECRET= /opt/weather-bot-new/.env | cut -d= -f2-)" != "" ] \
          && echo OK || echo FAIL
      ```
- [ ] DB backed up immediately before cutover:
      ```
      cp /opt/weather-bot-new/data/bot.db \
         /opt/weather-bot-new/data/backups/bot-precutover-$(date -u +%Y%m%dT%H%M%S).db
      ls -la /opt/weather-bot-new/data/backups | tail -3
      ```
- [ ] Disk headroom ≥ 5 GB:
      ```
      df -h /opt/weather-bot-new
      ```
- [ ] Port 5002 binding scope confirmed:
      ```
      ss -tlnp | grep ':5002'
      ```
      Expect: `127.0.0.1:5002` (loopback) OR (if `0.0.0.0:5002`) a
      reverse proxy with auth must front it
- [ ] Wallet USDC balance ≥ $250:
      verify on the Polymarket frontend header (NOT on Polygonscan
      against the signer EOA — see Y8 note in step 1.11b).  The $50
      buffer absorbs slippage + gas without ever risking a failed
      signed-tx queue.
      **Reconciliation** (FIX-2P-8 + Y8): the **Login wallet** address
      shown in the Polymarket top-right avatar MUST equal the `signer
      EOA` printed by step 1.11b.  USDC is held by the **proxy wallet**
      (a contract derived from the EOA, NOT the EOA itself) — the
      Polymarket UI shows the proxy wallet's balance.  Comparing USDC
      against the EOA address directly will always read 0 — that's
      expected and not a problem; only the EOA-vs-Login-wallet match
      is the gate.

### 3.3 Operator readiness

- [ ] Operator can recite the pause command without lookup:
      ```
      curl -s -X POST -H "X-Trigger-Secret: $TRIGGER_SECRET" \
          http://localhost:5002/api/admin/pause
      ```
- [ ] Operator knows `docker compose stop` is graceful (90s, FIX-09);
      `docker compose kill` is forced and unsafe (use only as last
      resort)
- [ ] Operator knows: on `sys.exit(3)` (reconciler mismatch alert), do
      NOT auto-restart.  Procedure: pull alert payload → check Polymarket
      frontend manually → reconcile DB by hand (`mark_order_failed` or
      manual position close) → only then restart.  See `FIX_NOTES.md`
      §"Runbook — Operator manual actions".
- [ ] Webhook channel verified receiving messages (test ping from 2.2)

### 3.4 First-day discipline

- [ ] Hour-0 to hour-6: operator hands-on on dashboard + logs the
      whole time.  Any unexpected ERROR/CRITICAL → pause.
- [ ] First BUY: open Polymarket frontend, confirm the position
      matches `positions` row for that token_id and strategy
- [ ] **Y10 fee-formula sanity check** (do this on the FIRST live BUY):
      pick a small NO buy ideally near price 0.50 to maximise the
      fee signal.  Open the Polymarket order receipt for that fill.
      Compute the expected fee using the canonical formula:
      ```
      expected_fee_usd = TAKER_FEE_RATE * price * (1 - price) * size_usd
                       = 0.05 * p * (1 - p) * size_usd
      ```
      For a 1-share buy at p=0.50, size_usd ≈ 0.50:
      ```
      expected_fee = 0.05 * 0.50 * 0.50 * 0.50 = $0.00625
      ```
      Pull the actual USDC delta from the Polymarket receipt (or query
      Polygonscan for the proxy wallet's USDC outflow on that tx).
      Compare:
      - Within ±10% of expected → fee formula correct (FIX-2P-2 in
        force on Polymarket's side too)
      - **2× higher** than expected → Polymarket's broker is applying
        the formula WITH a ×2 factor that we removed in FIX-2P-2.
        🚨 **EMERGENCY ROLLBACK** via 3.6.  All current EV calculations
        will be undercounting fee by half — the LOCKED_WIN cap and
        EV_BELOW_GATE filters are wrong, prepare to revert FIX-2P-2.
      - 4× higher → both the rate AND the ×2 factor were wrong; same
        rollback action.
      Record the actual fee in your operator log alongside the
      expected number — you only need to do this once per cutover, not
      on every trade.
- [ ] First EXIT or TRIM: confirm `positions.status='closed'`,
      `realized_pnl` populated, and `decision_log` has the matching
      reason; no orphan in `orders WHERE status='pending'`
- [ ] First settlement: confirm `settlements` row + correct
      `winning_outcome` + P&L flows into `daily_pnl`
- [ ] Days 1-3: dashboard audit every 2h.  Day 4+: every 6h.

### 3.5 Cutover

When every box above is ticked:

- [ ] ```
      cd /opt/weather-bot-new
      docker compose down
      docker compose build --no-cache weather-bot
      docker compose up -d
      sleep 125
      docker compose ps
      ```
- [ ] `docker compose ps` shows `healthy`
- [ ] First `--- Starting rebalance cycle ===` log line within 10 min
- [ ] Webhook receives the cutover-ping rebalance alert

If any of the above fails → execute the rollback below.

### 3.6 Rollback

```
# 1. Pause IMMEDIATELY
curl -s -X POST -H "X-Trigger-Secret: $TRIGGER_SECRET" \
    http://localhost:5002/api/admin/pause

# 2. Stop gracefully
docker compose stop

# 3. Restore the pre-cutover DB
cp /opt/weather-bot-new/data/backups/bot-precutover-<TIMESTAMP>.db \
   /opt/weather-bot-new/data/bot.db
chown 1000:1000 /opt/weather-bot-new/data/bot.db

# 4. Revert branch to the last known-good commit
git checkout <prev-good-commit>

# 5. Bring up the previous good state (paper mode for safety)
# Edit docker-compose.yml command back to ["python", "-m", "src.main", "--paper", "-v"]
docker compose up -d --build
sleep 125
docker compose ps   # → healthy
```

---

## Appendix — Quick-reference values

| Item | Value | Source |
|---|---|---|
| Branch | `claude/agitated-maxwell-60a135` | Day 3 review-fix tip |
| Web port | `5002` | docker-compose.yml |
| `stop_grace_period` | `90s` | Blocker #1 |
| HEALTHCHECK `start-period` | `120s` | 🟡 #2 |
| Container uid | `1000` | FIX-M5 |
| `daily_loss_limit_usd` (live) | `75` | FIX-17 |
| `daily_loss_limit_usd` (smoke) | `25` | This runbook |
| `locked_win_max_price` | `0.90` | FIX-17 |
| `max_total_exposure_usd` (live) | `200` | This runbook |
| `max_total_exposure_usd` (smoke) | `50` | This runbook |
| Variants | `{B, C, D'}` (no A) | FIX-17 |
| D' city whitelist | `{Los Angeles, Seattle, Denver}` | FIX-17 |
| TRIM cadence | 15 min | FIX-02 |
| Prune job | 03:00 UTC daily | FIX-13 |
| Reconciler probe tolerance | `0.01` price, `0.5` shares | H-3 |
