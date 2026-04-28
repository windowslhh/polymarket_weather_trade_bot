"""Verify CLOB-based balance lookup against the live DB (READ-ONLY).

Run after the 2026-04-28 redeemer rewrite (CLOB API path) to confirm
that the new balance query returns the *real* on-chain NO balance for
every position the bot cares about, including the two negRisk positions
the buggy ConditionalTokens.balanceOf path mis-classified as
``already_redeemed`` (Miami 88-89 + Chicago 66-67 on 2026-04-27).

What this script does:
  1. Loads .env + Keychain just like ``src.main``.
  2. Constructs a ClobClient against the live CLOB.
  3. For every position in the live DB that is either ``status='open'``
     or ``status='settled' AND redeem_tx_hash='already_redeemed'``,
     calls ``ClobClient.get_conditional_balance(token_id)`` and prints
     the raw 6-decimals balance vs the DB-recorded ``shares``.

What this script does NOT do:
  - Issue a redeem transaction.
  - Modify the DB.
  - Touch the live bot process.

Expected outcome:
  - id=1 (Miami 88-89, 3.124 shares): balance ≈ 3_124_000 raw — confirms
    the false-positive ``already_redeemed`` was caused by the position-id
    encoding mismatch, not a real on-chain zero.
  - id=2 (Chicago 66-67, 2.481 shares): balance ≈ 2_481_000 raw.
  - id=4/5/6 (open Los Angeles / Miami / Denver): balance ≈
    shares × 1_000_000 — sanity check the open positions also resolve.

If any of these come back 0, the CLOB-API fix is also wrong and we need
to keep investigating before considering the rollback.

Usage:
    .venv/bin/python scripts/verify_redeem_balance.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path

# Make the repo importable when run as a script.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv

from src.config import load_config
from src.markets.clob_client import ClobClient
from src.security import load_eth_private_key


_DB_PATH = _REPO / "data" / "bot.db"


def _select_targets() -> list[dict]:
    """Pull every position relevant to the verification (open + false-settled)."""
    db = sqlite3.connect(str(_DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """SELECT id, status, city, slot_label, token_id, condition_id,
                  neg_risk, shares, entry_price, redeem_tx_hash, redeem_status,
                  realized_pnl, closed_at
             FROM positions
            WHERE status = 'open'
               OR (status = 'settled' AND redeem_tx_hash = 'already_redeemed')
            ORDER BY id ASC"""
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


async def _check_one(clob: ClobClient, pos: dict) -> dict:
    """Query CLOB balance for ``pos['token_id']`` and assemble a report row."""
    token_id = pos["token_id"]
    raw_balance = await clob.get_conditional_balance(token_id)
    expected_raw = int(round(float(pos["shares"]) * 1_000_000))
    return {
        "id": pos["id"],
        "status": pos["status"],
        "city": pos["city"],
        "slot": (pos["slot_label"] or "")[:60],
        "neg_risk": pos["neg_risk"],
        "shares": pos["shares"],
        "expected_raw": expected_raw,
        "actual_raw": raw_balance,
        "diff": raw_balance - expected_raw,
        "ok": raw_balance > 0,
        "tx_hash": pos["redeem_tx_hash"],
    }


async def _main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("verify_redeem_balance")

    load_dotenv(_REPO / ".env")

    config = load_config()
    if not config.funder_address:
        logger.error("FUNDER_ADDRESS not set; cannot query funder balance.")
        return 1
    try:
        config.eth_private_key = load_eth_private_key()
    except RuntimeError as exc:
        logger.error("Private key load failed: %s", exc)
        return 1
    # Force live signing path (ClobClient short-circuits on paper/dry-run).
    config.dry_run = False
    config.paper = False

    targets = _select_targets()
    if not targets:
        logger.warning("No matching positions in DB — nothing to verify.")
        return 0

    logger.info(
        "Verifying CLOB balance for %d position(s) against funder=%s",
        len(targets),
        config.funder_address[:10] + "..." + config.funder_address[-6:],
    )

    clob = ClobClient(config)
    reports = []
    for pos in targets:
        try:
            rep = await _check_one(clob, pos)
        except Exception as exc:  # pragma: no cover (live network)
            logger.exception("CLOB query failed for position %d", pos["id"])
            rep = {
                "id": pos["id"],
                "status": pos["status"],
                "city": pos["city"],
                "slot": (pos["slot_label"] or "")[:60],
                "neg_risk": pos["neg_risk"],
                "shares": pos["shares"],
                "expected_raw": int(round(float(pos["shares"]) * 1_000_000)),
                "actual_raw": -1,
                "diff": None,
                "ok": False,
                "tx_hash": pos["redeem_tx_hash"],
                "error": str(exc),
            }
        reports.append(rep)

    print()
    print(
        f"{'id':>3} {'status':<8} {'city':<14} {'neg_risk':<8} "
        f"{'shares':>10} {'expected_raw':>13} {'actual_raw':>13} "
        f"{'diff':>10} {'tx_hash':<20}"
    )
    print("-" * 110)
    for r in reports:
        print(
            f"{r['id']:>3} {r['status']:<8} {r['city'][:14]:<14} "
            f"{('Y' if r['neg_risk'] else 'N'):<8} "
            f"{r['shares']:>10.4f} {r['expected_raw']:>13,d} "
            f"{r['actual_raw']:>13,d} "
            f"{r['diff'] if r['diff'] is not None else 'n/a':>10} "
            f"{(r['tx_hash'] or '<none>')[:20]:<20}"
        )

    print()
    bad = [r for r in reports if r["actual_raw"] <= 0]
    if not bad:
        print(
            "RESULT: PASS — all positions returned non-zero CLOB balance. "
            "The new CLOB-API path resolves real on-chain shares."
        )
        return 0
    print(
        f"RESULT: FAIL — {len(bad)} position(s) returned 0 (or error). "
        "CLOB-API fix is also wrong; investigate before continuing."
    )
    for r in bad:
        print(f"  - id={r['id']} {r['city']} {r['slot']}")
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
