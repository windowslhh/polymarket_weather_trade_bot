"""On-chain redemption for settled NO positions.

Polymarket's settlement closes a binary market and assigns each ERC1155
share a payout (1 USDC if the side won, 0 if it lost).  Until somebody
calls ``redeemPositions`` against the ConditionalTokens / NegRiskAdapter
contract, the funder's NO ERC1155 balance just sits there — the USDC
is sent to the funder's Safe at redeem time, not at settlement.

The settler owns the *trigger* (per-market closed=true → redeem); this
module owns the *call*.  It encodes the redeemPositions calldata, wraps
it in a Gnosis Safe execTransaction (the EOA can't redeem directly
because the funder Safe holds the shares), signs with the bot's L1 key,
and submits.

Hardenings vs. the trade-bot reference (``polymarket_trade_bot/src/client.py``):

  a. **on-chain balance check** — read ``ConditionalTokens.balanceOf(funder,
     positionId)`` first; redeem against the on-chain amount, never the
     DB-recorded ``shares``.  Catches partial fills, manual redeems, and
     stale state.  ``positionId`` is computed from ``conditionId + indexSet``
     per the Gnosis docs (see ``compute_position_id`` below).

  b. **gas cap** — abort if estimated gas cost exceeds ``$0.50`` (default).
     Polygon gas spikes during congestion can wipe out ~$3 redemptions; an
     up-front cap defers the redeem to a calmer block.  MATIC USD price is
     hardcoded at $0.50 for now (not market-sensitive enough to justify an
     oracle for a $0.50 budget; reroute to an oracle once we have one).

  c. **pending-tx race protection** — the *settler* atomically flips
     ``redeem_status`` from NULL → 'pending' before calling us.  We assume
     the row is already ours; on receipt we report status='success' so the
     settler can write the real tx_hash + flip status.  If we crash between
     send and receipt, the settler's next cycle sees status='pending', skips
     the row, and an operator can clear it (manual or via the timeout
     reaper, future work).

  d. **negRisk amounts ordering** — Polymarket convention: outcome 0 = YES,
     outcome 1 = NO.  Weather bot holds NO, so amounts = ``[0, no_balance]``.
     This is the inverse of the trade-bot reference (which holds YES).
     Validated by ``tests/test_redeemer.py``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Polygon mainnet contract addresses.  Mirrors trade-bot client.py to
# keep one source of truth per chain.
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CT_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# Polygon RPCs in fallback order.  Web3.HTTPProvider doesn't load-balance,
# so we walk the list at connect time and reuse the first that responds.
_POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon-rpc.com",
]

# Hardcoded MATIC/USD for the gas-cap heuristic.  Bumping with the market
# is not worth an oracle round-trip for a $0.50 budget; revisit if we ever
# trade large-cap settle redemptions where the gas cost actually matters.
MATIC_USD_PRICE = 0.50

# Max acceptable gas cost in USD for a single redeem.  Exceed → defer.
DEFAULT_GAS_CAP_USD = 0.50

# Estimated upper bound on Safe execTransaction gas for a redeemPositions
# inner call.  Trade bot uses 500_000 as the build_transaction gas; we use
# the same for the up-front cost estimate.
GAS_LIMIT_ESTIMATE = 500_000

# Inner ABI fragments — minimal surface needed for redeem + balance lookups.
_NEG_RISK_REDEEM_ABI = [{
    "name": "redeemPositions",
    "type": "function",
    "stateMutability": "nonpayable",
    "inputs": [
        {"name": "_conditionId", "type": "bytes32"},
        {"name": "_amounts", "type": "uint256[]"},
    ],
    "outputs": [],
}]

_CT_REDEEM_ABI = [{
    "name": "redeemPositions",
    "type": "function",
    "stateMutability": "nonpayable",
    "inputs": [
        {"name": "collateralToken", "type": "address"},
        {"name": "parentCollectionId", "type": "bytes32"},
        {"name": "conditionId", "type": "bytes32"},
        {"name": "indexSets", "type": "uint256[]"},
    ],
    "outputs": [],
}]

# ConditionalTokens ERC1155 + collection ID helpers — used to compute the
# per-outcome positionId we ask balanceOf for.
_CT_HELPER_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "getCollectionId",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSet", "type": "uint256"},
        ],
        "outputs": [{"type": "bytes32"}],
    },
    {
        "name": "getPositionId",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "collectionId", "type": "bytes32"},
        ],
        "outputs": [{"type": "uint256"}],
    },
]

_SAFE_ABI = [
    {
        "name": "execTransaction", "type": "function",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures", "type": "bytes"},
        ],
        "outputs": [{"type": "bool"}],
    },
    {
        "name": "nonce", "type": "function",
        "inputs": [], "outputs": [{"type": "uint256"}],
    },
    {
        "name": "getTransactionHash", "type": "function",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "_nonce", "type": "uint256"},
        ],
        "outputs": [{"type": "bytes32"}],
    },
]


@dataclass
class RedeemResult:
    """Outcome of a single redeem attempt.

    status values:
      - ``success``           tx confirmed, ERC1155 burned, USDC received
      - ``already_redeemed``  on-chain balance was already 0 (idempotent re-call)
      - ``no_balance``        on-chain balance is 0 *and* DB thought we had some
                              (likely manual redeem or stale state — caller can
                              treat as success but should reconcile shares)
      - ``gas_too_high``      transient: gas cost > cap, retry next cycle
      - ``tx_reverted``       receipt.status == 0 — usually means the inner
                              redeemPositions reverted (wrong index_set, etc.)
      - ``rpc_error``         network / web3 / Safe failure; retry next cycle
      - ``no_funder``         config missing FUNDER_ADDRESS — cannot redeem
    """
    status: str
    tx_hash: str | None = None
    redeemed_amount: int = 0  # raw 6-decimals USDC
    error: str | None = None


class Redeemer:
    """Thin async facade over the redeemPositions on-chain call.

    Constructed once at startup in ``main.py``; the settler injects it
    when iterating per-market settlements.

    All web3 calls are sync; we run them inside ``asyncio.to_thread``
    so the bot's event loop isn't blocked while we wait on Polygon
    receipts (typically 2-10 sec).
    """

    def __init__(
        self,
        funder_address: str,
        private_key: str,
        polygon_rpc_urls: list[str] | None = None,
        gas_cap_usd: float = DEFAULT_GAS_CAP_USD,
    ) -> None:
        self._funder = funder_address
        self._private_key = private_key
        self._rpcs = polygon_rpc_urls or _POLYGON_RPCS
        self._gas_cap_usd = gas_cap_usd
        self._w3 = None  # lazy

    def _get_w3(self):
        # Cached; reconnect on dead handle.  Same pattern as trade-bot
        # client.py::_get_w3 — bot survives transient RPC outages by
        # walking the fallback list on the next call.
        from web3 import Web3
        if self._w3 is not None:
            try:
                self._w3.eth.block_number
                return self._w3
            except Exception:
                logger.warning("Polygon RPC connection lost, reconnecting...")
                self._w3 = None
        for rpc_url in self._rpcs:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
                if w3.is_connected():
                    logger.debug("Connected to Polygon RPC: %s", rpc_url)
                    self._w3 = w3
                    return self._w3
            except Exception:
                continue
        # Last resort: return the first provider even if it didn't
        # respond, so callers can at least surface a real exception
        # rather than NoneType.
        self._w3 = Web3(Web3.HTTPProvider(self._rpcs[0]))
        return self._w3

    @staticmethod
    def compute_position_id(
        collateral_token: str,
        condition_id: str,
        index_set: int,
        w3,
    ) -> int:
        """Compute the ERC1155 ``positionId`` for ``balanceOf`` lookup.

        Per the Gnosis ConditionalTokens design:
          collectionId = getCollectionId(parent=0x0, conditionId, indexSet)
          positionId   = getPositionId(collateralToken, collectionId)

        We delegate to the on-chain helpers rather than re-implementing
        the keccak math in-process — it's a single eth_call each (cheap)
        and avoids subtle keccak/abi.encode bugs.

        For NegRiskAdapter markets, the adapter's redeem path doesn't
        use these IDs (it uses amounts[0] / amounts[1] directly), but
        the underlying ERC1155 balance still maps to a CT positionId
        with NegRiskAdapter as the collateral wrapper.  For the simple
        balance check we compute against the standard CT to get a usable
        per-outcome lower bound — the redeem call itself supplies the
        amounts directly.
        """
        ct = w3.eth.contract(
            address=w3.to_checksum_address(CT_ADDRESS),
            abi=_CT_HELPER_ABI,
        )
        cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
        collection_id = ct.functions.getCollectionId(
            b"\x00" * 32, cid_bytes, index_set,
        ).call()
        position_id = ct.functions.getPositionId(
            w3.to_checksum_address(collateral_token), collection_id,
        ).call()
        return int(position_id)

    def _on_chain_no_balance(self, condition_id: str, w3) -> int:
        """Read funder's ERC1155 NO balance for ``condition_id``.

        NegRiskAdapter splits map outcomes as YES=indexSet=1, NO=indexSet=2
        on the underlying CT.  We sum the relevant balance using indexSet=2
        (NO).  Returns 0 on any RPC failure — caller treats 0 as
        ``no_balance`` (idempotent — never a false positive redemption).
        """
        try:
            position_id = self.compute_position_id(
                USDC_ADDRESS, condition_id, 2, w3,
            )
            ct = w3.eth.contract(
                address=w3.to_checksum_address(CT_ADDRESS),
                abi=_CT_HELPER_ABI,
            )
            funder = w3.to_checksum_address(self._funder)
            return int(ct.functions.balanceOf(funder, position_id).call())
        except Exception as exc:
            logger.warning(
                "balanceOf lookup failed for cid=%s: %s — assuming 0",
                condition_id[:16] + "...", exc,
            )
            return 0

    def _estimate_gas_cost_usd(self, w3) -> float:
        """Estimate USD cost of the upcoming redeem tx."""
        gas_price_wei = int(w3.eth.gas_price)
        cost_wei = gas_price_wei * GAS_LIMIT_ESTIMATE
        cost_matic = cost_wei / 1e18
        return cost_matic * MATIC_USD_PRICE

    def _build_redeem_calldata(self, condition_id: str, neg_risk: bool,
                                no_amount_raw: int, w3) -> tuple[str, str]:
        """Encode the inner redeemPositions call.

        Returns ``(target_address, calldata_hex)``.  ``calldata_hex`` starts
        with ``0x``; the Safe wrapper strips that before passing to
        execTransaction.

        For neg_risk: amounts is ``[YES, NO]`` per Polymarket convention.
        Weather bot holds NO, so YES=0 / NO=balance.  This is the *inverse*
        of the trade-bot reference (it holds YES).
        """
        cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
        if neg_risk:
            target = w3.to_checksum_address(NEG_RISK_ADAPTER)
            inner = w3.eth.contract(address=target, abi=_NEG_RISK_REDEEM_ABI)
            calldata = inner.encode_abi(
                "redeemPositions",
                args=[cid_bytes, [0, no_amount_raw]],
            )
        else:
            target = w3.to_checksum_address(CT_ADDRESS)
            ct = w3.eth.contract(address=target, abi=_CT_REDEEM_ABI)
            # indexSets = [1 (YES), 2 (NO)] — per ConditionalTokens
            # standard, the redeem returns the holder's full position
            # against any of the listed outcomes.  We pass both so a
            # NO win or a degenerate YES win both clear cleanly.
            calldata = ct.encode_abi(
                "redeemPositions",
                args=[
                    w3.to_checksum_address(USDC_ADDRESS),
                    b"\x00" * 32, cid_bytes, [1, 2],
                ],
            )
        return target, calldata

    def _redeem_sync(self, condition_id: str, neg_risk: bool) -> RedeemResult:
        """Blocking redeem implementation; wrapped by ``redeem_position``.

        Pulled out as a method so async callers can ``asyncio.to_thread``
        without leaking tx-construction logic into the event loop.
        """
        if not self._funder:
            return RedeemResult(status="no_funder", error="FUNDER_ADDRESS not set")

        try:
            from eth_account import Account

            w3 = self._get_w3()
            no_balance = self._on_chain_no_balance(condition_id, w3)
            if no_balance == 0:
                # Idempotent: nothing to redeem.  Caller maps to success.
                return RedeemResult(status="already_redeemed", redeemed_amount=0)

            estimated_cost = self._estimate_gas_cost_usd(w3)
            if estimated_cost > self._gas_cap_usd:
                logger.warning(
                    "Redeem deferred: gas $%.4f > cap $%.4f (cid=%s)",
                    estimated_cost, self._gas_cap_usd,
                    condition_id[:16] + "...",
                )
                return RedeemResult(
                    status="gas_too_high",
                    error=f"estimated ${estimated_cost:.4f} > cap ${self._gas_cap_usd:.4f}",
                )

            target, calldata_hex = self._build_redeem_calldata(
                condition_id, neg_risk, no_balance, w3,
            )

            account = Account.from_key(self._private_key)
            signer = account.address
            funder = w3.to_checksum_address(self._funder)
            zero_addr = "0x0000000000000000000000000000000000000000"

            safe = w3.eth.contract(address=funder, abi=_SAFE_ABI)
            data_bytes = bytes.fromhex(calldata_hex[2:])
            nonce = safe.functions.nonce().call()

            tx_hash_bytes = safe.functions.getTransactionHash(
                target, 0, data_bytes,
                0, 0, 0, 0, zero_addr, zero_addr, nonce,
            ).call()

            signed_msg = Account.unsafe_sign_hash(
                tx_hash_bytes, self._private_key,
            )
            sig = (
                signed_msg.r.to_bytes(32, "big")
                + signed_msg.s.to_bytes(32, "big")
                + bytes([signed_msg.v])
            )

            tx = safe.functions.execTransaction(
                target, 0, data_bytes,
                0, 0, 0, 0, zero_addr, zero_addr, sig,
            ).build_transaction({
                "from": signer,
                "nonce": w3.eth.get_transaction_count(signer),
                "gas": GAS_LIMIT_ESTIMATE,
                "gasPrice": w3.eth.gas_price,
            })

            signed_tx = w3.eth.account.sign_transaction(tx, self._private_key)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            tx_hex = "0x" + tx_hash.hex().removeprefix("0x")
            if receipt["status"] == 1:
                logger.info(
                    "Redeemed cid=%s amount=%d tx=%s",
                    condition_id[:16] + "...", no_balance, tx_hex,
                )
                return RedeemResult(
                    status="success", tx_hash=tx_hex,
                    redeemed_amount=no_balance,
                )
            logger.error(
                "Redeem reverted cid=%s tx=%s",
                condition_id[:16] + "...", tx_hex,
            )
            return RedeemResult(
                status="tx_reverted", tx_hash=tx_hex,
                error="receipt.status == 0",
            )
        except Exception as exc:
            logger.exception(
                "Redeem RPC error cid=%s",
                condition_id[:16] + "...",
            )
            return RedeemResult(status="rpc_error", error=str(exc))

    async def redeem_position(
        self, condition_id: str, neg_risk: bool,
    ) -> RedeemResult:
        """Async wrapper — drives the sync impl in a thread."""
        return await asyncio.to_thread(
            self._redeem_sync, condition_id, neg_risk,
        )

    def check_condition_resolved(self, condition_id: str) -> tuple[bool, str | None]:
        """Read on-chain payout to confirm a condition is finalized.

        Returns ``(is_resolved, winner)`` where winner is ``"yes"``, ``"no"``,
        or None.  Used by the settler as a hard confirm before issuing the
        redeem call — Gamma's ``closed=true`` can flip 30+ minutes before
        on-chain payout finalizes (dispute window), and a redeem against
        an unfinalized condition reverts.

        Mirrors the trade-bot helper exactly so a future audit can diff
        against ``polymarket_trade_bot/src/client.py``.
        """
        try:
            w3 = self._get_w3()
            ct_abi = [
                {
                    "name": "payoutNumerators",
                    "type": "function",
                    "stateMutability": "view",
                    "inputs": [
                        {"name": "conditionId", "type": "bytes32"},
                        {"name": "index", "type": "uint256"},
                    ],
                    "outputs": [{"type": "uint256"}],
                },
                {
                    "name": "payoutDenominator",
                    "type": "function",
                    "stateMutability": "view",
                    "inputs": [{"name": "conditionId", "type": "bytes32"}],
                    "outputs": [{"type": "uint256"}],
                },
            ]
            ct = w3.eth.contract(
                address=w3.to_checksum_address(CT_ADDRESS),
                abi=ct_abi,
            )
            cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            denom = ct.functions.payoutDenominator(cid_bytes).call()
            if denom == 0:
                return False, None
            yes_payout = ct.functions.payoutNumerators(cid_bytes, 0).call()
            return True, ("yes" if yes_payout > 0 else "no")
        except Exception as exc:
            logger.debug(
                "On-chain resolution check failed cid=%s: %s",
                condition_id[:16] + "...", exc,
            )
            return False, None

    async def check_condition_resolved_async(
        self, condition_id: str,
    ) -> tuple[bool, str | None]:
        """Async facade for the above — settler is async."""
        return await asyncio.to_thread(
            self.check_condition_resolved, condition_id,
        )
