#!/Users/marathon/polymarket_weather_trade_bot/.venv/bin/python
"""Add on-chain redemption metadata columns to ``positions`` (idempotent).

Columns added (see ``src/portfolio/store.py::_migrate_columns`` for the
matching block — Store re-applies the same migration at startup):

  condition_id           TEXT          per-market conditionId (bytes32 hex)
  neg_risk               INTEGER       0 = ConditionalTokens, 1 = NegRiskAdapter
  redeem_tx_hash         TEXT          final tx hash, or ``pending:<ts>``
  redeem_status          TEXT          NULL / 'pending' / 'success' / 'failed'
  redeem_attempt_count   INTEGER       failure counter (alert at >= 3)

Run before promoting the redeemer code, so the live DB has the columns
the new INSERTs/UPDATEs reference.

Usage:
    .venv/bin/python scripts/migrate_redeem_columns.py
    .venv/bin/python scripts/migrate_redeem_columns.py --db-path /opt/weather-bot/data/bot.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


COLUMNS: list[tuple[str, str]] = [
    ("condition_id", "TEXT"),
    ("neg_risk", "INTEGER DEFAULT 0"),
    ("redeem_tx_hash", "TEXT"),
    ("redeem_status", "TEXT"),
    ("redeem_attempt_count", "INTEGER DEFAULT 0"),
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
