# Pending merge audit — `claude/focused-elbakyan-5de0e9` vs `claude/polymarket-weather-strategy-coeQJ`

**Date compiled:** 2026-04-28
**Author:** Claude (clever-carson-4e5a3a worktree)
**Triggered by:** city-local forecast cache hotfix (Option B); the user opted
for a surgical local fix rather than a wide cherry-pick because the two
branches have diverged in both directions and `focused-elbakyan` predates the
strategy-variant / clob-v2 / Phase A B-only / cycle-frequency-fix work that
landed on coeQJ.

This file inventories the **41 commits** on `focused-elbakyan-5de0e9` that
were *not* merged into coeQJ as part of the hotfix.  It is a reference for
the next person triaging which to bring forward, not a prescription.

## Summary

- merge-base: `9dd83c9 docs(runbook): add go-live runbook` (2026-04-26)
- focused-elbakyan unique commits: 48 total
- Of those, 7 were the targets of the city-local hotfix (now superseded by
  the local Option B implementation in this branch — do **not** cherry-pick
  these, the semantics are already in coeQJ via the new
  `city_local_date` / `get_forecasts_for_city_local_window` helpers).
- Of those, 1 (`e047f1a feat(cycle-fix-2): position_check runs entry scan
  with cached forecasts`) is already on coeQJ via the `cycle-frequency-fix`
  merge.
- The remaining 41 commits are the candidates listed below.

## Caveats

Each entry is annotated with my best-guess **status vs. coeQJ today** based
on commit messages and `git log`.  These are hypotheses, not verified
analyses — please diff each commit against current main before
cherry-picking.

- `LIKELY-COVERED`: similar fix has already landed on coeQJ via a different
  commit/path.
- `STILL-RELEVANT`: no equivalent visible on coeQJ; production-path bug
  worth bringing forward.
- `STRUCTURAL-CONFLICT`: the fix is real, but the surrounding code on
  focused-elbakyan predates a refactor on coeQJ (e.g. clob-v2, B/C/D
  variants), so a vanilla cherry-pick will not apply cleanly — needs a
  port.

## Production-critical (executor / settler / clob_client / fee)

| SHA | Title | Status | Notes |
|---|---|---|---|
| `e9dbeb6` | fix(FIX-2P-1): place_limit_order must pass OrderArgs dataclass, not dict | STRUCTURAL-CONFLICT | clob-v2 migration on coeQJ already changed `place_limit_order` to use `OrderArgs` (commit `5d5a5c5 feat(v2-3)`).  Verify before applying — likely no-op on coeQJ. |
| `3969a17` | fix(FIX-2P-2): Polymarket Weather fee = 5%, not 1.25% × 2 | LIKELY-COVERED | coeQJ has Fix C (`830baa3 fix: ghost-position guard + match_price tracking + taker fee calibration`) which already raises `TAKER_FEE_RATE` to the production-correct value.  Diff before re-applying. |
| `0fa3a0d` | fix(FIX-2P-6): preflight — confirm broker fee_rate matches TAKER_FEE_RATE | STILL-RELEVANT | Adds a startup gate that catches fee-config drift; not present on coeQJ. |
| `5469857` | fix(C-2): executor — no input mutation + BATCH_CAP_EXCEEDED REJECT + config injection | STILL-RELEVANT | Hardening that's orthogonal to clob-v2.  Likely needs porting against current executor. |
| `2b25ffc` | fix(C-2): batch-cap skip keyed on (token_id, strategy), not id(signal) | STILL-RELEVANT | Pairs with `5469857`. |

## Settler / position lifecycle

| SHA | Title | Status | Notes |
|---|---|---|---|
| `16fbe69` | fix(BUG-1): settler exception logging + cycle heartbeat | STILL-RELEVANT |  |
| `81b9d94` | fix(BUG-2): stuck-position watchdog (>48h open → warning alert) | STILL-RELEVANT |  |
| `4c52786` | fix(BUG-3): defer settlement when closed=True but outcomePrices still mid | STILL-RELEVANT | Production-path concern; CLAUDE.md already mandates "ONLY trigger on closed=true, never on partial price resolution" but the explicit `outcomePrices mid` guard may still be missing. |
| `afd626b` | fix(BUG-4): settle one event in a single transaction | STILL-RELEVANT | Atomicity / crash recovery. |
| `0f50583` | fix(BUG-5): position-check filters out tokens already settled (price 0/1) | LIKELY-COVERED | coeQJ's position_check already filters via discovery; verify behavior on cold-start before applying. |

## Date-anchor / forecast hygiene

| SHA | Title | Status | Notes |
|---|---|---|---|
| `d94b9f8` | fix(FIX-2P-10): anchor remaining `date.today()` call sites on UTC | STILL-RELEVANT | The hotfix on this branch did *not* address `markets/discovery::_parse_date` year fallback or `weather/metar::_local_today` fallback.  These remain UTC-anchored on coeQJ; that's a separate concern from the cache key.  Worth bringing forward as a small follow-up. |
| `9e2ebd5` | fix(Y9): `_parse_date` year fallback uses city-local tz when supplied | STILL-RELEVANT | Sibling of `d94b9f8`. |
| `a336e98` | fix(C-3): residual `date.today()` sweep — UTC-anchor business-logic paths | STILL-RELEVANT | Audit-completion commit; review for any remaining call sites on coeQJ. |

## Reconciler / monitoring

| SHA | Title | Status | Notes |
|---|---|---|---|
| `aca0185` | fix(G-1'): reconcile_pending_orders runs every 30 min, not just at startup | STILL-RELEVANT |  |
| `715f539` | fix(G-1' Phase 1): reconciler batch cap + per-probe + total `asyncio.timeout` | STILL-RELEVANT | Production hardening. |
| `eaf1c4a` | fix(G-4): wallet balance + nonce monitor — startup + hourly | STILL-RELEVANT |  |
| `0b6fe8c` | fix(G-7): dashboard daily_loss_remaining uses TODAY-only realized PnL | STILL-RELEVANT | Dashboard correctness. |

## DB schema / startup

| SHA | Title | Status | Notes |
|---|---|---|---|
| `7fe5f76` | hotfix(C-4): remove duplicate UNIQUE INDEX from SCHEMA — startup crash on prod DB | LIKELY-COVERED | This was a hotfix for a regression introduced by `98665f1`; if both are skipped, the underlying bug is too.  Verify on coeQJ before bringing forward. |
| `98665f1` | fix(C-4): exit_cooldowns keyed on (token_id, strategy) | STRUCTURAL-CONFLICT | Strategy-variant refactor on coeQJ may have already keyed cooldowns this way — diff carefully. |
| `302c725` | fix(Y6): strategy field invariant — triggers + startup scan + dashboard defense | STILL-RELEVANT | DB-trigger-level guard against the legacy "A" strategy leaking back in. |
| `0290057` | fix(Y6 Phase 1): trigger install failure alerts critical AND raises | STILL-RELEVANT | Pairs with `302c725`. |

## Preflight / circuit-breaker

| SHA | Title | Status | Notes |
|---|---|---|---|
| `020ca09` | fix(C-1): preflight back-off sleep before `sys.exit(2)` | STILL-RELEVANT |  |
| `4b172fc` | fix(C-6): consolidate circuit-breaker check into shared `_check_circuit_breaker` | STRUCTURAL-CONFLICT | Strategy-variant refactor changed circuit-breaker call sites; port carefully. |

## Minor / hygiene

| SHA | Title | Status | Notes |
|---|---|---|---|
| `d67ded2` | fix(Y1): drop dead `get_forecasts_batch` import from rebalancer | LIKELY-COVERED | The hotfix in this branch retained the import (still used internally).  Re-evaluate if Option B's import gets cleaned up. |
| `5b36258` | fix(Y2): de-duplicate city.tz fallback warning to once per (city, reason) | STILL-RELEVANT | The hotfix's `city_local_date` warning currently fires every call — pair this when trading volume is high. |

## Tests-only

| SHA | Title | Status |
|---|---|---|
| `02ac629` | test(C-5): pin STATION_FALLBACK decision_log breadcrumb on Denver KBKF→KDEN | STILL-RELEVANT |

## Dashboard / deploy / docs (low priority)

| SHA | Title | Status |
|---|---|---|
| `bf90c7c` | fix(FIX-2P-5): drop legacy strategy A from active dashboards + aggregates | LIKELY-COVERED via strategy-variant refactor on coeQJ |
| `4e9cc78` | fix(FIX-2P-7): deploy scripts chown .env + data/ to UID 1000 automatically | STILL-RELEVANT (Linux/docker only) |
| `94daedf` | fix(FIX-2P-8): runbook — print signer EOA + USDC reconciliation | DOCS |
| `b2b7807` | fix(FIX-2P-11): REJECT reason — CITY_NOT_IN_WHITELIST (UPPERCASE) | STILL-RELEVANT (small, easy port) |
| `2f5b8a7` | fix(Y6): dashboard total_realized filters by active variants | LIKELY-COVERED via strategy-variant refactor |
| `f120e8e` | fix(Y7): deploy scripts re-chown after VACUUM, before `docker compose up` | STILL-RELEVANT (Linux/docker only) |
| `f6cfdc5` + `d2f8e7b` | dashboard auth via DASHBOARD_SECRET (and revert) | SKIP (cancels out) |

## Docs-only (low priority)

`5579f85`, `4952837`, `a76f16e`, `c0729eb`, `6081840`, `8563a9f`, `6c1b27f`,
`a0339b4` — runbook / fee comparison / strategy notes.  Bring forward
opportunistically.

---

## Recommendation for next session

1. Start with the **STILL-RELEVANT** entries in *Production-critical* and
   *Settler / position lifecycle* — `4c52786` and `afd626b` in particular
   address real settlement-edge bugs that could chew live capital.
2. Defer the *STRUCTURAL-CONFLICT* set (`e9dbeb6`, `98665f1`, `4b172fc`) —
   each needs a port against the strategy-variant / clob-v2 versions on
   coeQJ, not a vanilla cherry-pick.
3. The *Date-anchor / forecast hygiene* group (`d94b9f8`, `9e2ebd5`,
   `a336e98`) is small and orthogonal to the rebalancer hotfix — bundle
   them in one PR.

This file should be deleted once the audit is acted on.

---

## Follow-up issues observed during 2026-04-28 hotfix verification

These are *new* bugs surfaced by the first two post-restart rebalance cycles.
They are unrelated to the city-local cache key fix (which verified clean) and
need their own follow-up sessions.

### A. CLOB price > 0.999 — EXIT signal at price 0.9995 rejected

```
2026-04-28 09:42:13 SELL NO Chicago 66-67°F @ 0.9995 ($2.48, ~2.48 shares) EV=-0.2520
2026-04-28 09:42:14 [WARNING] place_limit_order failed: invalid price (0.9995), min: 0.001 - max: 0.999
```

The strategy generated an EXIT at 0.9995 (locked-loss exit when daily max
crosses slot upper) but CLOB v2 rejects any price > 0.999.  The order
retried 3× and failed.  **Impact**: Chicago 66-67°F slot is stuck — even
though the exit signal is correct, the bot can't unwind via taker.  Workaround
when fixing: clamp ``signal.price`` to ``min(signal.price, 0.999)`` at the
executor level, or have ``evaluator.evaluate_exit_signals`` cap the exit
price at the CLOB max.  Root cause is the locked-loss-exit path computing
the live offer price without clamping; this same logic on the entry side
already caps at ``locked_win_max_price=0.90``.

### B. CLOB size < 5 shares — BUY signal at $0.19 / 0.36 shares rejected

```
2026-04-28 09:42:16 BUY NO Miami 86-87°F @ 0.5200 ($0.19, ~0.37 shares) EV=0.4169
2026-04-28 09:42:17 [WARNING] place_limit_order failed: Size (0.36) lower than the minimum: 5
```

Same class of bug as the pre-kill log entries (Miami 86-87 @ size 0.31 from
the 08:53 rebalance).  Half-Kelly sizing on a thin per-slot cap
(`max_position_per_slot_usd=$5` × `kelly_fraction=0.5` × residual room
under per-city cap) produces sub-$1 USD which translates to <5 shares at
mid-range NO prices.  CLOB v2 enforces a minimum of 5 shares per order.
**Impact**: legitimate high-EV BUY signals get rejected.  Two equally
valid fixes: (1) raise the per-slot floor so every BUY produces ≥5 shares
at the upper price bound, or (2) skip BUYs that round to <5 shares with
a `MIN_SIZE_BELOW_CLOB_FLOOR` REJECT log line for visibility.

A pre-existing session was working on (B) but was aborted; restart that
work in a new session.  (A) is independent and should be a quick guard
in `executor.py` plus a regression test.

Both issues should be addressed before the bot is deployed to VPS at
scale — they don't crash the bot but they silently waste signal generation.

