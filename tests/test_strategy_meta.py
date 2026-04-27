"""Tests for src.web.strategy_meta — the bridge between
``get_strategy_variants()`` and the Jinja layer.

These tests pin invariants that the templates rely on:
- ``strat_meta()`` always returns a dict with the same keys as
  ``get_strategy_variants()``
- Each meta dict has the four template-required keys
- ``meta_for(unknown)`` returns a complete fallback dict (templates
  unconditionally read ``.tag_class`` etc. — None or KeyError would
  500 the page)
- ``empty_strategy_aggregation`` and ``fold_legacy_into_active``
  preserve the active-key skeleton even when given foreign keys
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.web import strategy_meta


REQUIRED_META_KEYS = {"label", "description", "color", "tag_class"}


class TestStratMeta:

    def test_keys_match_active_variants(self):
        from src.config import get_strategy_variants

        active = set(get_strategy_variants().keys())
        assert set(strategy_meta.strat_meta().keys()) == active

    def test_each_meta_has_required_keys(self):
        for variant_key, meta in strategy_meta.strat_meta().items():
            missing = REQUIRED_META_KEYS - meta.keys()
            assert not missing, (
                f"Variant {variant_key} _meta missing keys: {missing}"
            )

    def test_returns_fresh_dict_each_call(self):
        """Mutating the returned dict must not bleed into the next call —
        templates / route handlers occasionally pop / overwrite keys.
        """
        first = strategy_meta.strat_meta()
        first.clear()
        second = strategy_meta.strat_meta()
        assert second  # repopulated
        # And mutating an inner meta dict shouldn't bleed either.
        any_key = next(iter(second))
        second[any_key]["label"] = "MUTATED"
        third = strategy_meta.strat_meta()
        assert third[any_key]["label"] != "MUTATED"


class TestMetaFor:

    def test_known_variant_returns_real_meta(self):
        from src.config import get_strategy_variants

        any_key = next(iter(get_strategy_variants()))
        meta = strategy_meta.meta_for(any_key)
        assert REQUIRED_META_KEYS <= meta.keys()

    def test_unknown_variant_returns_fallback(self):
        meta = strategy_meta.meta_for("ZZZ-not-a-variant")
        assert REQUIRED_META_KEYS <= meta.keys()
        # Fallback uses neutral grey tag so unknown legacy rows still render.
        assert meta["tag_class"] == "tag-stable"

    def test_none_returns_fallback(self):
        meta = strategy_meta.meta_for(None)
        assert REQUIRED_META_KEYS <= meta.keys()

    def test_empty_string_returns_fallback(self):
        meta = strategy_meta.meta_for("")
        assert REQUIRED_META_KEYS <= meta.keys()


class TestActiveVariantKeys:

    def test_matches_get_strategy_variants(self):
        from src.config import get_strategy_variants

        assert strategy_meta.active_variant_keys() == list(
            get_strategy_variants().keys(),
        )


class TestEmptyAggregation:

    def test_skeleton_has_active_keys_with_zero(self):
        agg = strategy_meta.empty_strategy_aggregation()
        assert set(agg.keys()) == set(strategy_meta.active_variant_keys())
        assert all(v == 0.0 for v in agg.values())

    def test_custom_default(self):
        agg = strategy_meta.empty_strategy_aggregation(default=1.5)
        assert all(v == 1.5 for v in agg.values())


class TestFoldLegacyIntoActive:

    def test_active_keys_initialised_to_zero(self):
        out = strategy_meta.fold_legacy_into_active({})
        assert set(out.keys()) == set(strategy_meta.active_variant_keys())
        assert all(v == 0.0 for v in out.values())

    def test_active_key_value_summed(self):
        active = strategy_meta.active_variant_keys()
        if not active:
            pytest.skip("no active variants")
        key = active[0]
        out = strategy_meta.fold_legacy_into_active({key: 5.5})
        assert out[key] == 5.5

    def test_legacy_key_added_alongside_active(self):
        out = strategy_meta.fold_legacy_into_active({"A": 1.23, "Z": -0.5})
        for k in strategy_meta.active_variant_keys():
            assert k in out  # active still present
        assert out["A"] == 1.23
        assert out["Z"] == -0.5


class TestSchemaRobustness:

    def test_variant_without_meta_falls_back(self):
        """A variant dict that forgot ``_meta`` should still produce a
        full meta dict via fallback — no KeyError on .tag_class etc.
        """
        broken = {"X": {"max_no_price": 0.5}}  # no _meta key
        with patch.object(strategy_meta, "get_strategy_variants",
                          return_value=broken):
            meta = strategy_meta.strat_meta()
        assert "X" in meta
        assert REQUIRED_META_KEYS <= meta["X"].keys()
