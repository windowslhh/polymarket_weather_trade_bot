# Pending plans

Architectural work planned after PR #5 (data-layer ICAO fix + M1 guardrails
+ L6 startup alignment, merged 2026-04-20 as `21573b0`).

| # | File | Scope | Ready to start? | Est. effort |
|---|------|-------|-----------------|-------------|
| **M2** | [`m2-gate-matrix.md`](m2-gate-matrix.md) | Unified `GATE_MATRIX` refactor + D1 discovery 0-price filter + O1 bulk-UNRESOLVED alignment alarm | Yes | 2-3 days |
| **M3** | [`m3-metar-calibration.md`](m3-metar-calibration.md) | Switch forecast error calibration actual source from Open-Meteo grid to METAR station daily_max (IEM ASOS archive) | Yes (no hard dependency on M2, but easier review if sequenced) | 1-2 days |
| **M4** | [`m4-city-budget.md`](m4-city-budget.md) | Cross-strategy shared city exposure budget. **Requires product decision first** (keep 4 strategies live / drop to 1 live + 3 shadow / alternative). | **No** — blocked on decision | 1-2 days once decided |

## Recommended order

1. **M2** next — highest structural leverage, lowest product risk. Future
   guardrail misses (like the Bug #1 that motivated PR #5) become much
   less likely once every entry path walks the same declarative gate list.
2. **M3** after — source-of-truth fix for calibration. Cleanest as an
   isolated PR so it can be reverted independently if stats shift in
   unexpected ways.
3. **M4** last — has a product dimension (which strategies stay live) that
   should be settled with real observation data first.

## Out of scope of all three

- Retiring `StrategyConfig.min_trim_ev` legacy field (kept for back-compat)
- Rewriting the CLOB client or executor
- Moving off SQLite
- New asset classes (only temperature markets for now)
