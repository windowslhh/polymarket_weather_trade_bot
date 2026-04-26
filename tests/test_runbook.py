"""FIX-2P-8: pin the runbook's wallet-fingerprint + USDC reconciliation steps.

Pre-fix the runbook had no step that surfaced the signer EOA derived
from `ETH_PRIVATE_KEY`.  Operators were funding USDC into the address
shown on the Polymarket frontend without ever cross-checking that the
bot in the container would actually sign from the same key — a
.env swap-in / typo would silently route trades through a different
wallet.  These assertions guard the two steps that close that gap.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "runbook" / "go_live_runbook.md"


def test_runbook_has_get_address_step() -> None:
    body = RUNBOOK.read_text()
    assert "c.get_address()" in body, (
        "FIX-2P-8: runbook must call client.get_address() so operators "
        "can fingerprint the signer EOA before cutover."
    )
    # Anchored on a heading-style marker so renumbering doesn't accidentally
    # delete the step.
    assert "FIX-2P-8" in body


def test_runbook_usdc_balance_step_references_signer_eoa() -> None:
    body = RUNBOOK.read_text()
    assert "Wallet USDC balance" in body
    # The reconciliation pointer must explicitly call out matching the
    # frontend address against the EOA printed in 1.11b.
    assert "signer EOA" in body, (
        "FIX-2P-8: USDC balance step must reference the signer EOA "
        "printed in step 1.11b so a wallet mismatch is caught."
    )


def test_runbook_distinguishes_signer_eoa_from_proxy_wallet() -> None:
    """Y8: the runbook must explicitly explain that the signer EOA and
    the USDC-holding proxy wallet are DIFFERENT addresses, and that
    comparing USDC balance directly against the EOA is wrong."""
    body = RUNBOOK.read_text()
    assert "proxy wallet" in body, (
        "Y8: runbook must name the proxy wallet concept"
    )
    assert "Login wallet" in body, (
        "Y8: runbook must reference the Login wallet UI element so "
        "operators know which Polymarket UI element to compare"
    )
    # Sanity: the Y8 explanation that "EOA balance reads 0 — that's expected"
    # exists so an operator doesn't flag it as a problem.
    assert "always read 0" in body or "always reads 0" in body, (
        "Y8: must call out that the EOA's USDC balance is always 0 and "
        "that's expected (USDC lives in the proxy wallet)"
    )
