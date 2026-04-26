"""G-4 (2026-04-26): wallet balance + nonce monitor.

Two checks, run on startup and every hour:

1. **USDC balance**: query the Polymarket proxy wallet's USDC balance
   via the SDK's ``get_balance_allowance`` (asset_type=COLLATERAL).
   Critical alert when below ``min_wallet_balance_usd``.  Pre-fix the
   bot would happily issue BUYs that the broker rejected for "insufficient
   balance" — visible only in CLOB error logs, not on the dashboard.

2. **Nonce**: query the signer EOA's transaction count from Polygon RPC
   (``eth_getTransactionCount``).  An ever-growing nonce is normal; a
   "stuck" transaction (e.g. underpriced gas during a fee spike) shows
   up as the nonce staying flat while the bot keeps trying to sign
   higher nonces.  We just record the nonce each cycle and log it so
   ops can spot stuck-tx symptoms in operator logs.  No automated
   alert on nonce alone (false-positive risk too high without baseline).

Skipped in paper / dry-run mode (no real wallet to query).  The
monitor is wired into the scheduler as a 60-min interval job.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def check_wallet_balance(clob_client) -> tuple[bool, float | None, str]:
    """Query the proxy wallet's USDC balance via py-clob-client.

    Returns ``(ok, balance_usd, message)`` where:
      - ``ok=True`` and ``balance_usd`` populated on success
      - ``ok=False`` on any error (network, SDK exception, parse fail).
        ``balance_usd`` may be None.

    Caller compares the value against the configured floor and sends
    the critical alert; this function stays narrowly scoped to "did
    the SDK hand back a number?".
    """
    try:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
    except ImportError:
        return False, None, "py-clob-client not installed"
    try:
        client = clob_client._get_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        resp = await asyncio.to_thread(
            client.get_balance_allowance, params,
        )
    except Exception as exc:
        return False, None, f"get_balance_allowance failed: {exc}"
    # Polymarket returns balance in USDC base units (6 decimals).  The
    # SDK shape is ``{"balance": "12345678", "allowance": "..."}``;
    # divide by 1e6 to get USD.
    raw = resp.get("balance") if isinstance(resp, dict) else None
    if raw is None:
        return False, None, f"unexpected SDK response shape: {resp!r}"
    try:
        balance_usd = float(raw) / 1_000_000.0
    except (ValueError, TypeError):
        return False, None, f"balance not numeric: {raw!r}"
    return True, balance_usd, f"wallet_balance_ok({balance_usd:.2f} USDC)"


async def check_signer_nonce(rpc_url: str, address: str) -> tuple[bool, int | None, str]:
    """Query the signer EOA's transaction count from Polygon RPC.

    Returns ``(ok, nonce, message)``.  Failure (network / RPC error)
    returns ``ok=False`` with ``nonce=None``; a healthy response gives
    the integer nonce.  Per the module docstring we do NOT compare
    against a previous value here — caller logs and history is in the
    operator log channel.
    """
    if not address:
        return False, None, "no signer address supplied"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getTransactionCount",
        "params": [address, "latest"],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(rpc_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return False, None, f"polygon RPC unreachable: {exc}"
    if "result" not in data:
        return False, None, f"polygon RPC error: {data!r}"
    try:
        nonce = int(data["result"], 16)
    except (ValueError, TypeError):
        return False, None, f"nonce not parseable: {data['result']!r}"
    return True, nonce, f"nonce_ok({nonce})"


async def run_wallet_monitor(
    clob_client,
    alerter,
    *,
    rpc_url: str,
    min_balance_usd: float,
    is_paper: bool,
    is_dry_run: bool,
) -> None:
    """Combined balance + nonce check; called on startup and hourly.

    Paper / dry-run mode short-circuits with a debug log — no wallet
    to query.  In live mode:
      - Balance below floor → critical alert
      - Balance fetch fails → critical alert (could be auth or
        Polymarket API outage; either way the operator should see it)
      - Nonce fetch fails → logger.warning only (RPC blips are common
        and not trade-blocking)
      - Nonce fetched → logger.info with the value (history in logs)
    """
    if is_paper or is_dry_run:
        logger.debug("Wallet monitor: skipped (paper/dry-run mode)")
        return

    bal_ok, balance_usd, bal_msg = await check_wallet_balance(clob_client)
    if not bal_ok:
        await alerter.send(
            "critical",
            f"Wallet monitor: balance check FAILED — {bal_msg}",
        )
        logger.error("Wallet monitor: %s", bal_msg)
    elif balance_usd is not None and balance_usd < min_balance_usd:
        await alerter.send(
            "critical",
            f"Wallet monitor: USDC balance ${balance_usd:.2f} "
            f"below floor ${min_balance_usd:.2f} — refill before next cycle",
        )
        logger.error(
            "Wallet monitor: balance %.2f < floor %.2f", balance_usd, min_balance_usd,
        )
    else:
        logger.info("Wallet monitor: %s", bal_msg)

    # Nonce: best-effort, no alert on its own.
    try:
        client = clob_client._get_client()
        address = await asyncio.to_thread(client.get_address)
    except Exception as exc:
        logger.warning("Wallet monitor: signer address lookup failed: %s", exc)
        return
    nonce_ok, nonce, nonce_msg = await check_signer_nonce(rpc_url, address)
    if nonce_ok:
        logger.info("Wallet monitor: %s for %s", nonce_msg, address[:10])
    else:
        logger.warning("Wallet monitor: %s", nonce_msg)
