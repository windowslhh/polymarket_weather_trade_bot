#!/Users/marathon/polymarket_weather_trade_bot/.venv/bin/python
"""Backfill ``condition_id`` + ``neg_risk`` on existing open positions.

Reads every ``positions`` row WHERE status='open' AND condition_id IS NULL,
looks up the per-position conditionId + negativeRisk via Polymarket's
data-api (``https://data-api.polymarket.com/positions?user=<funder>``),
and updates the row.

The redeemer module needs both columns; the migration adds the columns
empty, this script populates them for the rows that pre-date the redeemer.

Defaults to dry-run — prints what would change but commits nothing.  Pass
``--execute`` to actually persist.

Usage:
    .venv/bin/python scripts/backfill_redeem_metadata.py
    .venv/bin/python scripts/backfill_redeem_metadata.py --execute
    .venv/bin/python scripts/backfill_redeem_metadata.py --db-path /opt/weather-bot/data/bot.db --execute
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path


DATA_API_URL = "https://data-api.polymarket.com/positions"


def _fetch_funder_positions(funder: str) -> list[dict]:
    """Hit data-api for every position owned by the funder Safe.

    Strips proxy env vars before the call so a misconfigured local
    HTTP_PROXY can't 502-out the lookup — same pattern the trade-bot
    monitor uses (see ``polymarket_trade_bot/src/monitor.py``).
    """
    import requests

    proxy_keys = [
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ]
    saved = {k: os.environ.pop(k, None) for k in proxy_keys}
    try:
        session = requests.Session()
        session.trust_env = False
        r = session.get(
            DATA_API_URL,
            params={"user": funder.lower()},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def _load_funder() -> str:
    """Read FUNDER_ADDRESS from .env via os.environ.

    Mirrors src/config.py::_env_or_none — empty / placeholder ('0x', '0x0')
    is rejected so a half-edited .env doesn't backfill against a stranger.
    """
    from dotenv import load_dotenv

    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")
    raw = os.environ.get("FUNDER_ADDRESS", "").strip()
    if not raw or raw in ("0x", "0x0"):
        print("ERROR: FUNDER_ADDRESS missing or placeholder in .env", file=sys.stderr)
        sys.exit(1)
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path", default="data/bot.db",
        help="Path to bot.db (default: data/bot.db)",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Persist changes (default: dry-run / report only)",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 1

    funder = _load_funder()
    print(f"Funder: {funder}")

    print("Fetching on-chain positions from data-api...")
    try:
        api_positions = _fetch_funder_positions(funder)
    except Exception as exc:
        print(f"ERROR: data-api fetch failed: {exc}", file=sys.stderr)
        return 1
    print(f"data-api returned {len(api_positions)} positions for funder")

    # asset (token_id) → (conditionId, negativeRisk)
    by_token: dict[str, tuple[str, bool]] = {}
    for p in api_positions:
        asset = str(p.get("asset", ""))
        cid = str(p.get("conditionId", ""))
        neg = bool(p.get("negativeRisk", False))
        if asset and cid:
            by_token[asset] = (cid, neg)

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, event_id, token_id, slot_label, city, strategy "
            "FROM positions WHERE status = 'open' AND (condition_id IS NULL OR condition_id = '')"
        ).fetchall()
        print(f"Found {len(rows)} open positions needing backfill")

        planned: list[tuple[int, str, int, str]] = []  # (id, cid, neg, label)
        missing: list[dict] = []
        for row in rows:
            tid = row["token_id"]
            entry = by_token.get(tid)
            if entry is None:
                missing.append(dict(row))
                continue
            cid, neg = entry
            planned.append((row["id"], cid, 1 if neg else 0, row["slot_label"]))

        for pid, cid, neg, label in planned:
            print(
                f"  pos id={pid} [{label[:30]:30}] → condition_id={cid[:20]}... "
                f"neg_risk={neg}"
            )
        for row in missing:
            print(
                f"  pos id={row['id']} [{row['slot_label'][:30]:30}] — NO data-api match "
                f"(token_id={row['token_id'][:16]}...)"
            )

        if not args.execute:
            print(
                f"\nDRY RUN: would update {len(planned)} row(s); "
                f"{len(missing)} row(s) lack a data-api match.\n"
                "Re-run with --execute to persist."
            )
            return 0

        for pid, cid, neg, _label in planned:
            conn.execute(
                "UPDATE positions SET condition_id = ?, neg_risk = ? WHERE id = ?",
                (cid, neg, pid),
            )
        conn.commit()
        print(f"\nUpdated {len(planned)} row(s); {len(missing)} unmatched.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
