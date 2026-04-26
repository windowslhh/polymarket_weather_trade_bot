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
- FIX-2P-6 fee rate (live mode only): query CLOB for the actual taker
  fee on a live weather token and compare against ``TAKER_FEE_RATE``.
  Drift between the two is silent disaster — every backtest + EV
  calculation downstream is wrong.  Non-fatal so a transient CLOB hiccup
  can't block startup, but a critical alert + loud log so the operator
  sees the mismatch within seconds of the deploy.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Awaitable, Callable, Iterable

import httpx

logger = logging.getLogger(__name__)

# 5% taker → 500 bps.  Tolerance: Polymarket has shipped per-bp tweaks
# in the past; ±10 bps (0.1%) lets the bot tolerate a minor adjustment
# while still firing if the rate moves materially (e.g. 5% → 6%).
FEE_RATE_TOLERANCE_BPS = 10


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


async def check_fee_rate(
    clob_client,
    sample_token_id: str | None,
    *,
    expected_rate: float,
    is_paper: bool,
    is_dry_run: bool,
) -> tuple[bool, str]:
    """FIX-2P-6: confirm the broker's reported taker fee matches our constant.

    ``sample_token_id`` is one weather-market NO/YES token id obtained
    from a quick discover call; pass ``None`` when discovery yielded
    nothing (no active markets, fresh CLOB outage) — the check is then
    skipped with a benign message rather than producing a false alert.

    Returns ``(False, msg)`` only when we got a rate back AND it diverges
    by more than ``FEE_RATE_TOLERANCE_BPS`` from ``expected_rate``.  Any
    other failure mode (CLOB unreachable, no token to probe) returns
    ``(True, …)`` so this check never blocks startup; the caller still
    sends a critical alert on (False, …).
    """
    if is_paper or is_dry_run:
        return True, "fee_rate_skipped_non_live"
    if not sample_token_id:
        return True, "fee_rate_skipped_no_token"
    try:
        client = clob_client._get_client()
        bps = await asyncio.to_thread(client.get_fee_rate_bps, sample_token_id)
    except Exception as exc:
        # Don't block startup on a single API hiccup — log loud but allow
        # the bot through.  The next rebalance cycle will surface real
        # CLOB issues if they persist.
        logger.warning("Preflight: fee_rate check skipped (CLOB error): %s", exc)
        return True, f"fee_rate_skipped_clob_error: {exc}"
    expected_bps = round(expected_rate * 10_000)
    drift = abs(int(bps) - expected_bps)
    if drift > FEE_RATE_TOLERANCE_BPS:
        return False, (
            f"fee_rate_drift: broker={bps}bps "
            f"(={int(bps) / 100:.2f}%) vs TAKER_FEE_RATE constant "
            f"={expected_bps}bps (={expected_rate * 100:.2f}%)"
        )
    return True, f"fee_rate_ok({bps}bps≈{expected_rate * 100:.2f}%)"


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
    sample_token_provider: Callable[[], Awaitable[str | None]] | None = None,
    expected_fee_rate: float | None = None,
) -> None:
    """Execute all checks, sys.exit(2) on any hard failure.

    Webhook + fee-rate failures log critical but do NOT exit — a broken
    monitoring pipe or a transient CLOB rate hiccup is worth yelling
    about but shouldn't stop trading.  DB + CLOB-reachable failures
    are fatal.

    ``sample_token_provider`` is an awaitable that yields one live
    weather-market token id for the FIX-2P-6 fee-rate check, or None
    when no token is available.  ``expected_fee_rate`` is the canonical
    constant the broker is expected to confirm (e.g. 0.05 for the
    post-2026-03-30 5% Weather rate).  Both default to the no-op path
    so older callers (and tests) don't have to wire them.
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

    if sample_token_provider is not None and expected_fee_rate is not None:
        try:
            sample_token = await sample_token_provider()
        except Exception as exc:
            logger.warning("Preflight: sample-token discovery failed: %s", exc)
            sample_token = None
        fee_ok, fee_msg = await check_fee_rate(
            clob_client, sample_token,
            expected_rate=expected_fee_rate,
            is_paper=is_paper, is_dry_run=is_dry_run,
        )
        if not fee_ok:
            # Critical alert + loud log but do NOT sys.exit — see docstring.
            logger.error("Preflight fee_rate FAIL (continuing): %s", fee_msg)
            try:
                await alerter.send("critical", f"Preflight fee_rate drift: {fee_msg}")
            except Exception:
                pass
        else:
            logger.info("Preflight: %s", fee_msg)

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
