"""Tests for ``src.settlement.redeemer.Redeemer``.

These tests cover the *contract* the trade bot reference (``polymarket_trade_bot``)
got wrong for weather:

  1. NegRiskAdapter ``amounts`` ordering — Polymarket convention is
     ``[YES, NO]``; weather bot holds NO so amounts must be
     ``[0, no_balance]`` (the trade-bot reference holds YES and uses
     ``[yes_balance, 0]``).  Off-by-one here silently redeems zero shares.

  2. Gas cap — when Polygon gas spikes above the configured cap, the
     redeemer must defer (returning ``gas_too_high``) rather than burn
     budget on a $0.50 redemption that costs $1.50 in gas.

  3. Pending-tx race protection — the *settler* atomically claims the
     row before invoking us; the redeemer itself is single-shot.  A
     happy-path test confirms tx_hash flows back to the result.

  4. CLOB-API balance path (added 2026-04-28) — the original
     ``ConditionalTokens.balanceOf`` lookup returned 0 for negRisk
     markets and produced a false-positive ``already_redeemed`` on
     Miami 88-89 + Chicago 66-67.  The new path queries
     ``ClobClient.get_conditional_balance(token_id)`` instead and is
     covered by ``test_clob_balance_*`` below.

The web3 layer is mocked end-to-end so this suite never makes a real
RPC call (deterministic + offline).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.settlement.redeemer import (
    NEG_RISK_ADAPTER,
    Redeemer,
    RedeemResult,
    USDC_ADDRESS,
)


class _FakeClob:
    """Stand-in for ``src.markets.clob_client.ClobClient`` with the single
    method the Redeemer needs.  ``balance`` is the raw 6-decimals NO
    balance to return; if ``raise_exc`` is set, ``get_conditional_balance``
    raises it instead.
    """

    def __init__(self, balance: int = 0, raise_exc: Exception | None = None):
        self._balance = balance
        self._raise = raise_exc
        self.calls: list[str] = []

    async def get_conditional_balance(self, token_id: str) -> int:
        self.calls.append(token_id)
        if self._raise is not None:
            raise self._raise
        return self._balance


# ── Fake w3 plumbing ──────────────────────────────────────────────────

def _make_fake_w3(
    *,
    gas_price_wei: int = 30_000_000_000,  # 30 gwei
    no_balance: int = 5_000_000,          # 5.0 NO shares (6 decimals)
    captured: dict | None = None,
):
    """Build a w3 stand-in whose contract().functions.<X>() return mocks
    that can be inspected to verify how the redeemer encoded the call.

    ``captured`` is a dict the test passes in; the fake fills it with
    the calldata args we want to assert on (target, calldata bytes,
    encode_abi args).
    """
    captured = captured if captured is not None else {}

    # contract() returns one of three different shapes depending on the
    # ABI it was constructed with.  We dispatch on the address arg so a
    # single fake covers all three call sites.
    def make_inner_contract(address: str, abi):
        # NegRiskAdapter or CT redeem contract — must support encode_abi.
        c = MagicMock()
        captured["last_target"] = address

        def encode_abi(name: str, args):
            captured["last_encode_abi_call"] = (name, args)
            # Return a 0x-prefixed hex calldata; format must round-trip
            # through bytes.fromhex(calldata[2:]).
            return "0x" + ("aa" * 8)

        c.encode_abi.side_effect = encode_abi
        return c

    def make_safe_contract(address: str, abi):
        c = MagicMock()
        c.functions.nonce.return_value.call.return_value = 7
        c.functions.getTransactionHash.return_value.call.return_value = b"\x00" * 32
        # execTransaction.build_transaction(...) returns a tx dict; the
        # signed_tx mock then exposes .raw_transaction.
        tx_dict = {"to": address, "from": "0xsigner", "nonce": 0,
                   "gas": 500_000, "gasPrice": gas_price_wei}
        c.functions.execTransaction.return_value.build_transaction.return_value = tx_dict
        return c

    def make_ct_helper(address: str, abi):
        c = MagicMock()
        c.functions.balanceOf.return_value.call.return_value = no_balance
        c.functions.getCollectionId.return_value.call.return_value = b"\x11" * 32
        c.functions.getPositionId.return_value.call.return_value = 999
        return c

    fake_w3 = MagicMock()
    fake_w3.eth.block_number = 12345  # for connection-cache check
    fake_w3.eth.gas_price = gas_price_wei
    fake_w3.eth.get_transaction_count.return_value = 1
    fake_w3.is_connected.return_value = True

    # account.sign_transaction returns a MagicMock with .raw_transaction
    signed_tx = MagicMock()
    signed_tx.raw_transaction = b"signed-bytes"
    fake_w3.eth.account.sign_transaction.return_value = signed_tx
    fake_w3.eth.send_raw_transaction.return_value = b"\xab" * 32
    fake_w3.eth.wait_for_transaction_receipt.return_value = {"status": 1}

    fake_w3.to_checksum_address.side_effect = lambda x: x

    def contract(address: str, abi):
        # NEG_RISK_ADAPTER or the CT_ADDRESS for redeem encoding
        if address == NEG_RISK_ADAPTER:
            return make_inner_contract(address, abi)
        # ABI sniffing — Safe ABI has 'execTransaction', helper ABI has 'balanceOf'.
        if any(item.get("name") == "execTransaction" for item in abi):
            return make_safe_contract(address, abi)
        if any(item.get("name") == "balanceOf" for item in abi):
            return make_ct_helper(address, abi)
        # Default: another redeem contract (CT path)
        return make_inner_contract(address, abi)

    fake_w3.eth.contract.side_effect = contract
    return fake_w3


# ── Tests ─────────────────────────────────────────────────────────────

def test_neg_risk_amounts_order_no_first_zero():
    """The crucial inverse of the trade-bot reference: weather bot holds
    NO, so amounts must be ``[0, no_balance]`` — NOT ``[no_balance, 0]``.
    """
    captured: dict = {}
    fake_w3 = _make_fake_w3(no_balance=10_000_000, captured=captured)

    r = Redeemer(
        funder_address="0xfffffffffffffffffffffffffffffffffffffffe",
        private_key="0x" + "11" * 32,
        clob_client=_FakeClob(balance=10_000_000),
    )

    class _Sig:
        class _IntLike:
            def to_bytes(self, n, byteorder):
                return b"\x00" * n
        r = _IntLike()
        s = _IntLike()
        v = 27

    with patch.object(r, "_get_w3", return_value=fake_w3), \
         patch("eth_account.Account.from_key", return_value=MagicMock(address="0xsigner")), \
         patch("eth_account.Account.unsafe_sign_hash", return_value=_Sig()):
        result = r._redeem_sync(
            condition_id="0x" + "ab" * 32,
            neg_risk=True,
            token_id="tok_no",
        )

    assert result.status == "success", f"unexpected status: {result}"
    assert result.redeemed_amount == 10_000_000
    name, args = captured["last_encode_abi_call"]
    assert name == "redeemPositions"
    cid_bytes, amounts = args
    # Bot holds NO → YES=0, NO=balance.  The opposite ordering would
    # silently redeem zero shares.
    assert amounts == [0, 10_000_000], (
        f"amounts ordering wrong: got {amounts}, "
        "must be [0, no_balance] for NO-holding weather bot"
    )
    assert captured["last_target"] == NEG_RISK_ADAPTER


def test_tx_hash_with_leading_zero_nibble_preserved():
    """Regression: ``tx_hash.hex().lstrip("0x")`` ate any leading ``0`` /
    ``x`` characters from the raw hex (since ``str.lstrip`` treats its arg
    as a *char set*, not a prefix).  In web3 7.x / hexbytes 1.x ``.hex()``
    returns the un-prefixed form ``"00ab..."``, so a hash whose first nibble
    is zero (~6% of all hashes) was silently truncated to ``"0xab..."``
    before being persisted/logged.  Fix: ``removeprefix("0x")``.
    """
    captured: dict = {}
    fake_w3 = _make_fake_w3(no_balance=10_000_000, captured=captured)
    # First byte 0x00 → hex() == "00ab..." (32 bytes, leading zero nibble).
    leading_zero_hash = b"\x00\xab" + b"\xcd" * 30
    fake_w3.eth.send_raw_transaction.return_value = leading_zero_hash

    r = Redeemer(
        funder_address="0xfffffffffffffffffffffffffffffffffffffffe",
        private_key="0x" + "11" * 32,
        clob_client=_FakeClob(balance=10_000_000),
    )

    class _Sig:
        class _IntLike:
            def to_bytes(self, n, byteorder):
                return b"\x00" * n
        r = _IntLike()
        s = _IntLike()
        v = 27

    with patch.object(r, "_get_w3", return_value=fake_w3), \
         patch("eth_account.Account.from_key", return_value=MagicMock(address="0xsigner")), \
         patch("eth_account.Account.unsafe_sign_hash", return_value=_Sig()):
        result = r._redeem_sync(
            condition_id="0x" + "ab" * 32,
            neg_risk=True,
            token_id="tok_no",
        )

    assert result.status == "success", f"unexpected status: {result}"
    # Buggy `lstrip("0x")` would have produced "0xabcdcd…" (lost the 00).
    # Fixed `removeprefix("0x")` preserves the full 32-byte hash.
    expected_hex = "0x" + leading_zero_hash.hex()
    assert result.tx_hash == expected_hex, (
        f"tx_hash truncated: got {result.tx_hash}, expected {expected_hex}"
    )
    # Sanity: the full 64-hex-char body is intact (32 bytes × 2 chars + 2 prefix).
    assert len(result.tx_hash) == 66


def test_gas_cap_defers_when_too_expensive():
    """Gas at 100 gwei × 500k limit × $0.50/MATIC → ~$0.025; bumping the
    MATIC price multiplier into the assertion would be brittle, so we
    instead force the cap below the estimate by setting a very low cap.
    """
    fake_w3 = _make_fake_w3(gas_price_wei=300_000_000_000)  # 300 gwei

    r = Redeemer(
        funder_address="0xfffffffffffffffffffffffffffffffffffffffe",
        private_key="0x" + "11" * 32,
        clob_client=_FakeClob(balance=5_000_000),
        gas_cap_usd=0.0001,  # absurdly tight cap → always defer
    )

    with patch.object(r, "_get_w3", return_value=fake_w3):
        result = r._redeem_sync(
            condition_id="0x" + "ab" * 32,
            neg_risk=True,
            token_id="tok_no",
        )

    assert result.status == "gas_too_high"
    assert result.tx_hash is None


def test_no_balance_returns_already_redeemed():
    """CLOB ``get_conditional_balance`` returns 0 → idempotent skip.  No tx is sent."""
    fake_w3 = _make_fake_w3(no_balance=0)

    r = Redeemer(
        funder_address="0xfffffffffffffffffffffffffffffffffffffffe",
        private_key="0x" + "11" * 32,
        clob_client=_FakeClob(balance=0),
    )

    with patch.object(r, "_get_w3", return_value=fake_w3):
        result = r._redeem_sync(
            condition_id="0x" + "ab" * 32,
            neg_risk=True,
            token_id="tok_no",
        )

    assert result.status == "already_redeemed"
    assert result.redeemed_amount == 0
    # No raw_transaction sent — confirm the send_raw_transaction was never called.
    fake_w3.eth.send_raw_transaction.assert_not_called()


def test_no_funder_returns_no_funder_status():
    r = Redeemer(
        funder_address="", private_key="0x" + "11" * 32,
        clob_client=_FakeClob(balance=10_000_000),
    )
    result = r._redeem_sync(
        condition_id="0x" + "ab" * 32, neg_risk=True, token_id="tok_no",
    )
    assert result.status == "no_funder"
    assert result.error and "FUNDER_ADDRESS" in result.error


def test_tx_revert_returns_tx_reverted():
    fake_w3 = _make_fake_w3()
    fake_w3.eth.wait_for_transaction_receipt.return_value = {"status": 0}

    r = Redeemer(
        funder_address="0xfffffffffffffffffffffffffffffffffffffffe",
        private_key="0x" + "11" * 32,
        clob_client=_FakeClob(balance=5_000_000),
    )

    class _Sig:
        class _IntLike:
            def to_bytes(self, n, byteorder):
                return b"\x00" * n
        r = _IntLike()
        s = _IntLike()
        v = 27

    with patch.object(r, "_get_w3", return_value=fake_w3), \
         patch("eth_account.Account.from_key", return_value=MagicMock(address="0xsigner")), \
         patch("eth_account.Account.unsafe_sign_hash", return_value=_Sig()):
        result = r._redeem_sync(
            condition_id="0x" + "ab" * 32, neg_risk=True, token_id="tok_no",
        )

    assert result.status == "tx_reverted"
    assert result.tx_hash is not None


def test_check_condition_resolved_returns_winner_yes_no():
    """Sanity-check the on-chain confirm helper used by the settler.

    payoutDenominator > 0 → finalised; payoutNumerators(0) > 0 → YES win.
    """
    fake_w3 = _make_fake_w3()
    ct = MagicMock()
    ct.functions.payoutDenominator.return_value.call.return_value = 1
    ct.functions.payoutNumerators.return_value.call.return_value = 1  # YES wins
    fake_w3.eth.contract.side_effect = lambda address, abi: ct

    r = Redeemer(
        funder_address="0xfffffffffffffffffffffffffffffffffffffffe",
        private_key="0x" + "11" * 32,
    )
    with patch.object(r, "_get_w3", return_value=fake_w3):
        is_resolved, winner = r.check_condition_resolved("0x" + "ab" * 32)
    assert is_resolved is True
    assert winner == "yes"

    # Now flip to NO winner
    ct.functions.payoutNumerators.return_value.call.return_value = 0
    with patch.object(r, "_get_w3", return_value=fake_w3):
        is_resolved, winner = r.check_condition_resolved("0x" + "ab" * 32)
    assert is_resolved is True
    assert winner == "no"


def test_clob_balance_path_used_not_balanceof():
    """Regression for 2026-04-28: the redeemer must source the NO balance
    from ``ClobClient.get_conditional_balance(token_id)`` — *not* from
    ``ConditionalTokens.balanceOf(funder, positionId)``.  The pre-fix path
    returned 0 for negRisk markets and falsely marked Miami 88-89 +
    Chicago 66-67 ``already_redeemed``.

    We assert the fake CLOB was called with the right token_id and that
    the redeemer used the CLOB's balance (not whatever w3.balanceOf
    might have returned) when assembling the calldata.
    """
    captured: dict = {}
    # _make_fake_w3 wires balanceOf to a default 5_000_000 — if the
    # redeemer regresses to using it, the calldata would reflect 5M
    # instead of the CLOB's 7M.
    fake_w3 = _make_fake_w3(no_balance=5_000_000, captured=captured)
    clob = _FakeClob(balance=7_000_000)

    r = Redeemer(
        funder_address="0xfffffffffffffffffffffffffffffffffffffffe",
        private_key="0x" + "11" * 32,
        clob_client=clob,
    )

    class _Sig:
        class _IntLike:
            def to_bytes(self, n, byteorder):
                return b"\x00" * n
        r = _IntLike()
        s = _IntLike()
        v = 27

    with patch.object(r, "_get_w3", return_value=fake_w3), \
         patch("eth_account.Account.from_key", return_value=MagicMock(address="0xsigner")), \
         patch("eth_account.Account.unsafe_sign_hash", return_value=_Sig()):
        result = r._redeem_sync(
            condition_id="0x" + "ab" * 32, neg_risk=True,
            token_id="my_no_token_42",
        )

    assert result.status == "success"
    assert result.redeemed_amount == 7_000_000
    # CLOB was queried with the supplied token_id, not the condition_id.
    assert clob.calls == ["my_no_token_42"]
    # Calldata reflects the CLOB-sourced balance.
    _, args = captured["last_encode_abi_call"]
    _cid_bytes, amounts = args
    assert amounts == [0, 7_000_000]


def test_clob_balance_exception_treated_as_zero_no_tx():
    """If the CLOB raises (timeout / network), we must NOT fire a
    redeem against an unknown balance.  Treat as ``already_redeemed``
    (idempotent skip) — next cycle retries.
    """
    fake_w3 = _make_fake_w3()
    clob = _FakeClob(raise_exc=RuntimeError("clob timeout"))

    r = Redeemer(
        funder_address="0xfffffffffffffffffffffffffffffffffffffffe",
        private_key="0x" + "11" * 32,
        clob_client=clob,
    )

    with patch.object(r, "_get_w3", return_value=fake_w3):
        result = r._redeem_sync(
            condition_id="0x" + "ab" * 32, neg_risk=True,
            token_id="tok_no",
        )

    assert result.status == "already_redeemed"
    assert result.redeemed_amount == 0
    fake_w3.eth.send_raw_transaction.assert_not_called()


def test_no_clob_client_treated_as_zero_balance():
    """Defensive: the live wiring always injects a ClobClient; if a future
    refactor forgets to pass it, the redeemer must NOT fall back to a
    false-positive redemption.  Returning 0 → ``already_redeemed`` is the
    safe default; an alert / manual check will surface the misconfig.
    """
    fake_w3 = _make_fake_w3()

    r = Redeemer(
        funder_address="0xfffffffffffffffffffffffffffffffffffffffe",
        private_key="0x" + "11" * 32,
        clob_client=None,
    )

    with patch.object(r, "_get_w3", return_value=fake_w3):
        result = r._redeem_sync(
            condition_id="0x" + "ab" * 32, neg_risk=True,
            token_id="tok_no",
        )

    assert result.status == "already_redeemed"
    fake_w3.eth.send_raw_transaction.assert_not_called()


def test_check_condition_resolved_pending_returns_false():
    fake_w3 = _make_fake_w3()
    ct = MagicMock()
    ct.functions.payoutDenominator.return_value.call.return_value = 0
    fake_w3.eth.contract.side_effect = lambda address, abi: ct

    r = Redeemer(
        funder_address="0xfffffffffffffffffffffffffffffffffffffffe",
        private_key="0x" + "11" * 32,
    )
    with patch.object(r, "_get_w3", return_value=fake_w3):
        is_resolved, winner = r.check_condition_resolved("0x" + "ab" * 32)
    assert is_resolved is False
    assert winner is None
