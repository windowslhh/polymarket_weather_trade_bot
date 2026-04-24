"""FIX-08: exit cooldowns must survive a bot restart.

Previously ``rebalancer._recent_exits`` was an in-memory dict only.  A
crash inside the BUY→EXIT→BUY cooldown window would reset it on the
next startup, defeating the guard that was supposed to prevent churn.

Now writes go dual to RAM + DB, and startup loads active cooldowns
from DB into the cache via ``rebalancer.load_persistent_state()``.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker


async def _mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    store = Store(tmp)
    await store.initialize()
    return store


@pytest.mark.asyncio
async def test_record_and_readback():
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    t = datetime.now(timezone.utc)
    await tracker.record_exit_cooldown("tok_A", t, cooldown_hours=4.0)

    active = await tracker.load_active_exit_cooldowns()
    assert "tok_A" in active
    # Times round-trip to within a millisecond.
    assert abs((active["tok_A"] - t).total_seconds()) < 0.01
    await store.close()


@pytest.mark.asyncio
async def test_upsert_replaces_earlier_exit():
    """A second EXIT on the same token updates the row (later wins)."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    t1 = datetime.now(timezone.utc) - timedelta(minutes=30)
    t2 = datetime.now(timezone.utc)
    await tracker.record_exit_cooldown("tok_A", t1, cooldown_hours=4.0)
    await tracker.record_exit_cooldown("tok_A", t2, cooldown_hours=4.0)

    active = await tracker.load_active_exit_cooldowns()
    assert abs((active["tok_A"] - t2).total_seconds()) < 0.01
    await store.close()


@pytest.mark.asyncio
async def test_expired_rows_are_dropped():
    """Rows whose window has fully elapsed don't show up and are deleted."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    old = datetime.now(timezone.utc) - timedelta(hours=10)  # way past 4h window
    await tracker.record_exit_cooldown("tok_old", old, cooldown_hours=4.0)

    active = await tracker.load_active_exit_cooldowns()
    assert "tok_old" not in active

    # And the row is physically gone — no silent accumulation.
    async with store.db.execute(
        "SELECT COUNT(*) FROM exit_cooldowns WHERE token_id='tok_old'"
    ) as cur:
        (n,) = await cur.fetchone()
    assert n == 0
    await store.close()


@pytest.mark.asyncio
async def test_active_rows_survive_restart_simulation():
    """Write, close, reopen, reload — cooldown is still present."""
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    s1 = Store(tmp)
    await s1.initialize()
    t1 = PortfolioTracker(s1)
    now = datetime.now(timezone.utc)
    await t1.record_exit_cooldown("tok_survive", now, cooldown_hours=4.0)
    await s1.close()

    s2 = Store(tmp)
    await s2.initialize()
    t2 = PortfolioTracker(s2)
    active = await t2.load_active_exit_cooldowns()
    assert "tok_survive" in active
    await s2.close()
