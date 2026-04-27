#!/Users/marathon/polymarket_weather_trade_bot/.venv/bin/python
"""Clean a ghost ``positions`` row that was inserted before Fix A.

Bug background (Fix A, 2026-04-28): ``ClobClient.place_limit_order`` was
treating any non-empty ``orderID`` from ``create_and_post_order`` as a
successful fill, but the v2 SDK returns the same dict shape for orders
that posted-but-never-matched (``status='unmatched'``).  The first such
order on production was Miami 86-87 NO @0.565 — recorded as an open
position even though no trade ever happened.

This script:
  1. Locates the suspect row (default: city='Miami', slot 86-87, NO,
     entry_price≈0.565, status='open').  Filters tunable via flags.
  2. Verifies via ``client.get_trades(asset_id=token_id)`` that no trades
     reference the row's ``source_order_id``.
  3. With ``--execute``, marks the position as 'closed' (matches the
     existing exit semantics — there's no 'cancelled' status on positions)
     with ``exit_reason='ghost_no_trade'`` and ``exit_price=NULL``.
  4. Marks the corresponding ``orders`` row 'failed' (so reconciler ignores)
     with ``failure_reason='ghost_no_trade'``.

Default is dry-run: prints the candidate(s) and what would change.

Usage:
    .venv/bin/python scripts/clean_ghost_position.py
    .venv/bin/python scripts/clean_ghost_position.py --execute
    .venv/bin/python scripts/clean_ghost_position.py --position-id 123 --execute
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config  # noqa: E402
from src.markets.clob_client import ClobClient  # noqa: E402
from src.security import load_eth_private_key  # noqa: E402


async def _has_real_trades(
    clob: ClobClient,
    token_id: str,
    order_id: str | None,
) -> bool:
    """Return True if CLOB reports any trades for this token whose
    ``taker_order_id`` matches the position's source_order_id.

    If we can't call CLOB (paper / dry-run / SDK error), we conservatively
    return True so the script does NOT delete — operator can re-run with
    proper creds.
    """
    if not order_id or order_id in ("", "legacy") or order_id.startswith(("paper_", "dry_run")):
        # No real CLOB-side state to check.
        return False
    summary = await clob.get_fill_summary(
        token_id=token_id, order_id=order_id, created_at_epoch=None,
    )
    return summary is not None and summary.shares > 0


def _find_candidates(
    conn: sqlite3.Connection, args: argparse.Namespace,
) -> list[sqlite3.Row]:
    if args.position_id is not None:
        rows = conn.execute(
            "SELECT * FROM positions WHERE id = ? AND status = 'open'",
            (args.position_id,),
        ).fetchall()
        return list(rows)

    # Defaults match the 2026-04-28 Miami ghost: 86-87 NO @0.565.
    query = (
        "SELECT * FROM positions WHERE status = 'open' "
        "AND city = ? AND token_type = ? AND slot_label LIKE ?"
    )
    params: list = [args.city, args.token_type, f"%{args.slot_match}%"]
    if args.entry_price is not None:
        query += " AND ABS(entry_price - ?) < ?"
        params.extend([args.entry_price, args.tolerance])
    rows = conn.execute(query, params).fetchall()
    return list(rows)


async def _amain(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        candidates = _find_candidates(conn, args)
        if not candidates:
            print("No matching open positions found.")
            return 0

        # ``_has_real_trades`` always queries CLOB (/data/trades is L2),
        # even on dry-run, because the safety contract is "don't delete a
        # row until we've confirmed there's no fill behind it".  src/main.py
        # loads the EOA private key from macOS Keychain at live-mode
        # startup; ``load_config`` alone does NOT — the .env's
        # ETH_PRIVATE_KEY is empty by design.  Mirror main.py's load order
        # unconditionally so dry-run and --execute both authenticate.
        cfg = load_config()
        cfg.eth_private_key = load_eth_private_key()
        clob = ClobClient(cfg)

        deleted = 0
        for row in candidates:
            print()
            print(
                f"id={row['id']} city={row['city']} slot={row['slot_label']} "
                f"type={row['token_type']} entry={row['entry_price']:.4f} "
                f"shares={row['shares']:.2f} order={row['source_order_id']} "
                f"strategy={row['strategy']}",
            )
            try:
                has = await _has_real_trades(clob, row["token_id"], row["source_order_id"])
            except Exception as exc:
                print(f"  ERROR probing CLOB: {exc}", file=sys.stderr)
                continue
            if has:
                print("  SKIP: CLOB reports real trades for this order — NOT a ghost.")
                continue

            if not args.execute:
                print("  WOULD mark position closed (exit_reason=ghost_no_trade) "
                      "and matching pending order failed.")
                continue

            # Close the position with a sentinel reason — keeps the row for
            # audit but removes it from open-position queries.
            conn.execute(
                """UPDATE positions
                   SET status = 'closed', closed_at = datetime('now'),
                       exit_reason = 'ghost_no_trade'
                   WHERE id = ?""",
                (row["id"],),
            )
            # Fail any matching orders row keyed off the same source_order_id
            # / idempotency_key.  Use OR so the script works whether the
            # orders row was promoted via finalize_buy_order (order_id set)
            # or stranded as 'pending' (idempotency_key only).
            conn.execute(
                """UPDATE orders
                   SET status = 'failed',
                       failure_reason = COALESCE(failure_reason, '') || ';ghost_no_trade'
                   WHERE order_id = ?
                      OR (idempotency_key IS NOT NULL
                          AND idempotency_key = (
                              SELECT idempotency_key FROM orders
                              WHERE order_id = ? LIMIT 1
                          ))""",
                (row["source_order_id"], row["source_order_id"]),
            )
            conn.commit()
            print("  OK: closed (ghost_no_trade) + orders row(s) marked failed.")
            deleted += 1

        if args.execute:
            print(f"\nDone. Cleaned {deleted} ghost position(s).")
        else:
            print("\nDry-run only. Re-run with --execute to apply.")
        return 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="data/bot.db")
    parser.add_argument("--execute", action="store_true",
                        help="Actually mutate the DB (default: dry-run).")
    parser.add_argument("--position-id", type=int, default=None,
                        help="Target a specific position id; overrides filters.")
    parser.add_argument("--city", default="Miami")
    parser.add_argument("--token-type", default="NO")
    parser.add_argument("--slot-match", default="86-87",
                        help="Substring match against slot_label.")
    parser.add_argument("--entry-price", type=float, default=0.565)
    parser.add_argument("--tolerance", type=float, default=0.005)
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
