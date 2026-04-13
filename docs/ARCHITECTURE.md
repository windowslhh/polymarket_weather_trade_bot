# Architecture — Polymarket Weather Trading Bot

## 1. System Architecture

```mermaid
graph TB
    subgraph External["External APIs"]
        GAMMA[Gamma API<br/>gamma-api.polymarket.com]
        CLOB[Polymarket CLOB<br/>py-clob-client]
        NWS[NWS<br/>api.weather.gov]
        OM_ENS[Open-Meteo Ensemble<br/>ensemble-api.open-meteo.com]
        OM_ARCH[Open-Meteo Archive<br/>archive-api.open-meteo.com]
        OM_RUNS[Open-Meteo Prev Runs<br/>previous-runs-api.open-meteo.com]
        METAR[aviationweather.gov<br/>METAR observations]
    end

    subgraph Scheduler["APScheduler Jobs"]
        REBAL_JOB[Rebalance<br/>every 60 min]
        POS_JOB[Position Check<br/>every 15 min]
        METAR_JOB[METAR Refresh<br/>:57 and :03]
    end

    subgraph Core["Core Engine (src/)"]
        DISC[markets/discovery.py<br/>Market Discovery]
        FORE[weather/forecast.py<br/>Multi-source Forecast]
        HIST[weather/historical.py<br/>Error Distributions]
        MET[weather/metar.py<br/>Observations + DailyMaxTracker]
        TREND[strategy/trend.py<br/>Trend Detection]
        CAL[strategy/calibrator.py<br/>Distance Calibration]
        EVAL[strategy/evaluator.py<br/>Signal Generation]
        SIZ[strategy/sizing.py<br/>Kelly Sizing]
        REB[strategy/rebalancer.py<br/>Orchestrator]
        EXEC[execution/executor.py<br/>Order Execution]
        SETL[settlement/settler.py<br/>Settlement Detection]
    end

    subgraph Storage["Persistence (SQLite)"]
        STORE[portfolio/store.py<br/>data/bot.db]
        CACHE[data/history/*.json<br/>Error dist cache]
    end

    subgraph Web["Web Dashboard (Flask :5001)"]
        WEBAPP[web/app.py]
        PAGES[/ /positions /temperatures<br/>/trades /analytics /history /config]
        APIEP[/api/status /api/prices<br/>/api/temperatures /api/trigger]
    end

    GAMMA --> DISC
    NWS --> FORE
    OM_ENS --> FORE
    OM_ARCH --> HIST
    OM_RUNS --> HIST
    METAR --> MET

    DISC --> REB
    FORE --> REB
    HIST --> REB
    MET --> REB
    TREND --> REB
    CAL --> REB
    EVAL --> REB
    SIZ --> REB

    REB --> EXEC
    EXEC --> CLOB
    EXEC --> STORE

    REB --> SETL
    SETL --> GAMMA
    SETL --> STORE

    STORE --> WEBAPP
    REB --> WEBAPP

    REBAL_JOB --> REB
    POS_JOB --> REB
    METAR_JOB --> REB

    WEBAPP --> PAGES
    WEBAPP --> APIEP
```

---

## 2. Module Reference

### 2.1 `src/main.py` — Application Lifecycle

Entry point. Handles CLI argument parsing, component initialization, and graceful shutdown.

**Startup sequence:**
1. Parse CLI flags (`--dry-run`, `--paper`, `--verbose`, `--no-web`, `--port`)
2. Load `config.yaml` + `.env`
3. Validate METAR station configuration against Polymarket settlement stations
4. Initialize SQLite database (create tables, run migrations)
5. Build historical forecast error distributions (loaded from `data/history/` cache)
6. Backfill today's METAR observations into `DailyMaxTracker`
7. Start APScheduler (rebalance, settlement, METAR jobs)
8. Start Flask web server in background thread
9. Wait for SIGINT/SIGTERM; shut down scheduler and web server

---

### 2.2 `src/config.py` — Configuration

Loads `config.yaml` and environment variables into typed dataclasses.

**Key dataclasses:**

| Class | Purpose |
|-------|---------|
| `CityConfig` | ICAO code, lat/lon, timezone per city |
| `StrategyConfig` | All trading parameters |
| `SchedulingConfig` | Job intervals |
| `AppConfig` | Top-level bundle + credentials |

**`get_strategy_variants() → dict[str, dict]`** returns parameter overrides for each of the 4 variants (A/B/C/D). The rebalancer applies these overrides to `StrategyConfig` at evaluation time.

---

### 2.3 `src/markets/` — Market Discovery and Models

#### `discovery.py`

`discover_weather_markets(cities, client, min_volume, max_spread, max_days_ahead)` → `list[WeatherMarketEvent]`

- Queries Gamma API: `GET /events?tag_slug=weather&active=true&closed=false`
- Parses event title with regex to extract city name and market date
- Matches city name against `cities` config (case-insensitive, partial)
- Parses temperature slot labels: "78°F to 81°F", "82°F or above", "Below 65°F"
- Filters by: market volume ≥ `min_volume`, spread ≤ `max_spread`, days_ahead ≤ `max_days_ahead`
- Days-ahead comparison uses city-local date (not UTC) to handle midnight UTC boundary

**Known Gamma API quirks handled here:**
- `outcomePrices` and `clobTokenIds` are JSON strings, not lists — `json.loads()` required
- Event titles end with "?" — regex includes trailing `?`

#### `models.py`

| Class | Key fields |
|-------|-----------|
| `TempSlot` | `token_id_yes`, `token_id_no`, `temp_lower_f`, `temp_upper_f`, `price_yes`, `price_no`, `spread` |
| `WeatherMarketEvent` | `event_id`, `city`, `market_date`, `slots: list[TempSlot]`, `volume`, `resolution_source` |
| `TradeSignal` | `token_type`, `side`, `slot`, `event`, `expected_value`, `win_prob`, `suggested_size_usd`, `strategy`, `reason`, `is_locked_win` |

`TradeSignal.reason` is always set before execution. `TradeSignal.is_locked_win` is a formal `bool` field (not a private attribute).

#### `price_buffer.py`

`PriceBuffer` — TWAP price smoothing with outlier filtering. Prevents single-point price spikes from triggering unnecessary signals. Maintains a rolling window per token.

#### `resolution.py`

Parses the `resolution_source` metadata field from Gamma event data to determine which weather station Polymarket uses for settlement. Returns a normalized string ("nws", "wunderground", "noaa", etc.).

---

### 2.4 `src/weather/` — Weather Data

#### `forecast.py`

Multi-source forecast with priority chain: NWS → Ensemble → Single model → Cache.

**`get_forecast(city, target_date, client)` → `Forecast`**

If both NWS and Ensemble are available: weighted 50/50 average. Ensemble std used as `confidence_interval_f`.

**`Forecast` dataclass:**
```python
city: str
forecast_date: date
predicted_high_f: float
predicted_low_f: float
confidence_interval_f: float  # ±°F (ensemble std when available, 3.0 for NWS-only)
source: str                   # "nws+ensemble(gfs_seamless,...)" etc.
fetched_at: datetime
ensemble_spread_f: float | None  # inter-model disagreement
model_count: int
```

#### `metar.py`

METAR observation fetching from `aviationweather.gov`.

**`DailyMaxTracker`** — Tracks daily maximum temperature per city:
- Groups observations by city-local date (uses IANA timezone per city)
- `update(observation)` — Record new temperature reading
- `get_max(icao)` → `float | None` — Current daily max
- `is_post_peak(icao, local_hour)` — True if past peak heating window (14–17 local)
- Uses UTC timestamps internally; date grouping is city-local

#### `historical.py`

Builds empirical forecast error distributions from 2-year historical data.

**`ForecastErrorDistribution`** — stores sorted `error[]` array and provides:
- `prob_no_wins(lower, upper, forecast)` → P(NO wins)
- `prob_actual_in_range(lower, upper, forecast)` → P(actual in slot)
- `prob_actual_above(threshold, forecast)` → P(actual ≥ threshold)
- `mean`, `std`, `sample_count` properties

**`build_distribution(city, client)`** — fetches 2-year archive + historical runs, computes `error[i] = forecast[i] − actual[i]`, caches JSON to `data/history/<city>.json`.

Example city characteristics (from 2-year data):

| City | Mean bias (°F) | Std (°F) | Threshold |
|------|---------------|----------|-----------|
| Las Vegas | −0.17 | 1.47 | 3.0 (floor) |
| Phoenix | +0.3 | 1.6 | 3.0 (floor) |
| Denver | +2.04 | 3.59 | 7.2 |
| Cleveland | +3.44 | 4.36 | 8.7 |

#### `settlement.py`

Maps city names to Polymarket's settlement station ICAO codes. Polymarket uses Weather Underground data from airport ASOS/AWOS stations — the same underlying source as METAR.

`validate_station_config(cities)` warns if any configured ICAO differs from the settlement ICAO (would cause systematic losses).

#### `http_utils.py`

Retry logic for weather API calls: exponential backoff, configurable max retries, timeout handling.

---

### 2.5 `src/strategy/` — Strategy Engine

#### `evaluator.py`

Core signal generation logic. All four signal types originate here.

**Entry signal flow:** distance filter → win probability → post-peak boost → trend adjustment → EV calculation → EV threshold → skip conditions

**Exit signal flow (3-layer):**
1. Locked-win protection check
2. EV re-evaluation using current forecast + daily max
3. Pre-settlement force exit if within `force_exit_hours`

**Trim signal flow:** Re-compute EV using entry price; trim if `EV < −min_trim_ev`.

#### `sizing.py`

Kelly sizing with signal-proportional scaling and exposure caps.

```
kelly_full = (win_prob × net_odds − (1 − win_prob)) / net_odds
size = kelly_full × fraction × slot_cap
```

Caps applied in sequence: per-slot → per-city → global → minimum viable ($0.10).

#### `trend.py`

`ForecastTrend` maintains rolling forecast history (last 24 readings per city).

`get_trend(city, hours_to_settlement)` → `TrendState`:
- `STABLE`: cumulative change < 1°F over last 3 readings
- `BREAKOUT_UP`: cumulative change ≥ 3°F
- `BREAKOUT_DOWN`: cumulative change ≤ −3°F
- `SETTLING`: hours_to_settlement ≤ 6 AND recent_delta < 1°F

#### `calibrator.py`

Dynamic per-city distance threshold calibration.

`calibrate_distance_dynamic(error_dist)`:
- Accurate cities (`|mean_bias| < 1.5` AND `std < 2.5`): `max(3.0, 1.2 × std)`
- Uncertain cities: `min(15.0, 2.0 × std)`
- Fallback (<30 samples): returns `DEFAULT_THRESHOLD_F = 8`

#### `rebalancer.py`

Main hourly orchestrator. Composes all subsystems into a complete rebalance cycle.

**`run()` — Full rebalance cycle:**
1. Discover markets (Gamma)
2. Fetch and blend forecasts (NWS + Ensemble)
3. Update ForecastTrend per city
4. Fetch METAR, update DailyMaxTracker
5. For each event: calibrate threshold, evaluate all signal types, size signals, filter by cooldown
6. Group signals by strategy variant; apply per-variant overrides and caps
7. Execute via Executor
8. Run settlement check
9. Update dashboard state

**`run_position_check()` — 15-minute lightweight cycle:**
- METAR refresh only (no market discovery or NWS calls)
- Evaluate locked-win and exit signals on existing positions

**`get_dashboard_state()`** — Returns a snapshot dict consumed by Flask templates and API endpoints.

---

### 2.6 `src/portfolio/` — Position and Risk Management

#### `store.py`

Async SQLite persistence layer (`aiosqlite`, timeout=30s).

**Key tables** (see §4 for full schema):
- `positions` — all trades (open / closed / settled)
- `orders` — CLOB order records
- `daily_pnl` — daily P&L snapshots
- `settlements` — market resolutions (unique per `event_id, strategy`)
- `decision_log` — full audit trail per cycle
- `edge_history` — per-slot EV/win probability snapshots

#### `tracker.py`

High-level portfolio API delegating to `store.py`.

Key methods:
- `record_fill(...)` — insert open position
- `close_positions_for_token(...)` — mark closed with P&L
- `get_held_no_slots(event_id, strategy, current_prices)` — reconstruct `TempSlot` list from held positions (parses `slot_label` for bounds)
- `get_total_exposure(strategy)` / `get_city_exposure(city, strategy)` — for sizing caps

#### `risk.py`

Circuit breaker: checks `daily_loss_limit_usd`. If today's realized loss exceeds the limit, blocks further BUY signals.

---

### 2.7 `src/execution/executor.py` — Order Placement

`execute_signals(signals: list[TradeSignal])`

For each signal:
- **BUY**: place limit order via CLOB at `slot.price_no`; call `portfolio.record_fill()` on success
- **SELL**: place market order; call `portfolio.close_positions_for_token()` with `exit_price` and `exit_reason`

In dry-run mode: log intent, return immediately.
In paper mode: log simulated fill, record position.
In live mode: call `py-clob-client`.

---

### 2.8 `src/settlement/settler.py` — Settlement Detection

`check_settlements(store)` — idempotent settlement processing:

1. Fetch open positions grouped by `event_id`
2. Query Gamma for resolved markets (`closed=true`)
3. Determine winning outcome: YES resolves to ≥0.99, NO resolves to ≤0.01
4. Compute P&L: `pnl = (exit_price − entry_price) × shares`
5. Update positions: `status=settled`, `exit_price`, `realized_pnl`
6. Insert settlement record: `INSERT OR IGNORE INTO settlements ...` (unique on `event_id, strategy`)
7. Update `daily_pnl`

Settlement outcome resolution handles Gamma's variable text labels via bidirectional substring matching.

**Important:** Settlement triggers ONLY on `closed=true` from Gamma API. Individual slot prices resolving to 0/1 (early slot confirmation) are NOT settlement events.

---

### 2.9 `src/scheduler/jobs.py` — APScheduler Configuration

```python
# Rebalance: every 60 min, max 1 concurrent instance
scheduler.add_job(rebalancer.run, 'interval', minutes=60, max_instances=1)

# Position check + settlement: every 15 min
scheduler.add_job(position_and_settle, 'interval', minutes=15, max_instances=1)

# METAR refresh: cron at :57 and :03 of each hour
scheduler.add_job(rebalancer.refresh_metar, 'cron', minute='57,3')

# Startup: fire once after 5-second delay
scheduler.add_job(rebalancer.run, 'date', run_date=startup_time + 5s)
```

---

### 2.10 `src/web/app.py` — Flask Dashboard

**Architecture:** Flask runs in a background thread. Async DB/API calls are dispatched onto a persistent background `asyncio` event loop via `asyncio.run_coroutine_threadsafe`. A 5-second TTL cache prevents repeated DB queries.

**Pages and endpoints:** see REQUIREMENTS.md §4.

**Price polling:** `/api/prices` has a 30-second TTL. The `/positions` page JavaScript polls this endpoint every 30 seconds to update unrealized P&L without a page reload.

**Temperature refresh:** `/api/temperatures` has a 30-second TTL. The `/temperatures` page polls to update METAR curves.

---

## 3. Key Data Flows

### 3.1 Market Discovery → Signal Generation → Execution

```
Gamma API
  → discover_weather_markets()
      → WeatherMarketEvent list (slots, prices, token IDs)
  → rebalancer.run()
      → get_forecast() [NWS + Ensemble blend]
      → ForecastTrend.update()
      → DailyMaxTracker.update() [METAR]
      → calibrate_distance_dynamic() [k×std per city]
      → evaluate_no_signals()        → TradeSignal (NO)
      → evaluate_locked_win_signals() → TradeSignal (LOCKED)
      → evaluate_exit_signals()       → TradeSignal (EXIT)
      → evaluate_trim_signals()       → TradeSignal (TRIM)
      → compute_size()               → size_usd per signal
      → exit cooldown filter
      → executor.execute_signals()
          → CLOB API (live) / simulated (paper)
          → portfolio.record_fill() or close_positions_for_token()
      → check_settlements()
          → Gamma API (closed=true check)
          → store.insert_settlement()
          → store.upsert_daily_pnl()
```

### 3.2 Price Architecture

```
CLOB API prices  ─┐
                   ├→ PriceBuffer (TWAP + outlier filter)
Gamma API prices ─┘       │
                           ↓
                  signal evaluation (current price_no)
                           │
                           ↓
                  /api/prices endpoint (30s TTL)
                           │
                           ↓
                  /positions JS poller (30s interval)
```

### 3.3 Forecast Error → Bias Correction → Dynamic Threshold

```
Open-Meteo Archive (2y actuals)         ─┐
Open-Meteo Previous Runs (2y forecasts) ─┴→ build_distribution()
                                               │
                                         error[i] = forecast[i] − actual[i]
                                               │
                                  ┌────────────┴────────────────────┐
                                  ↓                                  ↓
                          mean_error                               std
                          (bias correction)                  (calibration)
                                  │                                  │
                          raw_forecast − mean_error        calibrate_distance_dynamic()
                          = bias_corrected_forecast         = per-city threshold
                                  │                                  │
                          distance = slot_distance(          compare distance
                            slot, bias_corrected)            vs threshold
```

---

## 4. Database Schema

### `positions`
```sql
CREATE TABLE positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    token_id    TEXT NOT NULL,
    token_type  TEXT NOT NULL,          -- 'YES' | 'NO'
    city        TEXT NOT NULL,
    slot_label  TEXT NOT NULL,
    side        TEXT NOT NULL,          -- 'BUY' | 'SELL'
    entry_price REAL NOT NULL,
    size_usd    REAL NOT NULL,
    shares      REAL NOT NULL,
    status      TEXT DEFAULT 'open',    -- 'open' | 'closed' | 'settled'
    strategy    TEXT DEFAULT 'B',       -- 'A' | 'B' | 'C' | 'D'
    created_at  TEXT NOT NULL,
    closed_at   TEXT,
    exit_price  REAL,
    realized_pnl REAL,
    buy_reason  TEXT,
    exit_reason TEXT
);
```

### `orders`
```sql
CREATE TABLE orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT,
    event_id    TEXT NOT NULL,
    token_id    TEXT NOT NULL,
    side        TEXT NOT NULL,
    price       REAL NOT NULL,
    size_usd    REAL NOT NULL,
    status      TEXT DEFAULT 'pending',  -- 'pending' | 'filled' | 'cancelled' | 'failed'
    created_at  TEXT NOT NULL,
    filled_at   TEXT
);
```

### `daily_pnl`
```sql
CREATE TABLE daily_pnl (
    date           TEXT PRIMARY KEY,
    realized_pnl   REAL DEFAULT 0.0,
    unrealized_pnl REAL DEFAULT 0.0,
    total_exposure REAL DEFAULT 0.0,
    updated_at     TEXT NOT NULL
);
```

### `settlements`
```sql
CREATE TABLE settlements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL,
    city            TEXT NOT NULL,
    strategy        TEXT DEFAULT 'B',
    winning_outcome TEXT,
    pnl             REAL,
    settled_at      TEXT NOT NULL,
    UNIQUE(event_id, strategy)         -- idempotency guard
);
```

### `decision_log`
```sql
CREATE TABLE decision_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_at       TEXT NOT NULL,
    city           TEXT,
    event_id       TEXT,
    signal_type    TEXT,               -- 'NO' | 'LOCKED' | 'EXIT' | 'TRIM'
    slot_label     TEXT,
    forecast_high_f REAL,
    daily_max_f    REAL,
    trend_state    TEXT,
    win_prob       REAL,
    expected_value REAL,
    price          REAL,
    size_usd       REAL,
    action         TEXT,               -- 'BUY' | 'SELL' | 'SKIP'
    reason         TEXT,
    strategy       TEXT
);
```

### `edge_history`
```sql
CREATE TABLE edge_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_at       TEXT NOT NULL,
    city           TEXT,
    market_date    TEXT,
    slot_label     TEXT,
    forecast_high_f REAL,
    price_yes      REAL,
    price_no       REAL,
    win_prob       REAL,
    ev             REAL,
    distance_f     REAL,
    trend_state    TEXT
);
```

---

## 5. `config.yaml` Parameter Reference

### `strategy` section

| Parameter | Default | Description |
|-----------|---------|-------------|
| `no_distance_threshold_f` | 8 | Fallback distance threshold (°F) when auto-calibration unavailable |
| `min_no_ev` | 0.03 | Minimum expected value to enter a NO position |
| `max_position_per_slot_usd` | 5.0 | Per-slot cap for normal signals |
| `max_exposure_per_city_usd` | 50.0 | Per-city cap, enforced per strategy variant |
| `max_total_exposure_usd` | 1000.0 | Global cap, enforced per strategy variant |
| `daily_loss_limit_usd` | 50.0 | Daily loss circuit breaker |
| `kelly_fraction` | 0.5 | Half-Kelly multiplier |
| `min_market_volume` | 500 | Minimum market volume (USD) to trade |
| `max_slot_spread` | 0.15 | Maximum YES/NO spread for liquidity filter |
| `min_trim_ev` | 0.005 | EV below which held positions are trimmed |
| `max_no_price` | 0.85 | Maximum NO price to buy (asymmetric risk guard) |
| `day_ahead_ev_discount` | 0.7 | EV threshold multiplier per day ahead |
| `max_days_ahead` | 2 | Only trade markets settling within N days |
| `max_positions_per_event` | 4 | Per-event slot cap |
| `auto_calibrate_distance` | true | Use k×std dynamic calibration |
| `calibration_confidence` | 0.90 | Retained for backward-compat; not used by k×std calibrator |
| `enable_locked_wins` | true | Buy NO when daily_max > slot upper |
| `locked_win_kelly_fraction` | 1.0 | Full Kelly for locked-win signals |
| `max_locked_win_per_slot_usd` | 10.0 | Per-slot cap for locked-win signals |
| `force_exit_hours` | 1.0 | Force-exit within N hours of settlement |
| `exit_cooldown_hours` | 4.0 | Cooldown after exit (same token blocked for new BUY) |

### `scheduling` section

| Parameter | Default | Description |
|-----------|---------|-------------|
| `discovery_interval_minutes` | 15 | Market discovery frequency |
| `rebalance_interval_minutes` | 60 | Full rebalance frequency |
| `pnl_snapshot_interval_hours` | 24 | P&L snapshot frequency |

### `cities` section

30 US cities configured with:
- `name` — must match Polymarket market title city name
- `icao` — airport ICAO code for METAR (must match Polymarket settlement station)
- `lat`, `lon` — coordinates for Open-Meteo API queries
- `tz` — IANA timezone string (e.g., "America/New_York")

---

## 6. Known Limitations

1. **Gamma API JSON-string encoding** — `outcomePrices` and `clobTokenIds` fields are returned as JSON strings, not native lists. All parsing code must `json.loads()` these fields.

2. **Market title trailing "?"** — Polymarket event titles end with "?". Regex patterns for city and date extraction must account for this.

3. **Early slot confirmation is not settlement** — Individual slots can resolve to price 0 or 1 before the event closes (early outcome). This is NOT a settlement event. Settlement is only triggered when `closed=true` from the Gamma API.

4. **NWS confidence is hardcoded** — The National Weather Service API does not publish forecast uncertainty. The bot hardcodes `confidence_interval_f = 3.0°F` for NWS-only forecasts. When ensemble data is available, the empirical ensemble std overrides this.

5. **Paper mode: CLOB prices unavailable** — The py-clob-client returns empty price data in paper mode. Gamma API prices are used as fallback for unrealized P&L calculations.

6. **DailyMaxTracker uses UTC dates internally** — Date grouping is by city-local date, but timestamps are stored in UTC. Tests must use `datetime.now(timezone.utc).date()`, not `date.today()`.

7. **Calibrator confidence bounds** — `calibrate_distance_dynamic` clamps output to [3°F, 15°F]. The `calibration_confidence` config key is retained for backward compatibility but is not used by the current k×std calibrator.

8. **Per-strategy exposure, not combined** — `max_total_exposure_usd` and `max_exposure_per_city_usd` are enforced independently per strategy variant. With 4 variants, actual combined exposure can reach up to 4× these values. Kelly sizing keeps individual positions to $1–5 in practice.

9. **Gamma API repeated params** — `/markets?clob_token_ids=id1&clob_token_ids=id2` (repeated params) is required. Comma-joined values return HTTP 422. All Gamma calls use httpx's list-of-tuples param format.
