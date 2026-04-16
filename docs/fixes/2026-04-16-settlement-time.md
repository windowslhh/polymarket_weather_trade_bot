# Fix: settlement-time computation used wrong Polymarket field

**Date:** 2026-04-16 (original), amended 2026-04-17 after code-review round 1
**Branch:** `claude/elastic-ritchie`
**Files changed:**
  * `src/markets/discovery.py` — root-cause fix + DST-safe helper refactor
  * `tests/test_discovery_settle_time.py` — new, 16 unit tests for the helper
  * `tests/test_discovery_utc_midnight.py` — added 3 end-to-end tests for the discovery flow

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
  `hours_to_settlement < force_exit_hours` (default 6h). With the bug this
  fired for every "today" market from 06:00 UTC onwards every day, silently
  killing entries for the entire afternoon UTC window.
* `evaluator.py:680` triggers **Layer-3 force-exit** when
  `0 ≤ hours_to_settlement ≤ force_exit_hours`. The `0 ≤` lower bound (added
  defensively in commits `8151294` / `aefd08a`) was the only thing preventing
  spurious daily mass-exits — those commits were patching the symptom, not
  the root cause.
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

But `event.endDate` is a **placeholder set to 12:00 UTC** of the calendar
date in the event title — it is **not** the actual resolution moment.
Polymarket uses other fields for that:

| Field on `markets[0]`            | Meaning                                     |
| -------------------------------- | ------------------------------------------- |
| `gameStartTime`                  | Start of the city's local calendar day, UTC-shifted (e.g. `2026-04-16 04:00:00+00` = 00:00 ET) |
| `closedTime` / `umaEndDate`      | Set by the settler after the UMA oracle posts the result |

The actual settle of "Highest temperature in `<city>` on `<date>`" is the
end of that calendar day **in the city's local timezone** — equivalently,
00:00 of the next day anchored in the city's tz, converted to UTC.

### Verification data captured from Gamma API on 2026-04-16 15:16 UTC

| Title                                       | `event.endDate`        | `markets[0].gameStartTime` |
| ------------------------------------------- | ---------------------- | -------------------------- |
| Highest temperature in **Atlanta** on Apr 16     | `2026-04-16T12:00:00Z` | `2026-04-16 04:00:00+00`   |
| Highest temperature in **Los Angeles** on Apr 16 | `2026-04-16T12:00:00Z` | `2026-04-16 07:00:00+00`   |
| Highest temperature in **Chicago** on Apr 17     | `2026-04-17T12:00:00Z` | `2026-04-17 05:00:00+00`   |
| Highest temperature in **Houston** on Apr 18     | `2026-04-18T12:00:00Z` | `2026-04-18 05:00:00+00`   |

Note that **`endDate` is identical** (12:00 UTC of the same day) for every
city sharing a date — confirming it cannot be the actual settle moment,
which must vary by timezone.

---

## Fix

Replaced the `endDate` parse with a new helper
`_compute_settle_timestamp(markets, market_date, city_tz)`.

### Design (amended after review round 1)

Both resolution paths construct the answer via
`ZoneInfo(city_tz)`-anchored midnight of `date + 1` and convert to UTC.
The only difference between the paths is how the calendar `date` is
sourced:

1. **Primary** — `markets[0].gameStartTime` parsed to UTC, converted back
   to `city_tz`, and `.date()` taken. This gives the city-local calendar
   date that Polymarket itself asserts the market belongs to (more
   authoritative than the free-form event title).
2. **Fallback** — the `market_date` parsed from the title.
3. **`None`** if `city_tz` is missing or unrecognised (no `ZoneInfo` =
   nothing meaningful to compute).

Anchoring midnight through `ZoneInfo` rather than doing
`gameStartTime + timedelta(hours=24)` is what makes this DST-safe — see
next section.

### DST safety (discovered during review round 1)

The original implementation used `gameStartTime + timedelta(hours=24)`.
That's wrong by ±1h on the two US DST transition days per year, because
a city's calendar day is 23h (spring forward) or 25h (fall back) long —
not always 24h:

| Day                         | `gameStartTime` (00:00 local) | Old `+24h` primary | ZoneInfo fallback | Correct (real settle) |
| --------------------------- | ----------------------------- | ------------------ | ----------------- | --------------------- |
| 2026-03-08 NYC (spring fwd) | `2026-03-08T05:00Z` (EST)    | `2026-03-09T05:00Z` ✗ | `2026-03-09T04:00Z` ✓ | `2026-03-09T04:00Z` |
| 2026-11-01 NYC (fall back)  | `2026-11-01T04:00Z` (EDT)    | `2026-11-02T04:00Z` ✗ | `2026-11-02T05:00Z` ✓ | `2026-11-02T05:00Z` |

On non-DST days both constructions agree. On a DST day the old primary
path was 1h wrong; the fallback path was correct. The refactor unifies
them on the correct construction so primary and fallback agree on all
days.

**Impact if the old `+24h` path had shipped:** twice a year, every "today"
market in every US city would have been off by 1h — nudging
`force_exit_hours`, the new-entry block, and `SETTLING` by that hour.
Not a full outage (the defensive bounds below still absorb it), but
enough to skew behaviour during the transition window.

### Why the downstream defensive bounds remain

The `0 ≤ hours_to_settlement` bounds in `evaluator.py:680-681`, the
`< force_exit_hours` gate at `evaluator.py:165`, and the `≤ 6` check in
`trend.py:50` are **retained, not removed**. They remain load-bearing
rather than purely cosmetic:

1. **DST edge** — any future regression that re-introduces `+24h`-style
   arithmetic (or a similar off-by-one in timezone handling) would
   produce ±1h drift on transition days; the bounds absorb that drift
   until CI catches it. The new unit tests in `TestDSTTransitions` are
   the primary safety net; the runtime bounds are the backup.
2. **Post-settle transient** — between the actual settle moment
   (≈ 00:00 city-local) and the next 15-min position-check cycle
   detecting `closed=true` in Gamma, `hours_to_settle` can be slightly
   negative for up to ~15 minutes. The bounds prevent spurious signals
   during that window.
3. **Upstream regression** — if Polymarket ever changes `gameStartTime`
   semantics or the title date-parser breaks, `_compute_settle_timestamp`
   returning something nonsensical is contained by the bounds rather
   than directly corrupting trading behaviour.

The bounds no longer fire **daily** (which they did under the old bug),
but each condition they defend against is real.

`event.endDate` is no longer consulted for settle-time computation.
The `settler` is unaffected — it already keys off Gamma `closed=true`
(per `CLAUDE.md`: "Settlement detection: ONLY trigger on `closed=true`").

---

## Validation

### Production-anchor values (computed by the refactored helper)

`now = 2026-04-16T15:33Z`, against real Gamma `gameStartTime` data:

```
Atlanta        → end_dt = 2026-04-17T04:00Z   hours_to_settle = +12.44h
Los Angeles    → end_dt = 2026-04-17T07:00Z   hours_to_settle = +15.44h
Chicago        → end_dt = 2026-04-18T05:00Z   hours_to_settle = +37.44h
Houston        → end_dt = 2026-04-19T05:00Z   hours_to_settle = +61.44h
```

* East-coast cities (ATL/Miami) settle earliest ✓
* West-coast cities (LA/SF/Seattle) settle ~3h later ✓
* Next-day markets (Apr 17) ≈ +24h offset from today's same-tz market ✓

These values are also pinned as parametrised assertions in
`tests/test_discovery_settle_time.py::TestProductionAnchors`.

### DST-day behaviour (agreement between paths)

On `2026-03-08` and `2026-11-01`, both the primary (`gameStartTime`) and
fallback (`market_date`) paths now return the same correct UTC moment.
Pinned in `tests/test_discovery_settle_time.py::TestDSTTransitions`.

### Pre-fix value for comparison

```
OLD endDate=2026-04-16T12:00:00Z → hours = -3.56h   (identical for all cities)
```

### Post-deploy smoke test (run after merge)

```bash
curl -s http://198.23.134.31:5002/markets | grep -A1 'Settlement:'
```

Expected: every "today" market shows a positive value; west-coast ~3h
larger than east-coast; "tomorrow" markets ≈ today + 24h (±1h on DST
transition days).
