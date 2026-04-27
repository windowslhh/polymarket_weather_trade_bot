"""Bridge between ``src.config.get_strategy_variants`` and the Jinja layer.

Templates need a flat ``{strategy_key: meta_dict}`` for tag classes /
labels / colours and a flat ``{strategy_key: 0.0}`` skeleton for
per-variant aggregations.  Centralising here keeps app.py routes thin
and makes adding a new variant a one-line change in src/config.py
(no template / route surgery).
"""
from __future__ import annotations

from src.config import get_strategy_variants

# Default tag class for legacy / unknown strategies showing up in the
# DB.  Picked deliberately neutral so a row that lost its variant
# definition still renders without breaking the page.
_FALLBACK_META = {
    "label": "?",
    "description": "Unknown / legacy strategy",
    "color": "#9ca3af",       # gray-400
    "tag_class": "tag-stable",
}


def active_variant_keys() -> list[str]:
    """Strategy keys for variants the bot is actively producing signals
    for, in declaration order.  Use this to iterate ``strategy_summary``
    rows or build a fresh ``strat_realized`` skeleton."""
    return list(get_strategy_variants().keys())


def strat_meta() -> dict[str, dict]:
    """``{strategy_key: meta_dict}`` for every active variant.  Templates
    read ``strat_meta[s].tag_class`` etc. — no if/elif on hardcoded
    strategy names.
    """
    out: dict[str, dict] = {}
    for name, variant in get_strategy_variants().items():
        out[name] = dict(variant.get("_meta") or _FALLBACK_META)
    return out


def meta_for(strategy: str | None) -> dict:
    """Look up display metadata for a strategy key, falling back to a
    safe placeholder when the key is missing or unknown (legacy DB rows
    written under a retired variant).  Always returns a complete dict
    so templates can use ``.tag_class`` / ``.label`` unconditionally.
    """
    if not strategy:
        return dict(_FALLBACK_META)
    return strat_meta().get(strategy, dict(_FALLBACK_META))


def empty_strategy_aggregation(default: float = 0.0) -> dict[str, float]:
    """Skeleton dict keyed by every active variant, all values set to
    ``default``.  Use this where the previous code wrote ``{"B": 0.0}``
    inline — the new shape adapts automatically as variants are added
    or removed.
    """
    return {k: default for k in active_variant_keys()}


def fold_legacy_into_active(
    realized: dict[str, float],
) -> dict[str, float]:
    """Merge any keys present in ``realized`` that don't correspond to
    an active variant (legacy A/C/D rows from earlier line-ups) into
    the dict so dashboards still render their realized P&L.

    Active variants always appear (initialised to 0.0); legacy keys
    appear only when they have non-zero historical P&L.  Templates
    iterate the resulting dict as-is.
    """
    out = empty_strategy_aggregation()
    for key, value in realized.items():
        out[key] = out.get(key, 0.0) + float(value)
    return out
