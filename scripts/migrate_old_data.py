#!/Users/marathon/polymarket_weather_trade_bot/.venv/bin/python
"""
Migrate legacy strategy data out of the bot database.

Backs up the DB, exports legacy records to JSON, and optionally deletes them.

Legacy = any strategy NOT IN ('A', 'B', 'C', 'D') in positions/settlements.
For decision_log (no strategy column), legacy records are identified by their
reason prefix: anything with [E], [F], or other non-ABCD strategy tags.

Usage:
    # Dry run (default) -- show what would be deleted
    python scripts/migrate_old_data.py

    # Dry run with custom DB path
    python scripts/migrate_old_data.py --db-path data/bot.db

    # Actually delete legacy records
    python scripts/migrate_old_data.py --execute
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ACTIVE_STRATEGIES = ("A", "B", "C", "D")

# Tables with a direct 'strategy' column
STRATEGY_TABLES = ("positions", "settlements")

# decision_log uses reason prefix like [E], [F] to indicate strategy
# We match records whose reason starts with a bracket+letter NOT in A-D
DECISION_LOG_LEGACY_SQL = """
    SELECT * FROM decision_log
    WHERE reason GLOB '\\[*\\] *'
      AND SUBSTR(reason, 2, 1) NOT IN ('A', 'B', 'C', 'D')
"""

# Simpler approach: match reason starting with [X] where X is not A/B/C/D
# SQLite GLOB uses * and ? wildcards, but for bracket matching we use LIKE + SUBSTR
DECISION_LOG_LEGACY_SQL = """
    SELECT * FROM decision_log
    WHERE reason LIKE '[%] %'
      AND LENGTH(reason) >= 4
      AND SUBSTR(reason, 1, 1) = '['
      AND SUBSTR(reason, 3, 1) = ']'
      AND SUBSTR(reason, 2, 1) NOT IN ('A', 'B', 'C', 'D')
"""

DECISION_LOG_LEGACY_DELETE = """
    DELETE FROM decision_log
    WHERE reason LIKE '[%] %'
      AND LENGTH(reason) >= 4
      AND SUBSTR(reason, 1, 1) = '['
      AND SUBSTR(reason, 3, 1) = ']'
      AND SUBSTR(reason, 2, 1) NOT IN ('A', 'B', 'C', 'D')
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate legacy strategy data out of the bot database.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path("data/bot.db"),
        help="Path to SQLite database (default: data/bot.db)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually delete legacy records. Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--backup-json",
        type=Path,
        default=Path("data/backup_legacy_strategies.json"),
        help="Path for JSON export (default: data/backup_legacy_strategies.json)",
    )
    return parser.parse_args()


def rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    """Convert sqlite3 cursor results to list of dicts."""
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def backup_database(db_path: Path) -> Path:
    """Create a full copy of the database file."""
    backup_path = db_path.with_suffix(".db.bak")
    print(f"  Backing up database to {backup_path} ...")
    shutil.copy2(db_path, backup_path)
    size_mb = backup_path.stat().st_size / (1024 * 1024)
    print(f"  Backup created: {size_mb:.2f} MB")
    return backup_path


def get_table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Get row counts for all relevant tables."""
    counts = {}
    for table in ("positions", "settlements", "decision_log", "orders", "daily_pnl", "edge_history"):
        try:
            cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = cursor.fetchone()[0]
        except sqlite3.OperationalError:
            counts[table] = -1  # table doesn't exist
    return counts


def get_strategy_distribution(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    """Get count of records per strategy value in a table with a strategy column."""
    try:
        cursor = conn.execute(
            f"SELECT COALESCE(strategy, '(NULL)') as strat, COUNT(*) FROM {table} GROUP BY strategy ORDER BY strategy"
        )
        return {row[0]: row[1] for row in cursor.fetchall()}
    except sqlite3.OperationalError:
        return {}


def find_legacy_records(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Find all legacy records across tables."""
    legacy = {}

    # positions and settlements: strategy NOT IN active set
    for table in STRATEGY_TABLES:
        placeholders = ",".join("?" for _ in ACTIVE_STRATEGIES)
        cursor = conn.execute(
            f"SELECT * FROM {table} WHERE strategy NOT IN ({placeholders})",
            ACTIVE_STRATEGIES,
        )
        legacy[table] = rows_to_dicts(cursor)

    # decision_log: match by reason prefix
    cursor = conn.execute(DECISION_LOG_LEGACY_SQL)
    legacy["decision_log"] = rows_to_dicts(cursor)

    return legacy


def print_statistics(conn: sqlite3.Connection, legacy: dict[str, list[dict]], phase: str) -> None:
    """Print table counts and legacy record info."""
    print(f"\n{'=' * 60}")
    print(f"  {phase}")
    print(f"{'=' * 60}")

    # Overall table counts
    counts = get_table_counts(conn)
    print("\n  Table row counts:")
    for table, count in counts.items():
        marker = ""
        if table in legacy and legacy[table]:
            marker = f"  <-- {len(legacy[table])} legacy"
        print(f"    {table:20s}: {count:>6d}{marker}")

    # Strategy distribution for tables that have it
    for table in STRATEGY_TABLES:
        dist = get_strategy_distribution(conn, table)
        if dist:
            print(f"\n  {table} by strategy:")
            for strat, cnt in sorted(dist.items()):
                tag = " (LEGACY)" if strat not in ACTIVE_STRATEGIES else ""
                print(f"    {strat:>8s}: {cnt:>6d}{tag}")

    # Decision log legacy breakdown
    if legacy.get("decision_log"):
        # Group by extracted strategy letter
        strat_counts: dict[str, int] = {}
        for rec in legacy["decision_log"]:
            reason = rec.get("reason", "")
            letter = reason[1] if len(reason) >= 3 else "?"
            strat_counts[letter] = strat_counts.get(letter, 0) + 1
        print(f"\n  decision_log legacy by reason prefix:")
        for letter, cnt in sorted(strat_counts.items()):
            print(f"    [{letter}]: {cnt:>6d}")


def export_to_json(legacy: dict[str, list[dict]], json_path: Path) -> None:
    """Export legacy records to a JSON backup file."""
    total = sum(len(recs) for recs in legacy.values())
    if total == 0:
        print("\n  No legacy records to export.")
        return

    json_path.parent.mkdir(parents=True, exist_ok=True)

    export = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "active_strategies": list(ACTIVE_STRATEGIES),
        "summary": {table: len(recs) for table, recs in legacy.items()},
        "records": legacy,
    }

    with open(json_path, "w") as f:
        json.dump(export, f, indent=2, default=str)

    size_kb = json_path.stat().st_size / 1024
    print(f"\n  Exported {total} legacy records to {json_path} ({size_kb:.1f} KB)")


def delete_legacy_records(conn: sqlite3.Connection) -> dict[str, int]:
    """Delete legacy records and return counts of deleted rows per table."""
    deleted = {}

    for table in STRATEGY_TABLES:
        placeholders = ",".join("?" for _ in ACTIVE_STRATEGIES)
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE strategy NOT IN ({placeholders})",
            ACTIVE_STRATEGIES,
        )
        deleted[table] = cursor.rowcount

    # decision_log: delete by reason prefix
    cursor = conn.execute(DECISION_LOG_LEGACY_DELETE)
    deleted["decision_log"] = cursor.rowcount

    conn.commit()
    return deleted


def main() -> int:
    args = parse_args()

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"\n  Polymarket Weather Bot -- Legacy Strategy Migration")
    print(f"  Mode: {mode}")
    print(f"  Database: {args.db_path}")
    print(f"  Active strategies kept: {', '.join(ACTIVE_STRATEGIES)}")

    # Validate DB exists
    if not args.db_path.exists():
        print(f"\n  ERROR: Database not found at {args.db_path}")
        return 1

    # Step 1: Full database backup
    print(f"\n{'─' * 60}")
    print("  Step 1: Database backup")
    print(f"{'─' * 60}")
    backup_path = backup_database(args.db_path)

    # Connect with row_factory for dict access
    conn = sqlite3.connect(str(args.db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row

    try:
        # Step 2: Find legacy records
        print(f"\n{'─' * 60}")
        print("  Step 2: Scanning for legacy records")
        print(f"{'─' * 60}")
        legacy = find_legacy_records(conn)
        total_legacy = sum(len(recs) for recs in legacy.values())

        # Reconnect without row_factory for statistics (uses raw tuples)
        conn_stats = sqlite3.connect(str(args.db_path), timeout=30.0)
        print_statistics(conn_stats, legacy, "BEFORE migration")
        conn_stats.close()

        if total_legacy == 0:
            print("\n  No legacy records found. Nothing to do.")
            return 0

        # Step 3: Export to JSON
        print(f"\n{'─' * 60}")
        print("  Step 3: Exporting legacy records to JSON")
        print(f"{'─' * 60}")

        # Convert sqlite3.Row objects to plain dicts for JSON serialization
        legacy_plain = {}
        for table, rows in legacy.items():
            legacy_plain[table] = [dict(row) for row in rows]

        export_to_json(legacy_plain, args.backup_json)

        # Step 4: Delete or skip
        print(f"\n{'─' * 60}")
        if args.execute:
            print("  Step 4: DELETING legacy records")
            print(f"{'─' * 60}")
            deleted = delete_legacy_records(conn)
            for table, count in deleted.items():
                print(f"    {table:20s}: {count:>6d} rows deleted")

            # Show after statistics
            conn_after = sqlite3.connect(str(args.db_path), timeout=30.0)
            print_statistics(conn_after, {"positions": [], "settlements": [], "decision_log": []}, "AFTER migration")
            conn_after.close()

            print(f"\n  Migration complete.")
            print(f"  Database backup at: {backup_path}")
            print(f"  JSON backup at: {args.backup_json}")
        else:
            print("  Step 4: DRY RUN -- no records deleted")
            print(f"{'─' * 60}")
            print(f"\n  Would delete {total_legacy} legacy records:")
            for table, recs in legacy_plain.items():
                if recs:
                    print(f"    {table:20s}: {len(recs):>6d} rows")
            print(f"\n  To execute, re-run with --execute flag:")
            print(f"    .venv/bin/python scripts/migrate_old_data.py --execute")
            print(f"\n  JSON backup written to: {args.backup_json}")
            print(f"  DB backup written to: {backup_path}")

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
