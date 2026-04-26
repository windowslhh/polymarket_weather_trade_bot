"""FIX-08: exit cooldowns must survive a bot restart.

Previously ``rebalancer._recent_exits`` was an in-memory dict only.  A
crash inside the BUY→EXIT→BUY cooldown window would reset it on the
next startup, defeating the guard that was supposed to prevent churn.

Now writes go dual to RAM + DB, and startup loads active cooldowns
from DB into the cache via ``rebalancer.load_persistent_state()``.

C-4 (2026-04-26): keys are now (token_id, strategy) tuples — see
test_record_and_readback below for the new shape.  Two variants
holding the same token track cooldowns independently so a TRIM in B
doesn't silence re-entry in C.
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
    await tracker.record_exit_cooldown("tok_A", t, cooldown_hours=4.0, strategy="B")

    active = await tracker.load_active_exit_cooldowns()
    # C-4: keys are now (token_id, strategy) tuples
    assert ("tok_A", "B") in active
    # Times round-trip to within a millisecond.
    assert abs((active[("tok_A", "B")] - t).total_seconds()) < 0.01
    await store.close()


@pytest.mark.asyncio
async def test_upsert_replaces_earlier_exit():
    """A second EXIT on the same (token, strategy) updates the row (later wins)."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    t1 = datetime.now(timezone.utc) - timedelta(minutes=30)
    t2 = datetime.now(timezone.utc)
    await tracker.record_exit_cooldown("tok_A", t1, cooldown_hours=4.0, strategy="B")
    await tracker.record_exit_cooldown("tok_A", t2, cooldown_hours=4.0, strategy="B")

    active = await tracker.load_active_exit_cooldowns()
    assert abs((active[("tok_A", "B")] - t2).total_seconds()) < 0.01
    await store.close()


@pytest.mark.asyncio
async def test_expired_rows_are_dropped():
    """Rows whose window has fully elapsed don't show up and are deleted."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    old = datetime.now(timezone.utc) - timedelta(hours=10)  # way past 4h window
    await tracker.record_exit_cooldown("tok_old", old, cooldown_hours=4.0, strategy="B")

    active = await tracker.load_active_exit_cooldowns()
    assert ("tok_old", "B") not in active

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
    await t1.record_exit_cooldown("tok_survive", now, cooldown_hours=4.0, strategy="B")
    await s1.close()

    s2 = Store(tmp)
    await s2.initialize()
    t2 = PortfolioTracker(s2)
    active = await t2.load_active_exit_cooldowns()
    assert ("tok_survive", "B") in active
    await s2.close()


# ──────────────────────────────────────────────────────────────────────
# C-4: per-strategy cooldown isolation
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_per_strategy_cooldowns_are_independent():
    """C-4: two variants holding the same token must track cooldowns
    INDEPENDENTLY.  Pre-fix the cooldown was keyed on token_id alone,
    so a TRIM in B silenced re-entry in C even though C's risk model
    might still favor the slot."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    now = datetime.now(timezone.utc)
    await tracker.record_exit_cooldown("tok_shared", now, cooldown_hours=4.0, strategy="B")

    active = await tracker.load_active_exit_cooldowns()
    # Only B's cooldown is active; C is free to re-enter
    assert ("tok_shared", "B") in active
    assert ("tok_shared", "C") not in active

    # Now record a SEPARATE cooldown for C.  B's row must NOT be touched.
    await tracker.record_exit_cooldown("tok_shared", now, cooldown_hours=4.0, strategy="C")

    active = await tracker.load_active_exit_cooldowns()
    assert ("tok_shared", "B") in active
    assert ("tok_shared", "C") in active

    # Two physical rows, not one upsert.
    async with store.db.execute(
        "SELECT COUNT(*) FROM exit_cooldowns WHERE token_id = 'tok_shared'"
    ) as cur:
        (n,) = await cur.fetchone()
    assert n == 2, (
        "C-4: each strategy must own its own cooldown row; "
        "a single ON CONFLICT(token_id) row would mean cross-variant interference"
    )
    await store.close()


@pytest.mark.asyncio
async def test_clear_exit_cooldown_targets_one_strategy():
    """When strategy is supplied, clear_exit_cooldown deletes only that
    (token, strategy) row.  Default (no strategy) clears every variant
    for the token (admin / pre-C-4 callers' shape preserved)."""
    store = await _mk_store()
    tracker = PortfolioTracker(store)

    now = datetime.now(timezone.utc)
    await tracker.record_exit_cooldown("tok_x", now, cooldown_hours=4.0, strategy="B")
    await tracker.record_exit_cooldown("tok_x", now, cooldown_hours=4.0, strategy="C")

    # Targeted clear: only B
    await store.clear_exit_cooldown("tok_x", strategy="B")
    active = await tracker.load_active_exit_cooldowns()
    assert ("tok_x", "B") not in active
    assert ("tok_x", "C") in active

    # Wide clear: both gone
    await store.clear_exit_cooldown("tok_x")
    active = await tracker.load_active_exit_cooldowns()
    assert not any(k[0] == "tok_x" for k in active)
    await store.close()


# ──────────────────────────────────────────────────────────────────────
# C-4 hotfix (2026-04-26): SCHEMA must NOT install the (token_id, strategy)
# unique index — the migration helpers do that AFTER _migrate_columns
# has added the strategy column.  Putting the index in SCHEMA breaks
# upgrade-path startup with "no such column: strategy".
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_initialize_works_on_pre_c4_exit_cooldowns_table():
    """C-4 hotfix: simulate a pre-C-4 production DB by dropping the
    `strategy` column from exit_cooldowns before re-initializing.
    Initialize must succeed (migrate adds the column + creates the
    composite unique index)."""
    import aiosqlite
    tmp = Path(tempfile.mkdtemp()) / "bot.db"

    # Pre-create an exit_cooldowns table with the OLD schema (token_id PK only)
    raw = await aiosqlite.connect(str(tmp))
    await raw.execute("""
        CREATE TABLE exit_cooldowns (
            token_id TEXT PRIMARY KEY,
            exit_time TEXT NOT NULL,
            cooldown_hours REAL NOT NULL
        )
    """)
    # Seed one legacy row to confirm it survives the migration
    await raw.execute(
        "INSERT INTO exit_cooldowns VALUES (?, ?, ?)",
        ("legacy_token", "2026-04-25 12:00:00", 4.0),
    )
    await raw.commit()
    await raw.close()

    # Now run the regular Store.initialize against this DB
    store = Store(tmp)
    await store.initialize()

    # The strategy column was added (defaulted to 'B' for the legacy row)
    async with store.db.execute(
        "SELECT token_id, strategy FROM exit_cooldowns"
    ) as cur:
        rows = await cur.fetchall()
    assert ("legacy_token", "B") in [(r[0], r[1]) for r in rows]

    # The composite unique index exists
    async with store.db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_exit_cooldowns_token_strategy'"
    ) as cur:
        idx_rows = await cur.fetchall()
    assert idx_rows, (
        "C-4 hotfix: idx_exit_cooldowns_token_strategy must be created "
        "by _migrate_indexes after _migrate_columns adds strategy"
    )
    await store.close()
