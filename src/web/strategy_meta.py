"""Bridge between ``src.config.get_strategy_variants`` and the Jinja layer.

Routes call into this module to get two things:

1. **Display metadata** (``strat_meta`` / ``meta_for``).  Each variant's
   ``_meta`` dict carries the four keys templates render —
   ``label`` / ``description`` / ``color`` / ``tag_class``.  No template
   should ever ``{% if strategy == 'X' %}``; instead they look up
   ``strat_meta[key].tag_class`` and the right CSS class falls out.

2. **Per-variant aggregation skeletons** (``empty_strategy_aggregation``
   / ``fold_legacy_into_active``).  Pre-shaped dicts keyed by every
   active variant so per-strategy P&L / exposure rollups don't need
   ``{"B": 0.0}`` literals scattered across routes — adding a variant
   propagates everywhere with zero route edits.

Adding a new variant is a single edit in ``src/config.py``.  Removing
a variant the DB still has rows for is also safe: legacy keys flow
through with the neutral ``_FALLBACK_META`` shape, the dashboard stays
up, and nothing 500s.
"""
from __future__ import annotations

from src.config import get_strategy_variants

# Used when a row's strategy key isn't in the active variants dict —
# either a legacy ``A`` from before this experiment line-up, or a typo
# from an out-of-band manual DB write.  Templates always succeed because
# every required key is present; the neutral grey ``tag-stable`` makes
# such rows visually distinct from active variants without breaking the
# layout.  Never returned as-is — callers always copy via ``dict(...)``
# so a template that mutates won't bleed across requests.
_FALLBACK_META = {
    "label": "?",
    "description": "Unknown / legacy strategy",
    "color": "#9ca3af",       # gray-400
    "tag_class": "tag-stable",
}


def active_variant_keys() -> list[str]:
    """Return the active-variant strategy keys in declaration order.

    Returns a fresh list each call — callers can mutate freely.  The
    ordering matches ``get_strategy_variants()`` insertion order, which
    is the same order the templates iterate (so dashboards render
    variants in a stable, predictable sequence).
    """
    return list(get_strategy_variants().keys())


def strat_meta() -> dict[str, dict]:
    """Return ``{strategy_key: meta_dict}`` for every active variant.

    Each ``meta_dict`` is a fresh shallow copy of the variant's
    ``_meta`` block (or ``_FALLBACK_META`` if the variant was declared
    without one — a defensive path that should not fire for any
    well-formed config but keeps the page alive if it does).  Mutation
    safety: callers can ``.pop`` / overwrite without bleeding into
    other requests because each invocation rebuilds the dict.
    """
    out: dict[str, dict] = {}
    for name, variant in get_strategy_variants().items():
        out[name] = dict(variant.get("_meta") or _FALLBACK_META)
    return out


def meta_for(strategy: str | None) -> dict:
    """Look up display metadata for one strategy key, with a safe
    fallback for unknown / legacy / empty / ``None`` inputs.

    Always returns a complete dict carrying every key the templates
    read, so a Jinja expression like ``meta_for(s).tag_class`` is
    safe whether ``s`` is an active variant or a forgotten ``A`` row.
    Returned dict is a fresh copy — mutation-safe.
    """
    if not strategy:
        return dict(_FALLBACK_META)
    return strat_meta().get(strategy, dict(_FALLBACK_META))


def empty_strategy_aggregation(default: float = 0.0) -> dict[str, float]:
    """Pre-shaped aggregation skeleton: one entry per active variant,
    all set to ``default``.

    Used at the top of route handlers that compute per-strategy P&L or
    exposure, replacing inline literals like ``{"B": 0.0}``.  Adding a
    variant in ``src/config.py`` automatically extends every route
    using this skeleton — no surgical edits.
    """
    return {k: default for k in active_variant_keys()}


def fold_legacy_into_active(
    realized: dict[str, float],
) -> dict[str, float]:
    """Build a per-variant P&L dict that initialises every active
    variant to 0.0 then adds in ``realized`` values keyed by strategy.

    Behaviour:
    - Active variants always appear in the result (even at 0.0) so
      dashboard rows render in a stable order.
    - Legacy keys (e.g. settled-row strategy 'A' from an earlier line-up)
      pass through with their value — the dashboard surfaces them
      alongside active variants instead of silently dropping the
      historical P&L.
    - Values are coerced to ``float`` so a stringified DB read
      (``"0.42"``) doesn't break the sum.

    Returns a fresh dict — caller may mutate.
    """
    out = empty_strategy_aggregation()
    for key, value in realized.items():
        out[key] = out.get(key, 0.0) + float(value)
    return out
