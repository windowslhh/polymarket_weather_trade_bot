# Requirements — Polymarket Weather Trading Bot

## 1. Project Overview

The bot is an automated trading system for Polymarket weather temperature markets. It discovers active "highest temperature" events on Polymarket, evaluates expected value using empirical forecast error distributions derived from 2 years of historical data, generates NO-only trade signals, and executes orders while managing position risk through Kelly-criterion sizing, exposure caps, and a hybrid 3-layer exit strategy.

**Operating modes:**
- `--dry-run` — log signals, no fills recorded
- `--paper` — simulate fills, track positions and P&L in SQLite (default in Docker)
- live (no flag) — real orders via Polymarket CLOB API (requires `ETH_PRIVATE_KEY`)

---

## 2. Core Strategy

### 2.1 Signal Types

| Type | Description |
|------|-------------|
| **NO** | Buy NO token when forecast temperature is far from a slot boundary (main signal) |
| **LOCKED** | Buy NO when observed daily max already exceeds the slot's upper bound (guaranteed win) |
| **EXIT** | Sell a held NO position via 3-layer hybrid logic |
| **TRIM** | Sell when EV has decayed below the minimum trim threshold |

The bot trades **NO tokens only** — no YES buys, no ladder strategies.

### 2.2 Signal Evaluation: NO Entry

**Step 1 — Bias-corrected forecast**

The raw forecast is adjusted by the empirical mean error of the forecast source:

```
bias_corrected_forecast = raw_forecast - mean_forecast_error
```

`mean_forecast_error` is computed from 2 years of (forecast − actual) pairs per city.

**Step 2 — Distance pre-filter**

For a slot with bounds `[L, U]`, the distance from the bias-corrected forecast to the slot:

| Slot type | Distance formula |
|-----------|-----------------|
| Range `[L, U]` | `min(|forecast − L|, |forecast − U|)` |
| Open high `≥X` | `max(0, X − forecast)` |
| Open low `<X` | `max(0, forecast − X)` |

Signals are discarded if `distance < no_distance_threshold_f`.

The threshold is either fixed (`no_distance_threshold_f: 8`) or auto-calibrated per city using the k×std dynamic formula (see §2.5).

**Step 3 — Win probability**

If ≥30 samples exist in the city's empirical error distribution, use it directly:

```
win_prob = P(actual temperature puts NO in-the-money | forecast)
```

Fallback when <30 samples: normal approximation using `confidence_interval_f`.

**Post-peak observation boost** (same-day markets only, local hour ≥ 14):

After the daily peak heating window (14:00–17:00 local time), the observed `daily_max` becomes highly informative. The bot computes win probability from both the forecast and the daily max observation, and takes the more favorable estimate. Confidence tightens from ±3°F during the window to ±1.5°F after 17:00.

**Step 4 — Trend adjustment**

| Trend state | Effect on signal |
|-------------|-----------------|
| `BREAKOUT_UP` | Boost win probability for lower-bound slots (temperatures rising, NO on upper slots safer) |
| `BREAKOUT_DOWN` | Boost win probability for upper-bound slots |
| `SETTLING` | EV threshold ×1.5 (only high-confidence trades near settlement) |

**Step 5 — EV calculation**

```
ev = win_prob × (1 − price_no)
   − (1 − win_prob) × price_no
   − entry_fee
```

where `entry_fee = 0.0125 × 2 × price_no × (1 − price_no)` (Polymarket taker fee, probability-weighted for a round-trip).

**Step 6 — EV threshold with days-ahead discount**

```
ev_threshold_effective = min_no_ev / (day_ahead_ev_discount ^ days_ahead)
```

Example: D+1 requires EV ≥ `0.03 / 0.7 = 0.043`; D+2 requires ≥ `0.03 / 0.49 = 0.061`.

**Step 7 — Skip conditions**

- Token already held
- `price_no > max_no_price` (0.85 — avoids asymmetric loss at high price)
- `ev < ev_threshold_effective`
- Distance < threshold

### 2.3 Signal Evaluation: Locked-Win Entry

When `daily_max > slot.temp_upper_f`, the NO token is a guaranteed winner (daily max never decreases). The bot uses:

- Fixed `win_prob = 0.99`
- Full Kelly sizing (`locked_win_kelly_fraction: 1.0`)
- Per-slot cap: `max_locked_win_per_slot_usd: $10`
- Skip if `price_no > 0.90` (margin too thin after fees)
- Skip for open-ended `≥X` slots (daily max ≥ X means YES wins, not NO)

### 2.4 Signal Evaluation: Exit (3-Layer Hybrid)

**Layer 1 — Locked-win protection**
If `daily_max > slot.temp_upper_f`, the NO token is a guaranteed winner → never sell.

**Layer 2 — EV-based exit**
Re-evaluate win probability using current forecast and daily max. If re-computed EV < 0, sell. If EV ≥ 0, hold (Layer 3 may override).

The exit distance threshold (how close the forecast must be to the slot for Layer 2 to activate):

| Trend | Exit distance |
|-------|--------------|
| `STABLE` | `no_distance_threshold_f × 0.30` |
| `BREAKOUT_*` | `no_distance_threshold_f × 0.20` |
| default | `no_distance_threshold_f × 0.25` |

**Layer 3 — Pre-settlement force exit**
If `hours_to_settlement ≤ force_exit_hours (1.0)` AND distance < exit distance threshold → force SELL regardless of EV. Prevents resolution risk from ambiguous outcomes.

**Exit cooldown:** After an EXIT signal for a token, the same `token_id` is blocked from new BUY signals for `exit_cooldown_hours: 4.0` to prevent BUY→EXIT churn.

### 2.5 Distance Threshold Auto-Calibration

When `auto_calibrate_distance: true`, each city's threshold is computed from its 2-year empirical forecast error distribution:

| City type | Condition | Formula | Example |
|-----------|-----------|---------|---------|
| Accurate | `|mean_bias| < 1.5°F` AND `std < 2.5°F` | `max(3.0, 1.2 × std)` | Las Vegas: std=1.47 → **3.0°F** (floor) |
| Uncertain | otherwise | `min(15.0, 2.0 × std)` | Denver: std=3.59 → **7.2°F** |
| Fallback | <30 samples | fixed `no_distance_threshold_f` | 8°F |

Hard bounds: [3°F, 15°F].

### 2.6 Position Sizing

Half-Kelly criterion with signal-proportional scaling:

```
kelly_full = (win_prob × net_odds − (1 − win_prob)) / net_odds
net_odds   = (1 − price_no) / price_no

# Normal signal
size = kelly_full × kelly_fraction × max_position_per_slot_usd

# Locked-win signal
size = kelly_full × locked_win_kelly_fraction × max_locked_win_per_slot_usd
```

Applied exposure caps (in order):
1. Per-slot cap (`max_position_per_slot_usd: $5` or `max_locked_win_per_slot_usd: $10`)
2. Per-city remaining capacity (`max_exposure_per_city_usd: $50` per strategy)
3. Global remaining capacity (`max_total_exposure_usd: $1000` per strategy)
4. Minimum order: $0.10 (rounds to $0 if below)

### 2.7 Strategy Variants (A–D)

Four variants run in parallel as independent portfolios, each overriding specific config values:

| Variant | Description | Key overrides |
|---------|-------------|---------------|
| **A** — Conservative Far | Far-distance, fewer trades | `max_no_price=0.70`, `kelly=0.5`, `min_no_ev=0.05`, `max_positions=3` |
| **B** — Locked Aggressor | Aggressive locked-win sizing | same as A + `locked_win_kelly=1.0`, `max_positions=6` |
| **C** — Close-Range High EV | Tight distance, high EV gate | `max_no_price=0.75`, `kelly=0.3`, `min_no_ev=0.06`, city cap=$25 |
| **D** — Quick Exit | Aggressive risk management | `force_exit_hours=2.0`, `exit_cooldown=2.0` |

Exposure caps are enforced independently per variant. All 4 variants can simultaneously hold up to `max_total_exposure_usd` each.

### 2.8 Risk Controls

| Control | Value | Scope |
|---------|-------|-------|
| Daily loss circuit breaker | $50 | Per bot instance |
| Max positions per event | 4 (variant A/C/D), 6 (B) | Per event per strategy |
| Max USD per slot | $5 normal, $10 locked | Per signal |
| Max city exposure | $50 (A/B/D), $25 (C) | Per city per strategy |
| Max total exposure | $1000 | Per strategy |
| Max days ahead | 2 | Prevents trading unreliable D+3 forecasts |
| Max NO price | 0.85 (0.70/0.75 by variant) | Avoids asymmetric risk |
| Exit cooldown | 4h (2h variant D) | Prevents BUY→EXIT churn |

---

## 3. Data Sources

### 3.1 Weather Forecasts

**NWS (National Weather Service)** — `api.weather.gov`
- Official US government source; same data Polymarket uses for settlement
- Fetches gridpoint forecast for city coordinates
- Confidence hardcoded at ±3.0°F (NWS does not publish uncertainty)

**Open-Meteo Ensemble** — `ensemble-api.open-meteo.com`
- Models: GFS Seamless, ICON Seamless, ECMWF IFS 0.25°
- Ensemble mean = consensus forecast; ensemble std = `confidence_interval_f`
- Inter-model spread computed for diagnostics

**Combination**: If both NWS and Ensemble are available, the bot uses a 50/50 weighted average. Ensemble std is used as the confidence interval (data-driven, not hardcoded).

**Fallback chain:** NWS → Ensemble → Single model (Open-Meteo) → Last cached value

### 3.2 Historical Forecast Errors

**Open-Meteo Archive API** — `archive-api.open-meteo.com`
- 2-year daily actual high temperatures

**Open-Meteo Historical Runs API** — `previous-runs-api.open-meteo.com`
- Archived forecast values for the same dates
- Computes `error[i] = forecast[i] − actual[i]` for each day
- Cached in `data/history/<city>.json`, refreshed every ~7 days

### 3.3 Real-Time Observations

**aviationweather.gov METAR** — `https://aviationweather.gov/api/data/metar`
- METAR observations from airport ASOS/AWOS stations
- Same underlying stations Polymarket uses for settlement (via Weather Underground)
- Polled at :57 and :03 past each hour (synchronized with METAR issuance times)
- Temperatures converted from Celsius to Fahrenheit

METAR stations used (matching Polymarket settlement):

| City | ICAO |
|------|------|
| New York | KLGA |
| Los Angeles | KLAX |
| Chicago | KORD |
| Houston | KIAH |
| Phoenix | KPHX |
| Dallas | KDFW |
| San Francisco | KSFO |
| Seattle | KSEA |
| Denver | KDEN |
| Miami | KMIA |
| Atlanta | KATL |
| Boston | KBOS |
| Minneapolis | KMSP |
| Detroit | KDTW |
| Nashville | KBNA |
| Las Vegas | KLAS |
| Portland | KPDX |
| Memphis | KMEM |
| Louisville | KSDF |
| Salt Lake City | KSLC |
| Kansas City | KMCI |
| Charlotte | KCLT |
| St. Louis | KSTL |
| Indianapolis | KIND |
| Cincinnati | KCVG |
| Pittsburgh | KPIT |
| Orlando | KMCO |
| San Antonio | KSAT |
| Cleveland | KCLE |
| Tampa | KTPA |

### 3.4 Polymarket APIs

**Gamma API** — `https://gamma-api.polymarket.com`
- Market discovery: events with `tag_slug=weather`, `active=true`, `closed=false`
- Price queries: `outcomePrices` and `clobTokenIds` (returned as JSON strings — must `json.loads()`)
- Settlement detection: `closed=true` on an event triggers P&L computation

**CLOB API** — via `py-clob-client`
- Order placement (limit orders for BUY, market orders for SELL)
- Requires `POLYMARKET_API_KEY` + `ETH_PRIVATE_KEY`
- In paper mode: CLOB prices are unavailable; Gamma prices used as fallback

---

## 4. Web Dashboard

Flask server on port 5001. All pages use a 5-second TTL cache to avoid thrashing the database.

### 4.1 Pages

**`/` — Dashboard**
- Bot mode (paper / live / dry), total exposure vs limit
- Unrealized P&L, active events count
- Per-strategy (A/B/C/D) exposure and realized P&L
- Latest signals table (city, slot, EV, win%, size)
- Open positions summary
- Forecast trends per city
- Daily loss limit meter
- Recent decision log (last 8 entries)

**`/temperatures` — Temperature Dashboard**
- One card per active city
- Local city time (IANA timezone)
- Daily max temperature curve (METAR observation series)
- Forecast predicted high with ±confidence band
- Avg Bias (mean forecast error from 2-year history)
- Reliability tier: A (std <1.5°F), B (1.5–2.5°F), C (2.5–4°F), D (>4°F)
- Refreshes via `GET /api/temperatures` every 30 seconds

**`/positions` — Positions Dashboard**
- Open positions grouped by strategy (A/B/C/D)
- Per-position: entry price, current price (30s polling via `/api/prices`), unrealized P&L
- Entry reason (`buy_reason` column)
- City exposure summary
- Closed/settled positions with realized P&L and exit reason

**`/trades` — Trade Timeline**
- Unified BUY / SELL / SKIP timeline (most recent first)
- Per-strategy P&L, exposure, open/settled counts

**`/analytics` — Edge Analytics**
- Edge snapshot history (`edge_history` table)
- EV and win probability distribution over time

**`/history` — P&L History**
- Daily P&L snapshots
- Settlement records per event

**`/config` — Configuration**
- Read-only view of active `config.yaml` strategy and scheduling parameters

### 4.2 API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Bot mode, exposure, unrealized P&L, signal count, last run, trends |
| `/api/prices` | GET | Current Gamma prices for open position token IDs (30s TTL) |
| `/api/temperatures` | GET | Observation series, forecasts, daily maxes, error dists (30s TTL) |
| `/api/trigger` | POST | Manually trigger a full rebalance cycle (120s timeout) |

---

## 5. Scheduling

| Job | Interval | Description |
|-----|----------|-------------|
| Full rebalance | Every 60 min | Market discovery, forecasts, signal generation, order execution, settlement check |
| Position check | Every 15 min | METAR refresh + locked-win/exit signals (no market discovery) |
| METAR refresh | Cron :57 and :03 | Sync DailyMaxTracker with latest airport observations |
| Startup run | 5s after start | One full rebalance on startup |

---

## 6. Deployment

### 6.1 Docker

```yaml
# docker-compose.yml
services:
  bot:
    build: .
    container_name: weather-bot
    restart: unless-stopped
    ports:
      - "5001:5001"
    volumes:
      - ./data:/app/data     # Persist SQLite DB + forecast cache
      - ./.env:/app/.env:ro  # Inject secrets (read-only)
    environment:
      - TZ=UTC
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "3"
```

Default command: `python -m src.main --paper -v` (paper trading, verbose).

### 6.2 Environment Variables (`.env`)

| Variable | Required | Description |
|----------|----------|-------------|
| `POLYMARKET_API_KEY` | Live only | Polymarket API key |
| `ETH_PRIVATE_KEY` | Live only | Ethereum private key for signing orders |
| `ALERT_WEBHOOK_URL` | Optional | Webhook URL for trade alerts |

### 6.3 VPS Deployment

```bash
cd /opt/weather-bot && git pull && docker compose up -d --build
```

Health check:
```bash
curl -s http://198.23.134.31:5001/api/status | python3 -m json.tool
```

### 6.4 Data Persistence

All persistent state lives in `data/`:
- `data/bot.db` — SQLite database (positions, orders, P&L, settlements, decision log)
- `data/history/<city>.json` — Cached forecast error distributions (~7-day refresh)
