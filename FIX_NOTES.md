# FIX Notes — Long-term Items (not in go-live plan)

Active plan: `fix/go-live-plan` covers Blockers + High + Medium fixes. This file tracks
the "Long-term" items that were explicitly out of scope but worth preserving for a later
sprint.

## Long-term (post go-live)

- **Dashboard auth**: web/app.py exposes `/api/admin/*` protected only by `TRIGGER_SECRET`
  in headers. For multi-operator use, move to OIDC/SSO.
- **Horizontal scale**: single-node SQLite. If we ever need >1 bot instance sharing
  state, migrate to Postgres with row-level locks on `positions`/`orders`.
- **Forecast pipeline observability**: we log ensemble spread but don't expose per-model
  health. Add a per-cycle dashboard panel: which models responded, which were fallbacks.
- **EV calibration loop**: we use static win_prob curves from calibrator.py. A nightly
  job that refits the curves against actual settlement outcomes would tighten edges.
- **Order book depth sizing**: we take midpoint and hope. Larger size (>$50/slot) should
  walk the book and cap at e.g. 2× best-ask.
- **Backtest integration with live db**: current backtest pipeline is offline; stitching
  it to the live edge_history would let us paper-grade variants without redeploying.

## Known regressions tolerated until after go-live

- VPS deploy path remains `/opt/weather-bot-new` (not `/opt/weather-bot`). Legacy dir
  still exists; clean up after one week of stable `-new` operation.
- `get_strategy_realized_pnl()` in store.py still returns a dict keyed `{A,B,C,D}`.
  FIX-17 drops A and renames D→D'; we leave the dict keys alone for dashboard
  back-compat. Track removing 'A' key in a later PR once no dashboard references it.
