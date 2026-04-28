"""Rollback the 2026-04-28 false-positive ``already_redeemed`` settlements.

Background: the initial auto-redeem rollout used
``ConditionalTokens.balanceOf(funder, positionId)`` which returns 0 for
negRisk markets (positionId encoding mismatch).  As a result, Miami
88-89 and Chicago 66-67 were marked ``settled`` with redeem_tx_hash =
``already_redeemed`` even though USDC was still recoverable on chain.

This script reverses those two rows so the next redeem cycle can do the
actual on-chain call.  It is **idempotent** and **defaults to dry-run**:

  * Without flags → prints the SQL it would run, the position rows it
    would touch, the settlements rows it would delete, and the daily_pnl
    delta — then exits 0.
  * ``--execute`` → runs the same statements inside a single transaction
    and prints before/after state.

Targets are limited to rows where:
    status = 'settled' AND redeem_tx_hash = 'already_redeemed'
    AND neg_risk = 1

so a re-run is a no-op (after rollback, status='open' and the WHERE
clause matches nothing).

Usage:
    .venv/bin/python scripts/rollback_false_settled.py            # dry-run
    .venv/bin/python scripts/rollback_false_settled.py --execute  # write
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DB_PATH = _REPO / "data" / "bot.db"


def _select_targets(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        """SELECT id, event_id, status, city, slot_label, shares, entry_price,
                  realized_pnl, redeem_tx_hash, redeem_status, closed_at,
                  strategy
             FROM positions
            WHERE status = 'settled'
              AND redeem_tx_hash = 'already_redeemed'
              AND neg_risk = 1
            ORDER BY id ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def _summarise_targets(targets: list[dict]) -> tuple[float, list[tuple[str, str]]]:
    """Compute total realized P&L to roll back + (event_id, strategy) pairs."""
    total_pnl = sum(float(t["realized_pnl"] or 0) for t in targets)
    pairs = [(t["event_id"], t["strategy"]) for t in targets]
    return total_pnl, pairs


def _print_table(rows: list[dict], cols: list[tuple[str, int]]) -> None:
    header = "  ".join(f"{c:<{w}}" for c, w in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        line = "  ".join(
            f"{str(r.get(c, ''))[:w]:<{w}}" for c, w in cols
        )
        print(line)


def _do_rollback(db: sqlite3.Connection, targets: list[dict],
                 total_pnl: float, pairs: list[tuple[str, str]]) -> None:
    """Apply the rollback inside a single transaction."""
    db.execute("BEGIN")
    try:
        for t in targets:
            db.execute(
                """UPDATE positions
                      SET status = 'open',
                          closed_at = NULL,
                          exit_price = NULL,
                          realized_pnl = NULL,
                          redeem_tx_hash = NULL,
                          redeem_status = NULL,
                          redeem_attempt_count = 0
                    WHERE id = ?""",
                (t["id"],),
            )
        for event_id, strategy in pairs:
            db.execute(
                "DELETE FROM settlements WHERE event_id = ? AND strategy = ?",
                (event_id, strategy),
            )
        # Decrement realized_pnl on every daily_pnl row touched by these
        # settlements.  We grouped them by date implicitly (closed_at):
        # the original increment happened on the date the false-redeem
        # ran, which for both rows is 2026-04-28 (per closed_at).
        if targets:
            # Subtract per-date so a future run (different day) decrements
            # the right bucket; group the in-memory list by date.
            by_date: dict[str, float] = {}
            for t in targets:
                d = (t.get("closed_at") or "")[:10]
                by_date[d] = by_date.get(d, 0.0) + float(t["realized_pnl"] or 0)
            for date_str, pnl in by_date.items():
                db.execute(
                    """UPDATE daily_pnl
                          SET realized_pnl = realized_pnl - ?,
                              updated_at = datetime('now')
                        WHERE date = ?""",
                    (pnl, date_str),
                )
        db.commit()
    except Exception:
        db.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="Apply the rollback (default is dry-run; only prints SQL).",
    )
    args = parser.parse_args()

    if not _DB_PATH.exists():
        print(f"ERROR: DB not found at {_DB_PATH}", file=sys.stderr)
        return 1

    db = sqlite3.connect(str(_DB_PATH), timeout=30.0)
    db.row_factory = sqlite3.Row

    targets = _select_targets(db)
    total_pnl, pairs = _summarise_targets(targets)

    print(f"Mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print(f"DB:   {_DB_PATH}")
    print(f"Found {len(targets)} false-settled position(s).")
    print()

    if not targets:
        print("Nothing to roll back.  Exiting.")
        db.close()
        return 0

    print("Positions to be reverted to status='open':")
    _print_table(targets, [
        ("id", 4), ("event_id", 8), ("city", 14), ("strategy", 4),
        ("shares", 10), ("entry_price", 6), ("realized_pnl", 10),
        ("closed_at", 22), ("redeem_tx_hash", 18),
    ])
    print()

    print("Settlements rows to be DELETED:")
    for event_id, strategy in pairs:
        print(f"  - event_id={event_id} strategy={strategy}")
    print()

    print("daily_pnl deltas (by closed_at date):")
    by_date: dict[str, float] = {}
    for t in targets:
        d = (t.get("closed_at") or "")[:10]
        by_date[d] = by_date.get(d, 0.0) + float(t["realized_pnl"] or 0)
    for date_str, pnl in by_date.items():
        cur = db.execute(
            "SELECT realized_pnl FROM daily_pnl WHERE date = ?", (date_str,),
        ).fetchone()
        before = float(cur["realized_pnl"]) if cur else 0.0
        print(
            f"  - date={date_str}: realized_pnl {before:.4f} → {before - pnl:.4f} "
            f"(delta -{pnl:.4f})"
        )
    print()
    print(f"TOTAL realized_pnl to remove: ${total_pnl:.4f}")
    print()

    if not args.execute:
        print("DRY-RUN: no changes written.  Re-run with --execute to apply.")
        db.close()
        return 0

    print("Applying rollback...")
    _do_rollback(db, targets, total_pnl, pairs)
    print("Rollback complete.")
    print()

    # Verify post-state.
    leftover = _select_targets(db)
    print(f"Remaining false-settled rows: {len(leftover)} (expect 0).")
    for t in targets:
        cur = db.execute(
            "SELECT id, status, redeem_tx_hash, realized_pnl, closed_at "
            "FROM positions WHERE id = ?",
            (t["id"],),
        ).fetchone()
        if cur:
            print(f"  - id={cur['id']} status={cur['status']} "
                  f"redeem_tx_hash={cur['redeem_tx_hash']} "
                  f"realized_pnl={cur['realized_pnl']} "
                  f"closed_at={cur['closed_at']}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
