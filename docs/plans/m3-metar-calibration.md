# Plan: M3 Switch calibration actuals from Open-Meteo to METAR

**Status**: planned, 2026-04-20
**Predecessor**: M2 (PR-B, in progress — not a hard dependency, but easier to review in sequence)
**Target PR**: PR-C

## Motivation

`src/weather/historical.py::build_error_distribution` computes the forecast
error distribution as `forecast - actual`, where **actual** is pulled from
Open-Meteo's gridded reanalysis (`temperature_2m_max` at the city's lat/lon).

Polymarket settles on the **airport METAR station's daily maximum** via
Weather Underground. These two numbers are systematically different:
- Open-Meteo interpolates from a ~3km grid; airport microclimate isn't
  captured (urban heat island, runway ground conditions, etc.)
- The station is a specific thermometer that reports integer-Fahrenheit
  observations on a specific cadence; settlement uses `wu_round()`

Consequence: `error_dist.mean` mixes "forecast vs reality" with
"Open-Meteo grid vs METAR station". The former is what we want to correct
for; the latter is pure measurement noise that gets baked into:
- Bias-corrected distance filter in `evaluate_no_signals`
- Per-city distance thresholds via `calibrator.calibrate_distance_dynamic`
- Locked-win win_prob values (0.999 / 0.99 assume calibration is tight)

For Houston KHOU post-fix, `error_dist.mean = +2.32°F`. Unclear how much
of that 2.32 is real forecast bias vs Open-Meteo/METAR divergence — we
won't know until we switch the source.

## Scope

### Data source

**Primary**: IEM ASOS archive (`mesonet.agron.iastate.edu`). Covers all
SETTLEMENT_STATIONS, multi-year lookback, CSV responses.

Endpoint pattern:
```
https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py
  ?station=KHOU
  &data=tmpf               # air temp in Fahrenheit
  &year1=2024&month1=4&day1=20
  &year2=2026&month2=4&day2=20
  &tz=Etc/UTC
  &format=onlycomma
  &latlon=no&missing=M&trace=T
  &direct=no&report_type=3
```

Response: CSV with columns `station,valid,tmpf`. Raw observations (usually
5-min or hourly cadence).

**Fallback**: aviationweather.gov METAR API (current `fetch_settlement_temp`
data source) — only provides ~36h history, insufficient for 2-year build
but useful for incremental updates.

### Processing

1. Pull 2-year raw obs for each station
2. Convert `valid` timestamp to station local timezone (use `CityConfig.tz`)
3. Group by local date
4. Daily max = max(tmpf) across that date's observations
5. Emit `(date, daily_max_f)` pairs

### Integration

Replace `fetch_historical_actuals()` in `src/weather/historical.py` with
`fetch_historical_metar_maxes()` that hits IEM. Keep Open-Meteo path
available as a **fallback** for cities without IEM coverage (shouldn't
happen for current US cities but defensive).

New cache location: `data/history/{icao}_errors_metar.json` (side-by-side
with legacy, so pre-M3 caches don't get silently reinterpreted).

### Validation

1. Rebuild distributions for all 30 cities
2. Compare `mean` / `std` before-vs-after
3. Expected: `std` should be comparable; `mean` may shift by ±1-2°F if
   Open-Meteo was systematically off at that city
4. Backtest: run `scripts/backtest_ensemble_spread.py` with both caches,
   compare PnL. Difference should be small (std-driven thresholds mostly
   preserved) but bias-correction term will change.

### Rollout

- **First deploy**: ship new caches alongside old (`_errors.json` and
  `_errors_metar.json` coexist). Bot reads from `_errors_metar.json` if
  present, else falls back to `_errors.json`.
- After 1-2 weeks of observed stability: delete old caches.

## Acceptance criteria

1. `data/history/{icao}_errors_metar.json` generated for all 30 cities
2. Unit test: given a mocked IEM response, `fetch_historical_metar_maxes`
   returns correct daily maxes with timezone handling
3. Integration test: rebuild Houston with both sources, assert `std` is
   within 0.5°F of each other (sanity check — if they're wildly different
   the METAR processing is wrong)
4. Backtest PnL delta <10% vs current (larger delta = investigate before ship)

## Risks

- **IEM rate limits**: undocumented; historically 1req/sec has been fine
  but need to cap concurrency (current `build_all_distributions` uses
  `asyncio.gather` across all 30 cities — throttle for IEM)
- **Missing days**: IEM occasionally has gaps (<1% for K-stations). Need
  sane handling — skip day, don't emit NaN error
- **wu_round behavior**: calibration uses raw floats, settlement uses
  `wu_round(daily_max)` for slot-edge decisions. The two are consistent
  for distributional purposes; no change needed here, just flag the
  distinction in code comments

## Out of scope

- Re-calibrating the locked-win win_prob (0.999 / 0.99) — those are
  analytical bounds from `margin >= locked_win_margin_f`, not empirical
- Changing `post_peak_confidence` values — those are orthogonal

## Hand-off notes

- Depends only on config/lat-lon from `config.yaml` + the SETTLEMENT_STATIONS
  map — no changes to strategy code
- Worktree / branch: pick a fresh branch off current main (after M2 merges
  is preferred for clean diff, but not strictly required)
- Start by curling the IEM endpoint manually for one station to confirm
  the response format hasn't changed since this plan was written
