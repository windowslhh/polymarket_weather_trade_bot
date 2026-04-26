"""G-1' Phase 1 (2026-04-26): per-cycle caps on the periodic reconciler.

Two safeguards:
  - Per-probe asyncio.timeout(15) inside probe_order_status so a stuck
    CLOB request can't hold rebalancer.cycle_lock indefinitely.
  - RECONCILER_BATCH_CAP=20 + outer asyncio.timeout(120) on
    reconcile_pending_orders so a 30-row backlog or pathological run
    bounded above.

Pre-fix the reconciler ran every pending row inline with no upper
bound; combined with cycle_lock acquisition, a CLOB outage that
filled `pending` with 50+ rows could wedge rebalance + position_check
for the duration of the recovery probe.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.alerts import Alerter
from src.markets.clob_client import ClobClient, ProbeResult
from src.portfolio.store import Store
from src.recovery import reconciler as reconciler_mod
from src.recovery.reconciler import (
    ClobOrderStatus,
    RECONCILER_BATCH_CAP,
    RECONCILER_TOTAL_TIMEOUT_S,
    reconcile_pending_orders,
)


def _alerter() -> Alerter:
    a = Alerter(webhook_url="")
    a.send = AsyncMock()  # type: ignore[method-assign]
    return a


async def _mk_store_with_n_pending(n: int) -> Store:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    for i in range(n):
        await store.insert_pending_order(
            idempotency_key=f"key_{i:03}",
            event_id=f"ev_{i}", token_id=f"tok_{i}",
            side="BUY", price=0.5, size_usd=5.0,
        )
    return store


# ──────────────────────────────────────────────────────────────────────
# Reconciler batch cap
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconciler_processes_at_most_RECONCILER_BATCH_CAP_per_cycle():
    """G-1' Phase 1: 21 pending rows → first 20 processed, 21st deferred.
    Pre-fix all 21 would run inline, holding cycle_lock the whole time."""
    store = await _mk_store_with_n_pending(21)
    alerter = _alerter()

    probe_calls = []

    async def _probe(row):
        probe_calls.append(row["idempotency_key"])
        return ClobOrderStatus(state="unknown")  # marks failed; no exit

    await reconcile_pending_orders(
        store, alerter,
        query_clob_order=_probe,
        is_paper=False,
        exit_on_mismatch=False,
    )

    assert len(probe_calls) == RECONCILER_BATCH_CAP, (
        f"G-1': expected exactly {RECONCILER_BATCH_CAP} probes, got {len(probe_calls)}"
    )
    # First 20 processed; the 21st is still pending in DB
    async with store.db.execute(
        "SELECT COUNT(*) FROM orders WHERE status='pending'"
    ) as cur:
        (still_pending,) = await cur.fetchone()
    assert still_pending == 1, (
        f"G-1': expected 1 row deferred to next cycle, got {still_pending} pending"
    )

    # Backlog warning fired
    warning_calls = [
        c for c in alerter.send.call_args_list if c.args[0] == "warning"
    ]
    assert any("backlog" in c.args[1] for c in warning_calls), (
        "G-1': must alert ops about the backlog"
    )

    await store.close()


@pytest.mark.asyncio
async def test_reconciler_no_cap_message_when_under_limit():
    """Sanity: a small batch (≤ cap) produces no backlog warning."""
    store = await _mk_store_with_n_pending(3)
    alerter = _alerter()

    async def _probe(row):
        return ClobOrderStatus(state="unknown")

    await reconcile_pending_orders(
        store, alerter,
        query_clob_order=_probe,
        is_paper=False,
        exit_on_mismatch=False,
    )

    warning_calls = [
        c for c in alerter.send.call_args_list if c.args[0] == "warning"
    ]
    assert not any("backlog" in c.args[1] for c in warning_calls)
    await store.close()


# ──────────────────────────────────────────────────────────────────────
# Reconciler total timeout
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconciler_top_level_timeout_leaves_remaining_pending(monkeypatch):
    """G-1' Phase 1: if total elapsed time exceeds RECONCILER_TOTAL_TIMEOUT_S,
    the call returns gracefully (warning alert) and remaining rows stay
    pending for the next cycle."""
    store = await _mk_store_with_n_pending(5)
    alerter = _alerter()

    # Shorten the total timeout so the test runs fast.
    monkeypatch.setattr(reconciler_mod, "RECONCILER_TOTAL_TIMEOUT_S", 0.5)
    monkeypatch.setattr(reconciler_mod, "RECONCILER_BATCH_CAP", 100)

    async def _slow_probe(row):
        # Each probe takes 200ms; with 5 rows that's 1000ms total
        # (> 500ms timeout), so timeout fires partway through.
        await asyncio.sleep(0.2)
        return ClobOrderStatus(state="unknown")

    await reconcile_pending_orders(
        store, alerter,
        query_clob_order=_slow_probe,
        is_paper=False,
        exit_on_mismatch=False,
    )

    # Some pending rows must still be there — timeout aborted partway
    async with store.db.execute(
        "SELECT COUNT(*) FROM orders WHERE status='pending'"
    ) as cur:
        (still_pending,) = await cur.fetchone()
    assert still_pending > 0, "G-1': total-timeout must leave unfinished rows pending"

    # Warning alert fired
    warning_calls = [
        c for c in alerter.send.call_args_list if c.args[0] == "warning"
    ]
    assert any("timeout" in c.args[1].lower() for c in warning_calls)
    await store.close()


# ──────────────────────────────────────────────────────────────────────
# probe_order_status per-call asyncio.timeout(15)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_order_status_returns_unreachable_on_timeout(monkeypatch):
    """Per-probe budget 15s.  A stuck CLOB call must surface as
    state='unreachable' so the reconciler skips this row and moves on."""
    cfg = SimpleNamespace(
        dry_run=False, paper=False,
        polymarket_api_key="k", polymarket_secret="s", polymarket_passphrase="p",
        eth_private_key="0xabc",
    )
    client = ClobClient(cfg)  # type: ignore[arg-type]
    client._client = MagicMock()

    # Make the SDK call sleep longer than the (shortened-to-50ms below)
    # asyncio.timeout.  Note: time.sleep runs in a to_thread executor,
    # which doesn't honour asyncio cancellation, so we keep the sleep
    # short (200ms) — the asyncio side raises TimeoutError at 50ms,
    # the thread finishes shortly after, and the test stays fast.
    def _block_forever(*a, **kw):
        import time
        time.sleep(0.2)

    client._client.get_trades = MagicMock(side_effect=_block_forever)
    client._client.get_orders = MagicMock(side_effect=_block_forever)

    # Patch asyncio.timeout to a tiny budget so the test doesn't actually
    # sleep 15 seconds.
    import asyncio as _asyncio
    real_timeout = _asyncio.timeout

    def _short_timeout(seconds):
        return real_timeout(0.05)

    monkeypatch.setattr(
        "src.markets.clob_client.asyncio.timeout", _short_timeout,
    )

    result = await client.probe_order_status(
        token_id="tok-1", side="BUY", price=0.5, size_shares=10.0,
    )
    assert result.state == "unreachable"
    assert "timed out" in result.message.lower() or "timeout" in result.message.lower()
