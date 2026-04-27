#!/Users/marathon/polymarket_weather_trade_bot/.venv/bin/python
"""Backfill ``match_price`` and ``fee_paid_usd`` for existing positions.

For every open position whose ``source_order_id`` is a real Polymarket
order id (i.e. NOT 'legacy', not empty, not a 'paper_*' synthetic), call
``client.get_trades(asset_id=token_id)`` and aggregate trades whose
``taker_order_id`` matches.  Update the row with the weighted-average
match price + total taker fee.

Skipped:
  - ``source_order_id IN ('legacy', '', NULL)``    — pre-FIX-03 rows
  - ``source_order_id LIKE 'paper_%'``             — paper-mode synthetics
  - rows that already have a non-null match_price  — re-run safe

Usage:
    .venv/bin/python scripts/backfill_match_price.py             # default
    .venv/bin/python scripts/backfill_match_price.py --dry-run    # show only
    .venv/bin/python scripts/backfill_match_price.py --db-path /opt/weather-bot/data/bot.db

Requires .env with the same ``ETH_PRIVATE_KEY`` / ``POLYMARKET_API_*``
credentials the live bot uses.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

# Allow running as a top-level script from the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config  # noqa: E402
from src.markets.clob_client import ClobClient  # noqa: E402
from src.security import load_eth_private_key  # noqa: E402


def _is_real_order_id(order_id: str | None) -> bool:
    if order_id is None:
        return False
    s = str(order_id).strip()
    if not s:
        return False
    if s == "legacy":
        return False
    if s.startswith("paper_"):
        return False
    if s.startswith("dry_run"):
        return False
    return True


async def _backfill_row(
    clob: ClobClient,
    row: sqlite3.Row,
) -> tuple[float | None, float | None]:
    """Return (match_price, fee_paid_usd) or (None, None) on miss."""
    summary = await clob.get_fill_summary(
        token_id=row["token_id"],
        order_id=row["source_order_id"],
        created_at_epoch=None,  # broad lookback — we don't have order created_at on the row
    )
    if summary is None:
        return None, None
    return summary.match_price, summary.fee_paid_usd


async def _amain(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        # Sanity: columns must exist.  Run scripts/migrate_match_price.py first.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()}
        if "match_price" not in cols or "fee_paid_usd" not in cols:
            print(
                "ERROR: positions.match_price / positions.fee_paid_usd missing — "
                "run scripts/migrate_match_price.py first",
                file=sys.stderr,
            )
            return 1

        rows = conn.execute(
            """SELECT id, token_id, source_order_id, entry_price, shares
               FROM positions
               WHERE status = 'open' AND match_price IS NULL""",
        ).fetchall()
        print(f"Found {len(rows)} open position(s) without match_price")

        eligible = [r for r in rows if _is_real_order_id(r["source_order_id"])]
        skipped_synth = len(rows) - len(eligible)
        if skipped_synth:
            print(f"  (skipping {skipped_synth} legacy / paper / dry-run rows)")

        # Build the CLOB client BEFORE branching on dry-run so the dry-run
        # path can preview real match_price / fee values.  ``get_fill_summary``
        # always queries CLOB (it hits /data/trades, an L2 endpoint) so we
        # need an authenticated client either way.  src/main.py loads the EOA
        # private key from macOS Keychain at live-mode startup; ``load_config``
        # alone does NOT — the .env's ETH_PRIVATE_KEY is empty by design
        # (Keychain is the source of truth).  Mirror main.py's load order
        # unconditionally here; the script refuses to run in paper / dry_run
        # config below regardless.
        cfg = load_config()
        if cfg.dry_run or cfg.paper:
            print(
                "ERROR: cfg.dry_run / cfg.paper is true — backfill needs live "
                "CLOB creds.  Run with the live .env.",
                file=sys.stderr,
            )
            return 1
        cfg.eth_private_key = load_eth_private_key()
        clob = ClobClient(cfg)

        updated = 0
        misses = 0
        errors = 0
        for r in eligible:
            try:
                match_price, fee = await _backfill_row(clob, r)
            except Exception as exc:
                errors += 1
                print(
                    f"  ERROR id={r['id']} token={r['token_id'][:12]}... -> {exc}",
                    file=sys.stderr,
                )
                continue
            if match_price is None:
                misses += 1
                print(
                    f"  MISS id={r['id']} order={r['source_order_id'][:14]}... "
                    f"limit={r['entry_price']:.4f} (no matching trades on CLOB)",
                )
                continue
            slip = match_price - r["entry_price"]
            if args.dry_run:
                print(
                    f"  WOULD id={r['id']} token={r['token_id'][:12]}... "
                    f"limit={r['entry_price']:.4f} match={match_price:.4f} "
                    f"fee=${fee:.4f} slip={slip:+.4f}",
                )
            else:
                conn.execute(
                    "UPDATE positions SET match_price = ?, fee_paid_usd = ? WHERE id = ?",
                    (match_price, fee, r["id"]),
                )
                conn.commit()
                updated += 1
                print(
                    f"  OK id={r['id']} token={r['token_id'][:12]}... "
                    f"limit={r['entry_price']:.4f} match={match_price:.4f} "
                    f"fee=${fee:.4f} slip={slip:+.4f}",
                )

        if args.dry_run:
            print(
                f"Dry-run: {len(eligible)} eligible — would-update={len(eligible) - misses - errors} "
                f"miss={misses} error={errors} skipped={skipped_synth}",
            )
        else:
            print(
                f"Done. Updated {updated} row(s), missed {misses}, "
                f"errors {errors}, skipped {skipped_synth}.",
            )
        return 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="data/bot.db")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show eligible rows without hitting CLOB or writing.",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
