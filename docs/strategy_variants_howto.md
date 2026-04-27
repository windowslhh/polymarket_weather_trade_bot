# Adding a new strategy variant

Active variants are declared in `src/config.py::get_strategy_variants()`.
The web layer, templates, and tests all read off whatever that dict
returns — adding (or removing) a variant is **a single edit in
`src/config.py`**.  Nothing in `src/web/`, `src/web/templates/`, or
`tests/test_strategy_variants.py` should need to change.

## The 30-second version

Open `src/config.py`, find `get_strategy_variants()`, paste a new entry
into the dict.  That's it.

```python
def get_strategy_variants() -> dict[str, dict]:
    return {
        "B": {...},
        "C": {...},
        "D": {...},
        # ── new variant here ──
        "E": {
            # StrategyConfig overrides — any key matching a
            # StrategyConfig field name overrides that field for this
            # variant.  Fields you don't override fall back to the
            # StrategyConfig default.
            "max_no_price": 0.65,
            "kelly_fraction": 0.5,
            "min_no_ev": 0.06,
            "max_positions_per_event": 4,
            "max_position_per_slot_usd": 5.0,
            "max_exposure_per_city_usd": 10.0,
            "locked_win_kelly_fraction": 1.0,
            "max_locked_win_per_slot_usd": 10.0,
            # _meta is the display block.  Underscore prefix marks it
            # as not-a-config-field; ``strategy_params`` strips it
            # before splatting into ``replace(StrategyConfig(), **)``.
            "_meta": {
                "label": "E (Tight EV)",
                "description": "max_no_price=0.65 — tight cap, higher EV",
                "color": "#10b981",       # any hex; rendered nowhere right now
                "tag_class": "tag-info",  # see CSS classes below
            },
        },
    }
```

Run `pytest tests/test_strategy_variants.py` — the parametrized cases
extend over `E` automatically.  Restart the bot; the dashboard's
config / trades / positions / dashboard pages render an `E` column /
card / panel automatically.

## Schema contract

Each variant value is a flat dict with two kinds of keys:

| Kind | Examples | Where it goes |
|---|---|---|
| StrategyConfig field overrides | `max_no_price`, `kelly_fraction`, `max_exposure_per_city_usd`, ... | `dataclasses.replace(StrategyConfig(), **strategy_params(variant))` builds the per-variant config |
| `_meta` (single key) | `_meta = {label, description, color, tag_class}` | `src/web/strategy_meta.py::strat_meta()` exposes it to templates |

**Required `_meta` keys** (all four, all strings; pinned by
`tests/test_strategy_variants.py::TestSchema`):

- `label` — short human name shown next to the strategy tag
  (e.g. `"B (Conservative)"`)
- `description` — one-line summary shown in the `/config` table
- `color` — hex string; reserved for future use (charts), no template
  reads it today
- `tag_class` — Jinja CSS class string from the set defined in
  `src/web/templates/base.html`

**`tag_class` options** (defined in `base.html`):

| Class | Visual |
|---|---|
| `tag-info` | blue (baseline / production variant) |
| `tag-warning` | amber (control / experimental) |
| `tag-danger` | red (aggressive / cap-relaxed) |
| `tag-stable` | grey (legacy / unknown — fallback) |

If a variant needs a colour outside this set, add a new class in
`base.html` first.

## What changing a variant rebalances automatically

After the edit, with no other changes:

| Surface | Behaviour |
|---|---|
| `src/strategy/rebalancer.py` | Iterates `get_strategy_variants()` once per cycle; new variant produces signals from the next rebalance |
| `/` dashboard | "Strategy variants" panel adds a row |
| `/positions` | Adds a per-variant panel; legacy / unknown keys still surface under the fallback |
| `/trades` | Adds a metric card; per-variant P&L bucketed correctly |
| `/config` | Strategy-variants table adds a row, falling back to `StrategyConfig` defaults for any field not overridden |
| `/history` | Settlement rows tagged with the right CSS class |
| `tests/test_strategy_variants.py` | Parametrized cases (`test_each_variant_produces_signals`, `test_locked_win_full_kelly_sizes_larger_than_half`) extend automatically |
| `tests/test_strategy_meta.py` | Schema-shape tests pass as long as `_meta` is well-formed |

## DB schema and the `strategy` column

The `strategy` column on `positions` / `orders` / `settlements` /
`decision_log` accepts A/B/C/D (Y6 trigger constraint).  Variant keys
should be a single uppercase letter or short string in that family.

If you need a key outside A/B/C/D, the trigger needs an update first —
do **not** just add the variant; the bot will start writing rows and
the trigger will reject them at INSERT.

## Removing a variant

Just delete the entry from `get_strategy_variants()`.  Existing DB rows
keep their original `strategy` value — they show up in dashboards under
the neutral grey `tag-stable` fallback (label `"?"`).

`run_position_check` evaluates legacy positions against base
`StrategyConfig` defaults (no overrides) so they keep generating exit
signals until they settle or get manually closed.

## Things you do **not** need to touch

- `src/web/app.py` route handlers — all four affected routes already
  pull from `get_strategy_variants()` via the `strategy_meta` helpers.
- `src/web/templates/*.html` — templates iterate `variants.items()` and
  look up `strat_meta[key].tag_class`.
- `src/strategy/rebalancer.py` — both `replace()` sites go through
  `strategy_params(variant)` to strip `_meta`.
- `tests/test_strategy_meta.py` — pinned to schema invariants, not
  specific variant counts.
- `tests/test_strategy_variants.py` — same; parametrized over whatever
  the dict returns.

## When this breaks

- **Variant key not uppercase / longer than 3 chars** →
  `TestSchema::test_strategy_keys_are_uppercase_letters` fails.
- **`_meta` missing or has non-string values** →
  `TestSchema::test_every_variant_has_meta_block` /
  `test_meta_values_are_strings` fail.
- **Override key not a StrategyConfig field** →
  `TestSchema::test_all_overrides_are_valid_strategy_fields` fails
  (dataclass `replace()` would TypeError at runtime — this catches it
  in CI before deploy).
- **`tag_class` references a CSS class that doesn't exist in
  `base.html`** → template renders, but the strategy tag has no
  styling.  Not test-caught; visible on `/config` immediately.

## Related modules

- `src/config.py::get_strategy_variants` — the dict
- `src/config.py::strategy_params` — strips `_meta` before splat
- `src/web/strategy_meta.py` — five helpers used by `app.py` routes
- `src/web/templates/base.html` — CSS class definitions
- `tests/test_strategy_variants.py` — schema invariants + parametrized
  signal-generation tests
- `tests/test_strategy_meta.py` — bridge module unit tests
