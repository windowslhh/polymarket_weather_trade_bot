#!/Users/marathon/polymarket_weather_trade_bot/.venv/bin/python
"""Add ``match_price`` + ``fee_paid_usd`` columns to ``positions`` (idempotent).

These columns capture the actual per-share fill price (USDC_paid /
shares_received from the trade response) and the taker fee paid, so the
dashboard's "Entry" column reflects effective cost basis instead of the
limit price the bot submitted.

The Store class also runs this migration at startup via ``_migrate_columns``;
this standalone script exists so the merge step can apply it to the live
prod DB without restarting the bot.

Usage:
    .venv/bin/python scripts/migrate_match_price.py            # default data/bot.db
    .venv/bin/python scripts/migrate_match_price.py --db-path /opt/weather-bot/data/bot.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


COLUMNS = [
    ("match_price", "REAL"),
    ("fee_paid_usd", "REAL"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path", default="data/bot.db",
        help="Path to bot.db (default: data/bot.db)",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        cur = conn.execute("PRAGMA table_info(positions)")
        existing = {row[1] for row in cur.fetchall()}
        added: list[str] = []
        for column, sql_type in COLUMNS:
            if column in existing:
                print(f"SKIP: positions.{column} already present")
                continue
            conn.execute(
                f"ALTER TABLE positions ADD COLUMN {column} {sql_type}",
            )
            added.append(column)
            print(f"ADDED: positions.{column} ({sql_type})")
        conn.commit()
        print(f"Done. Added {len(added)} column(s) to {db_path}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
