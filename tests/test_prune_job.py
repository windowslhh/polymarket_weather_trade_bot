"""FIX-13: nightly prune + WAL checkpoint.

Verifies _prune_table deletes rows older than the configured window and
leaves fresh rows intact.  Also spot-checks that the prune_and_checkpoint
job is registered in setup_scheduler.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.portfolio.store import Store
from src.scheduler.jobs import _prune_table, setup_scheduler


async def _mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    return store


@pytest.mark.asyncio
async def test_prune_edge_history_over_30d():
    """Rows older than 30 days are deleted, newer rows survive."""
    store = await _mk_store()
    now = datetime.now(timezone.utc)
    # Seed: one 60-day-old, one 29-day-old, one fresh.
    rows = [
        ("Chicago", "2026-02-01", "70-72°F",
         (now - timedelta(days=60)).isoformat()),
        ("Chicago", "2026-03-26", "70-72°F",
         (now - timedelta(days=29)).isoformat()),
        ("Chicago", "2026-04-25", "70-72°F",
         now.isoformat()),
    ]
    for city, mkt_date, label, cycle_at in rows:
        await store.db.execute(
            """INSERT INTO edge_history (cycle_at, city, market_date, slot_label,
                   forecast_high_f, price_yes, price_no, win_prob, ev, distance_f, trend_state)
               VALUES (?, ?, ?, ?, 75, 0.3, 0.7, 0.6, 0.04, 2, 'STABLE')""",
            (cycle_at, city, mkt_date, label),
        )
    await store.db.commit()

    deleted = await _prune_table(store, "edge_history", "cycle_at", "-30 days")
    assert deleted == 1

    async with store.db.execute("SELECT COUNT(*) FROM edge_history") as cur:
        (remaining,) = await cur.fetchone()
    assert remaining == 2
    await store.close()


@pytest.mark.asyncio
async def test_prune_decision_log_over_90d():
    store = await _mk_store()
    now = datetime.now(timezone.utc)
    for days_ago in (100, 50, 0):
        await store.db.execute(
            """INSERT INTO decision_log (cycle_at, city, event_id, signal_type, slot_label,
                   forecast_high_f, daily_max_f, trend_state, win_prob, expected_value,
                   price, size_usd, action)
               VALUES (?, 'NYC', 'e', 'NO', '80°F', 75, 72, 'STABLE', 0.6, 0.04,
                   0.4, 5, 'BUY')""",
            ((now - timedelta(days=days_ago)).isoformat(),),
        )
    await store.db.commit()

    deleted = await _prune_table(store, "decision_log", "cycle_at", "-90 days")
    assert deleted == 1
    await store.close()


def test_prune_job_registered_in_scheduler():
    """setup_scheduler must add a job with id='prune_job' so ops can find it."""
    cfg = SimpleNamespace(
        scheduling=SimpleNamespace(rebalance_interval_minutes=60),
    )
    scheduler = setup_scheduler(cfg, MagicMock(), alerter=None)
    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "prune_job" in job_ids
