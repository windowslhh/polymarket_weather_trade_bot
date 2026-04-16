# Fix: settlement-time computation used wrong Polymarket field

**Date:** 2026-04-16
**Branch:** `claude/elastic-ritchie`
**Files changed:** `src/markets/discovery.py`
**Tests:** all 749 pass; no test changes needed (existing tests construct
`WeatherMarketEvent` directly with explicit `end_timestamp`).

---

## Symptom

On `http://198.23.134.31:5002/markets`, every active "today" market displayed
a **negative or near-zero** "Settlement: Xh" value, regardless of city. After
12:00 UTC each day, the dashboard showed:

```
Atlanta / Chicago / Dallas / Denver / Houston / Los Angeles / Miami / Seattle / SF
  tag:  "-3h left"   (negative; rendered red)
  Settlement: -2.6h  (red)
```

Same value across all cities. The actual remaining time before settle was
8–19 hours depending on city timezone.

This was not just a UI annoyance — it polluted strategy decisions:

* `evaluator.py:165` blocks **new BUY entries** when
  `hours_to_settlement < force_exit_hours` (default 6h). With the bug, this
  fired for every "today" market from 06:00 UTC onwards every day, silently
  killing entries for the entire afternoon UTC window.
* `evaluator.py:680` triggers **Layer-3 force-exit** when
  `0 ≤ hours_to_settlement ≤ force_exit_hours`. The `0 ≤` lower bound (added
  defensively in commits `8151294` / `aefd08a`) was the only thing preventing
  spurious daily mass-exits — those commits were patching the symptom, not the
  root cause.
* `trend.py:50` flags **`SETTLING`** trend state when
  `hours_to_settlement ≤ 6`. Negative values satisfy this, so the trend
  classifier was incorrectly switching to `SETTLING` mid-afternoon UTC.

---

## Root cause

`src/markets/discovery.py` (pre-fix, line 233) read Polymarket's
`event.endDate` field as the settlement timestamp:

```python
end_ts = event_data.get("endDate")
end_dt = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
```

But `event.endDate` is a **placeholder set to 12:00 UTC** of the calendar date
in the event title — it is **not** the actual resolution moment. Polymarket
uses other fields for that:

| Field on `markets[0]`            | Meaning                                     |
| -------------------------------- | ------------------------------------------- |
| `gameStartTime`                  | Start of the city's local calendar day, UTC-shifted (e.g. `2026-04-16 04:00:00+00` = 00:00 ET) |
| `closedTime` / `umaEndDate`      | Set when the UMA oracle posts the result (after settle) |

The actual settle of "Highest temperature in `<city>` on `<date>`" is the end
of that calendar day **in the city's local timezone**, which is
`gameStartTime + 24h`.

### Verification data captured from Gamma API on 2026-04-16 15:16 UTC

| Title                                       | `event.endDate`        | `markets[0].gameStartTime` |
| ------------------------------------------- | ---------------------- | -------------------------- |
| Highest temperature in **Atlanta** on Apr 16 | `2026-04-16T12:00:00Z` | `2026-04-16 04:00:00+00`   |
| Highest temperature in **Los Angeles** on Apr 16 | `2026-04-16T12:00:00Z` | `2026-04-16 07:00:00+00`   |
| Highest temperature in **Chicago** on Apr 17 | `2026-04-17T12:00:00Z` | `2026-04-17 05:00:00+00`   |
| Highest temperature in **Houston** on Apr 18 | `2026-04-18T12:00:00Z` | `2026-04-18 05:00:00+00`   |

Note that **`endDate` is identical** (12:00 UTC of the same day) for every
city sharing a date — confirming it cannot be the actual settle moment, which
must vary by timezone.

---

## Fix

Replaced the `endDate` parse with a new helper
`_compute_settle_timestamp(markets, market_date, city_tz)` that resolves in
this order:

1. **`markets[0].gameStartTime + 24h`** — primary source. Handles both
   `"2026-04-16 04:00:00+00"` and ISO-Z formats.
2. **City-timezone fallback**: 00:00 of `(market_date + 1)` in `city_tz`,
   converted to UTC. Used when `gameStartTime` is missing or unparseable.
3. **`None`** if neither source works (caller already handles `None` —
   `hours_to_settle` becomes `None`, downstream guards skip).

`event.endDate` is no longer consulted for settle-time computation.
The `settler` is unaffected — it already keys off Gamma `closed=true` (per
`CLAUDE.md`: "Settlement detection: ONLY trigger on `closed=true`").

### Why no downstream cleanup

The `0 ≤ hours_to_settlement` defensive bounds in `evaluator.py:680-681` and
similar guards remain useful as belt-and-suspenders for the brief transient
window between actual settle (~00:00 city-local) and the next 15-min
position-check cycle that detects `closed=true`. With the root cause fixed,
they simply stop firing **daily** at 12 UTC. They are no longer load-bearing
but are not harmful either.

---

## Validation

Captured local computation against the real Gamma data above
(`now = 2026-04-16T15:33:28Z`):

```
Atlanta        gameStartTime=2026-04-16 04:00Z + 24h
              → end_dt = 2026-04-17T04:00Z   hours_to_settle = +12.44h
Los Angeles    gameStartTime=2026-04-16 07:00Z + 24h
              → end_dt = 2026-04-17T07:00Z   hours_to_settle = +15.44h
Chicago        gameStartTime=2026-04-17 05:00Z + 24h
              → end_dt = 2026-04-18T05:00Z   hours_to_settle = +37.44h
Houston        gameStartTime=2026-04-18 05:00Z + 24h
              → end_dt = 2026-04-19T05:00Z   hours_to_settle = +61.44h
```

* East-coast cities (ATL/Miami) settle earliest ✓
* West-coast cities (LA/SF/Seattle) settle ~3h later ✓
* Next-day markets (Apr 17) ≈ +24h offset from today's same-tz market ✓

Fallback path tested with empty `markets[0]` — yielded the same values
(within sub-minute precision), confirming the two sources agree.

Pre-fix value for comparison:
```
OLD endDate=2026-04-16T12:00:00Z → hours = -3.56h  (identical for all cities)
```

### Post-deploy smoke test (run after merge)

```bash
curl -s http://198.23.134.31:5002/markets | grep -A1 'Settlement:'
```

Expected: every "today" market shows a positive value; west-coast > east-coast
by ~3 hours; "tomorrow" markets show ≈ today + 24h.

---

## Memory drift noted

The user's `memory/vps_access.md` contains stale paths/ports observed during
this fix:

* Deploy dir is `/opt/weather-bot-new`, not `/opt/weather-bot`
* Public port mapping is `0.0.0.0:5002 → container:5001`, not `5001` directly

These will be updated alongside this fix.
