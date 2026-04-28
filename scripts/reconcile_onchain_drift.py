#!/Users/marathon/polymarket_weather_trade_bot/.venv/bin/python
"""Reconcile LOCAL DB drift against on-chain Polymarket positions.

Background (2026-04-28 / 2026-04-29 cutover):
The bot was running an older codepath where (a) GTC orders rested as
makers when last-trade price < best ask and (b) a defensive A1 cancel
in the executor then cancelled our own resting orders.  The fallout:
- Some on-chain BUYs went unrecorded because the wrapper's matched
  detection treated resting orders as failures (and the executor
  cancelled them, so they never actually rested for long — but a few
  managed to fill before cancel landed and the DB never wrote a row).
- Some closed positions weren't recorded as closed because the bot
  emitted EXIT signals that did execute on chain but the DB write
  raced and was lost / orphaned.
- A position was inserted without ``condition_id`` or ``neg_risk``,
  which would break redemption when the market settles.

This is a one-shot reconciler that fixes the THREE specific drifts
observed at 2026-04-28 23:55 BJT.  The candidate rows are hard-coded
defensively (refuses to touch anything else) so this script is safe
to re-run if anything goes wrong.

Default is dry-run: prints the candidate(s), the diff, and what would
change.  Pass ``--execute`` to write.

Usage:
    .venv/bin/python scripts/reconcile_onchain_drift.py
    .venv/bin/python scripts/reconcile_onchain_drift.py --execute
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "bot.db"


# Drift 1: close stale Miami 86-87 April 28 (id=5) — sold on chain at 0.10
DRIFT_1_POSITION_ID = 5
DRIFT_1_EXPECT_TOKEN_ID = (
    "92940678720880581400306315021030802103659220894280219621492295206604798830383"
)
DRIFT_1_EXPECT_STATUS = "open"
DRIFT_1_EXIT_PRICE = 0.10
DRIFT_1_EXIT_REASON = "external_sell_drift_2026_04_28"
DRIFT_1_CLOSED_AT = "2026-04-28 13:42:02"
DRIFT_1_CHAIN_SELL_TX = (
    "0x34161167be47ecec256c32a8d26eddffa1ee7789551d0244e05d7b78ee6e6c7f"
)
# size_usd=2.42 is stored on the row; sale proceeds = 3.42 * 0.10 = $0.342;
# loss = -2.078.  Use stored size_usd to keep dollar arithmetic deterministic.

# Drift 2: insert missing Atlanta 71-or-below NO BUY (chain-only)
DRIFT_2_TOKEN_ID = (
    "65870453147471103770203271770551888816024269668286294483188114791242302466136"
)
DRIFT_2_CONDITION_ID = (
    "0xcd06d682c54f53b7141d6ccfc0191323928d0837a03a2929a4bed6cd19997ac7"
)
DRIFT_2_EVENT_ID = "418829"
DRIFT_2_CITY = "Atlanta"
DRIFT_2_SLOT_LABEL = (
    "Will the highest temperature in Atlanta be 71°F or below on April 28?"
)
DRIFT_2_SHARES = 6.89
DRIFT_2_PRICE = 0.6927
DRIFT_2_SIZE_USD = 4.77  # 6.89 * 0.6927 ≈ 4.7727
DRIFT_2_CREATED_AT = "2026-04-28 13:19:44"
DRIFT_2_CHAIN_BUY_TX = (
    "0x50b3fb452915bc2603d999df96eeb9e6ed678581ff338d52457c5677c3d89a1e"
)
DRIFT_2_STRATEGY = "D"
DRIFT_2_BUY_REASON = "chain_only_recovered_2026_04_28"

# Drift 3: backfill condition_id + neg_risk on id=7 (Miami 86-87 April 29)
DRIFT_3_POSITION_ID = 7
DRIFT_3_EXPECT_TOKEN_ID = (
    "69190488453074786854018294758570921588640634479324317585154072420773545507753"
)
DRIFT_3_CONDITION_ID = (
    "0x69303c4444b45f8e0bb06246dca3707a3f7d4e26bfac903dbb069da80eeb58a4"
)
DRIFT_3_NEG_RISK = 1


def _row_to_dict(cur: sqlite3.Cursor, row: sqlite3.Row) -> dict:
    return {desc[0]: row[idx] for idx, desc in enumerate(cur.description)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true",
                        help="Write changes (default: dry-run, prints diff)")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: db not found at {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print(f"DB: {DB_PATH}")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY-RUN (use --execute to write)'}")
    print("=" * 78)

    # === Drift 1: close id=5 Miami 86-87 April 28 ============================
    print("\n[Drift 1] Close stale Miami 86-87 April 28 (id=5)")
    cur.execute(
        "SELECT * FROM positions WHERE id = ?",
        (DRIFT_1_POSITION_ID,),
    )
    row = cur.fetchone()
    if row is None:
        print(f"  ABORT: position id={DRIFT_1_POSITION_ID} not found")
        return 2
    d = _row_to_dict(cur, row)
    if d["token_id"] != DRIFT_1_EXPECT_TOKEN_ID:
        print(f"  ABORT: token_id mismatch on id={DRIFT_1_POSITION_ID}")
        print(f"    expected: {DRIFT_1_EXPECT_TOKEN_ID}")
        print(f"    actual:   {d['token_id']}")
        return 2
    if d["status"] != DRIFT_1_EXPECT_STATUS:
        print(f"  SKIP: id={DRIFT_1_POSITION_ID} already status={d['status']!r}, "
              f"not {DRIFT_1_EXPECT_STATUS!r} — already reconciled")
    else:
        size_usd = float(d["size_usd"] or 0.0)
        shares = float(d["shares"] or 0.0)
        # Use the on-chain SELL: 3.42 shares @ 0.10 = $0.342 proceeds.
        # Cost basis is the stored size_usd ($2.42 from the BUY).
        # Realized P&L = proceeds - cost_basis.
        # The 0.062-share gap between stored shares (3.482) and on-chain
        # SELL size (3.42) is consistent with a small fee/rounding loss
        # at the BUY, and is materially zero ($0.04 at exit price 0.1).
        # We treat the position as fully closed.
        proceeds = 3.42 * DRIFT_1_EXIT_PRICE
        realized_pnl = proceeds - size_usd
        print(f"  current : status={d['status']} shares={shares:.4f} "
              f"entry={d['entry_price']} size_usd={size_usd:.4f}")
        print(f"  on-chain: SELL 3.42 @ {DRIFT_1_EXIT_PRICE} "
              f"(tx {DRIFT_1_CHAIN_SELL_TX[:10]}...)")
        print(f"  apply   : status='closed' exit_price={DRIFT_1_EXIT_PRICE} "
              f"exit_reason={DRIFT_1_EXIT_REASON!r} closed_at={DRIFT_1_CLOSED_AT!r} "
              f"realized_pnl={realized_pnl:+.4f}")
        if args.execute:
            cur.execute(
                "UPDATE positions SET status='closed', exit_price=?, "
                "exit_reason=?, closed_at=?, realized_pnl=? WHERE id=?",
                (DRIFT_1_EXIT_PRICE, DRIFT_1_EXIT_REASON,
                 DRIFT_1_CLOSED_AT, realized_pnl, DRIFT_1_POSITION_ID),
            )
            print(f"  WROTE: rowcount={cur.rowcount}")

    # === Drift 2: insert Atlanta 71-or-below NO ==============================
    print("\n[Drift 2] Insert missing Atlanta 71-or-below NO (chain-only BUY)")
    cur.execute(
        "SELECT id, status FROM positions WHERE token_id=? AND status IN ('open','settled')",
        (DRIFT_2_TOKEN_ID,),
    )
    existing = cur.fetchone()
    if existing is not None:
        print(f"  SKIP: positions row already exists id={existing[0]} "
              f"status={existing[1]!r} — already reconciled")
    else:
        print(f"  on-chain: BUY {DRIFT_2_SHARES} NO @ {DRIFT_2_PRICE} "
              f"(tx {DRIFT_2_CHAIN_BUY_TX[:10]}...)")
        print(f"  apply   : INSERT positions: city={DRIFT_2_CITY} "
              f"slot_label={DRIFT_2_SLOT_LABEL!r} strategy={DRIFT_2_STRATEGY} "
              f"shares={DRIFT_2_SHARES} entry_price={DRIFT_2_PRICE} "
              f"size_usd={DRIFT_2_SIZE_USD} status='open' neg_risk=1")
        if args.execute:
            cur.execute(
                "INSERT INTO positions ("
                "event_id, token_id, token_type, city, slot_label, side, "
                "entry_price, size_usd, shares, status, strategy, "
                "created_at, buy_reason, source_order_id, match_price, "
                "condition_id, neg_risk"
                ") VALUES (?, ?, 'NO', ?, ?, 'BUY', ?, ?, ?, 'open', ?, "
                "?, ?, ?, ?, ?, 1)",
                (
                    DRIFT_2_EVENT_ID,
                    DRIFT_2_TOKEN_ID,
                    DRIFT_2_CITY,
                    DRIFT_2_SLOT_LABEL,
                    DRIFT_2_PRICE,
                    DRIFT_2_SIZE_USD,
                    DRIFT_2_SHARES,
                    DRIFT_2_STRATEGY,
                    DRIFT_2_CREATED_AT,
                    DRIFT_2_BUY_REASON,
                    f"chain:{DRIFT_2_CHAIN_BUY_TX}",
                    DRIFT_2_PRICE,
                    DRIFT_2_CONDITION_ID,
                ),
            )
            print(f"  WROTE: rowcount={cur.rowcount} new_id={cur.lastrowid}")

    # === Drift 3: backfill id=7 condition_id + neg_risk =====================
    print("\n[Drift 3] Backfill id=7 (Miami 86-87 April 29) condition_id + neg_risk")
    cur.execute(
        "SELECT * FROM positions WHERE id = ?",
        (DRIFT_3_POSITION_ID,),
    )
    row = cur.fetchone()
    if row is None:
        print(f"  ABORT: position id={DRIFT_3_POSITION_ID} not found")
        return 2
    d = _row_to_dict(cur, row)
    if d["token_id"] != DRIFT_3_EXPECT_TOKEN_ID:
        print(f"  ABORT: token_id mismatch on id={DRIFT_3_POSITION_ID}")
        return 2
    if (d["condition_id"] or "") == DRIFT_3_CONDITION_ID and d["neg_risk"] == DRIFT_3_NEG_RISK:
        print(f"  SKIP: id={DRIFT_3_POSITION_ID} already has correct "
              f"condition_id and neg_risk — already reconciled")
    else:
        print(f"  current : condition_id={d['condition_id']!r} neg_risk={d['neg_risk']}")
        print(f"  on-chain: condition_id={DRIFT_3_CONDITION_ID} neg_risk=1")
        print(f"  apply   : condition_id={DRIFT_3_CONDITION_ID} neg_risk={DRIFT_3_NEG_RISK}")
        if args.execute:
            cur.execute(
                "UPDATE positions SET condition_id=?, neg_risk=? WHERE id=?",
                (DRIFT_3_CONDITION_ID, DRIFT_3_NEG_RISK, DRIFT_3_POSITION_ID),
            )
            print(f"  WROTE: rowcount={cur.rowcount}")

    if args.execute:
        conn.commit()
        print("\n=== COMMITTED ===")
    else:
        print("\n=== DRY-RUN (no writes) ===")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
