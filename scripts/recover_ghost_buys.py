#!/usr/bin/env python3
"""GF-2: one-shot recovery for ghost BUY fills.

Recovers DB state for BUY orders that returned ``status=delayed`` from
Polymarket CLOB, were marked 'failed' by the bot wrapper, but actually
matched on chain via the server's async match window.

Usage:
    .venv/bin/python scripts/recover_ghost_buys.py            # all candidates
    .venv/bin/python scripts/recover_ghost_buys.py 521 522    # specific order ids
    .venv/bin/python scripts/recover_ghost_buys.py --dry-run  # show what would happen

The script is idempotent: re-running won't double-insert positions because
``Store.recover_ghost_buy_fill`` short-circuits when a position already
exists for the given ``source_order_id``.

Always back up the DB first:
    cp data/bot.db data/bot.db.bak.$(date +%Y%m%d-%H%M%S)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import httpx

# Allow running from repo root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.markets.clob_client import ClobClient  # noqa: E402
from src.portfolio.store import Store  # noqa: E402
from src.recovery.ghost_fills import recover_one_ghost_fill  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _run(target_ids: list[int] | None, dry_run: bool) -> int:
    config = load_config()
    if config.paper or config.dry_run:
        print("ERROR: bot is configured for paper / dry-run mode; "
              "ghost recovery requires live CLOB access.", file=sys.stderr)
        return 2

    store = Store(config.db_path)
    await store.initialize()

    candidates = await store.get_failed_delayed_buy_orders()
    if target_ids:
        candidates = [r for r in candidates if int(r["id"]) in target_ids]
        missing = set(target_ids) - {int(r["id"]) for r in candidates}
        if missing:
            print(
                f"WARNING: requested order ids not in failed-delayed pool: "
                f"{sorted(missing)} (already recovered? wrong status?)",
                file=sys.stderr,
            )

    if not candidates:
        print("No ghost-fill candidates found.")
        return 0

    print(f"Found {len(candidates)} candidate(s):")
    for row in candidates:
        print(
            f"  id={row['id']} order_id={row['order_id'][:14]} "
            f"token={row['token_id'][:12]} price={row['price']:.4f} "
            f"size=${row['size_usd']:.2f} created={row['created_at']}"
        )

    if dry_run:
        print("\n--dry-run: not invoking CLOB; not writing.")
        return 0

    clob = ClobClient(config)
    recovered = 0
    skipped = 0
    async with httpx.AsyncClient(timeout=15) as http:
        for row in candidates:
            print(f"\nProbing order_id={row['order_id'][:14]} ...")
            try:
                ok, msg = await recover_one_ghost_fill(
                    store=store,
                    clob_client=clob,
                    failed_order_row=row,
                    http_client=http,
                )
            except Exception as exc:
                print(f"  ERROR: {exc}", file=sys.stderr)
                continue
            if ok:
                print(f"  RECOVERED: {msg}")
                recovered += 1
            else:
                print(f"  skipped: {msg}")
                skipped += 1

    print(f"\nDone: {recovered} recovered, {skipped} skipped, "
          f"{len(candidates) - recovered - skipped} errored.")
    return 0


def _parse_args() -> tuple[list[int] | None, bool]:
    parser = argparse.ArgumentParser(
        description="Recover ghost BUY fills from CLOB on-chain matches.",
    )
    parser.add_argument(
        "order_ids", nargs="*", type=int,
        help="Specific orders.id values to recover (omit for all candidates)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List candidates without probing CLOB or writing positions",
    )
    args = parser.parse_args()
    return (args.order_ids or None), args.dry_run


def main() -> None:
    target_ids, dry_run = _parse_args()
    sys.exit(asyncio.run(_run(target_ids, dry_run)))


if __name__ == "__main__":
    main()
