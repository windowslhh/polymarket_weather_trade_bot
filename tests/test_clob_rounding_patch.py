"""v2-9 ROUNDING_CONFIG monkey-patch regression coverage.

The Polymarket CLOB rejects orders whose maker_amount carries more than
2 fractional digits ("invalid amounts, the market buy orders maker
amount supports a max accuracy of 2 decimals, taker amount a max of 4
decimals" — observed 2026-04-29, 20 BUYs lost in 7h before deploy).
The patch in ``src.markets.clob_client`` rewrites every entry in
``py_clob_client_v2.order_builder.builder.ROUNDING_CONFIG`` so
``amount=2`` for all tick sizes, forcing the SDK's existing
``round_down(raw_amt, amount)`` step to clamp to cents BEFORE the wire
payload is built.

These tests pin two things that 05a59c3's silent fail-soft missed:

1. The patch actually took effect at import time — every entry in the
   live ``ROUNDING_CONFIG`` carries ``amount=2``.  A future SDK bump
   that renames the dict, restructures ``RoundConfig`` fields, or adds
   a new tick size would silently leave the door open without this.

2. The downstream ``get_order_amounts`` invariant holds: for the
   pathological 7.41 × 0.55 = 4.0755 case (the original production
   reject), the BUY maker_amount comes back rounded to 2 decimals (4.07,
   wire = 4_070_000 microUSDC) — exactly what Polymarket's gateway will
   accept.  This pins the SEMANTIC outcome so a regression that, say,
   re-introduces ``amount=4`` somewhere shows up here even if the dict
   shape stays intact.
"""
from __future__ import annotations

# Import order matters: importing ``src.markets.clob_client`` triggers
# the module-level monkey-patch.  The assertions below would still pass
# even without this side-effect (the test environment imports the
# module via other tests too), but stating the dependency explicitly
# documents what the patch is anchored to.
import src.markets.clob_client  # noqa: F401  (import for side effect)


def test_rounding_config_amount_is_two_for_every_tick_size():
    """Every entry in the live ROUNDING_CONFIG must carry ``amount=2``.

    Pinned to the v2 SDK because that's the one the live bot uses
    (see ``_get_client`` in ``src.markets.clob_client``).  If
    py-clob-client ships a future major where the dict moves, this
    fails fast — and so does the patch's own startup verification
    loop, by design.
    """
    from py_clob_client_v2.order_builder.builder import ROUNDING_CONFIG

    assert ROUNDING_CONFIG, "ROUNDING_CONFIG is empty — SDK shape changed"
    for tick_size, cfg in ROUNDING_CONFIG.items():
        assert cfg.amount == 2, (
            f"ROUNDING_CONFIG[{tick_size!r}].amount={cfg.amount}, "
            f"expected 2 — patch did not take effect"
        )


def test_buy_maker_amount_clamps_pathological_case_to_two_decimals():
    """Reproduce the 2026-04-29 production reject: BUY 7.41 shares × 0.55
    USDC raw-product is 4.0755 (4 decimals), Polymarket only accepts ≤2.

    The patched ROUNDING_CONFIG drives the SDK's own clamping logic to
    push the maker_amount back to 4.07 (2 decimals, wire 4_070_000).
    Verifying through ``OrderBuilder.get_order_amounts`` rather than
    re-implementing the rounding makes the test robust to internal
    refactors (round_up/round_down ordering, decimal_places tweaks)
    while still pinning the customer-facing invariant.
    """
    from py_clob_client_v2.order_builder.builder import (
        OrderBuilder,
        ROUNDING_CONFIG,
    )

    # ``OrderBuilder`` only touches ``self.signer`` inside ``build_order``
    # (chain id, addresses).  ``get_order_amounts`` is a pure function
    # over (side, size, price, round_config), so we can hand it a
    # signer-less builder and avoid pulling in eth-account / signing
    # plumbing for a precision check.
    builder = OrderBuilder(signer=None)

    side, maker_amount, taker_amount = builder.get_order_amounts(
        "BUY", size=7.41, price=0.55,
        round_config=ROUNDING_CONFIG["0.01"],
    )
    # maker_amount is in 1e6 microUSDC units.  4.07 USDC = 4_070_000.
    # ``round_down(7.41, 2) * round_normal(0.55, 2)`` == 4.0755 →
    # patched ``amount=2`` clamp lands at 4.07 (NOT 4.0755 / 4_075_500).
    assert maker_amount == 4_070_000, (
        f"BUY maker_amount={maker_amount} (expected 4_070_000); "
        f"patched ROUNDING_CONFIG should clamp to 2 decimals"
    )
    # ``taker_amount = round_down(size, round_config.size=2)`` =
    # round_down(7.41, 2) = 7.41 → 7_410_000 microUSDC (token units).
    # The decimals invariant is bounded by ``round_config.size`` for
    # BUY taker; size stays at 2 (not touched by the patch), so this
    # is also ≤2 decimals.
    assert taker_amount == 7_410_000


def test_sell_taker_amount_clamps_to_two_decimals():
    """Symmetric SELL case.  For SELL, ``raw_taker_amt = size * price``
    is the field Polymarket validates as taker_amount.  Same 4-decimal
    risk on a 0.01-tick market unless ``amount=2`` clamps it.
    """
    from py_clob_client_v2.order_builder.builder import (
        OrderBuilder,
        ROUNDING_CONFIG,
    )

    builder = OrderBuilder(signer=None)

    side, maker_amount, taker_amount = builder.get_order_amounts(
        "SELL", size=7.41, price=0.55,
        round_config=ROUNDING_CONFIG["0.01"],
    )
    # SELL maker_amount = round_down(size, 2) = 7.41 → 7_410_000.
    assert maker_amount == 7_410_000
    # SELL taker_amount = clamp(7.41 * 0.55, amount=2) = 4.07 → 4_070_000.
    assert taker_amount == 4_070_000
