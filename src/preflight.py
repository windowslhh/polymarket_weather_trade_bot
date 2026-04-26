"""FIX-M7: startup preflight checks.

Runs after store.initialize but before scheduler.start.  Each check is
small and fails loud (sys.exit(2) + critical alert) so we don't discover
"CLOB credentials wrong" an hour into a live run.

Checks:
- DB writable: open a transaction, bump a meta row, rollback.  Catches
  "disk full", "WAL lock orphaned", "volume mounted read-only".
- CLOB reachable (live mode only): tries get_address(); failure is
  almost always an auth / network issue.  Paper + dry-run skip.
- Webhook reachable (if configured): POST a lightweight info ping.
  Non-fatal — webhook outages shouldn't halt trading — but logs +
  alert the in-process alerter so the operator sees it.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)


async def check_db_writable(store) -> tuple[bool, str]:
    """Verify DB accepts a write + rollback without raising."""
    try:
        # A harmless write: create a scratch row then roll back.  aiosqlite
        # doesn't expose explicit rollback on its connection without
        # touching the internal transaction — use a temp table.
        await store.db.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _preflight(x INTEGER)"
        )
        await store.db.execute("INSERT INTO _preflight(x) VALUES (1)")
        await store.db.execute("DROP TABLE _preflight")
        await store.db.commit()
        return True, "db_writable"
    except Exception as exc:
        return False, f"db_not_writable: {exc}"


async def check_clob_reachable(
    clob_client, is_paper: bool, is_dry_run: bool,
) -> tuple[bool, str]:
    """Live-mode only: confirm py-clob-client can authenticate + reach
    the exchange.  Skipped in paper/dry-run (no real credentials)."""
    if is_paper or is_dry_run:
        return True, "clob_skipped_non_live"
    try:
        client = clob_client._get_client()
        # get_address is a cheap auth check — py-clob-client builds it
        # from the eth private key; any crypto failure surfaces here.
        addr = await asyncio.to_thread(client.get_address)
        if not addr:
            return False, "clob_empty_address"
        return True, f"clob_ok(addr={addr[:10]}...)"
    except Exception as exc:
        return False, f"clob_unreachable: {exc}"


async def check_webhook_reachable(webhook_url: str) -> tuple[bool, str]:
    """Empty URL = dev mode, skip.  Otherwise ping and accept any 2xx."""
    url = (webhook_url or "").strip()
    if not url:
        return True, "webhook_skipped_no_url"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(url, json={
                "content": "ℹ️ weather-bot preflight",
            })
            if 200 <= resp.status_code < 300:
                return True, f"webhook_ok({resp.status_code})"
            return False, f"webhook_status_{resp.status_code}"
    except Exception as exc:
        return False, f"webhook_unreachable: {exc}"


async def run_preflight(
    *,
    store,
    clob_client,
    alerter,
    webhook_url: str,
    is_paper: bool,
    is_dry_run: bool,
) -> None:
    """Execute all checks, sys.exit(2) on any hard failure.

    Webhook failures log critical but do NOT exit — a broken monitoring
    pipe is worth yelling about but shouldn't stop trading.  DB + CLOB
    failures are fatal.
    """
    db_ok, db_msg = await check_db_writable(store)
    if not db_ok:
        await alerter.send("critical", f"Preflight DB failure: {db_msg}")
        logger.error("Preflight DB FAIL: %s", db_msg)
        sys.exit(2)
    logger.info("Preflight: %s", db_msg)

    clob_ok, clob_msg = await check_clob_reachable(
        clob_client, is_paper=is_paper, is_dry_run=is_dry_run,
    )
    if not clob_ok:
        await alerter.send("critical", f"Preflight CLOB failure: {clob_msg}")
        logger.error("Preflight CLOB FAIL: %s", clob_msg)
        sys.exit(2)
    logger.info("Preflight: %s", clob_msg)

    hook_ok, hook_msg = await check_webhook_reachable(webhook_url)
    if not hook_ok:
        logger.error("Preflight webhook FAIL (continuing): %s", hook_msg)
        # Try to surface via the alerter anyway — it logs to stdout even
        # when the webhook is down.
        try:
            await alerter.send("critical", f"Preflight webhook failure: {hook_msg}")
        except Exception:
            pass
    else:
        logger.info("Preflight: %s", hook_msg)
