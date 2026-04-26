"""BUG-4: settler writes (positions UPDATE + settlements INSERT +
daily_pnl UPDATE) must land in a single transaction.

Pre-fix the three writes each had their own commit:
  (1) UPDATE positions ... + commit
  (2) per-strategy INSERT settlements (each commit)
  (3) UPDATE daily_pnl + commit

A crash between (1) and (2) marked positions settled but left no audit
trail in `settlements`, and the next cycle would skip the now-settled
rows because check_settlements only loops over status='open'.  The
daily_pnl gap was permanent.

These tests pin atomicity by injecting a failure after the positions
UPDATE and asserting we end up in a clean rollback state (positions
still 'open', settlements still empty, daily_pnl untouched).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.portfolio.store import Store
from src.settlement import settler as settler_mod
from src.settlement.settler import SettlementOutcome, check_settlements


async def _mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    s = Store(tmp)
    await s.initialize()
    return s


async def _seed_open_position(store: Store, event_id: str, strategy: str = "B") -> int:
    return await store.insert_position(
        event_id=event_id,
        token_id=f"no_{event_id}",
        token_type="NO",
        city="Miami",
        slot_label="80-81°F on April 25?",
        side="BUY",
        entry_price=0.5,
        size_usd=5.0,
        shares=10.0,
        strategy=strategy,
    )


async def _outcome_no_wins(event_id: str) -> SettlementOutcome:
    """Return an outcome where YES = 0 → NO wins (gives positive PnL)."""
    return SettlementOutcome(
        winning_slot="78-79°F on April 25?",  # different slot won, our NO wins
        label_prices={"78-79°F on April 25?": 1.0, "80-81°F on April 25?": 0.0},
        token_prices={f"no_{event_id}": 0.0},
    )


@pytest.mark.asyncio
async def test_happy_path_lands_all_three_writes():
    store = await _mk_store()
    pid = await _seed_open_position(store, "ev1")
    assert pid

    async def _outcome(client, event_id):
        return await _outcome_no_wins(event_id)

    with patch.object(settler_mod, "_fetch_settlement_outcome", _outcome):
        results = await check_settlements(store)
    assert len(results) == 1

    # All three writes visible
    async with store.db.execute(
        "SELECT status, realized_pnl FROM positions WHERE id = ?", (pid,),
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "settled"
    assert row[1] is not None and row[1] > 0  # NO won → positive PnL

    async with store.db.execute(
        "SELECT COUNT(*) FROM settlements WHERE event_id = ?", ("ev1",),
    ) as cur:
        (n_settle,) = await cur.fetchone()
    assert n_settle == 1

    async with store.db.execute("SELECT realized_pnl FROM daily_pnl") as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] is not None and row[0] > 0
    await store.close()


@pytest.mark.asyncio
async def test_failure_after_position_update_rolls_back_atomically():
    """Injection: positions UPDATE succeeds for the first row, then
    _compute_position_pnl raises on the second row.  Pre-BUG-4 the
    first position would already be 'settled' with realized_pnl set,
    but the settlements + daily_pnl writes would be missing.
    Post-BUG-4 the whole event rolls back."""
    store = await _mk_store()
    pid1 = await _seed_open_position(store, "ev2", strategy="B")
    pid2 = await _seed_open_position(store, "ev2", strategy="C")
    assert pid1 and pid2

    async def _outcome(client, event_id):
        return await _outcome_no_wins(event_id)

    real_pnl = settler_mod._compute_position_pnl
    calls = {"n": 0}

    def _pnl_raise_on_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated mid-loop failure")
        return real_pnl(*args, **kwargs)

    with patch.object(settler_mod, "_fetch_settlement_outcome", _outcome), \
         patch.object(settler_mod, "_compute_position_pnl", _pnl_raise_on_second):
        results = await check_settlements(store)
    assert results == []  # event was rolled back, not in results

    # BOTH positions must still be 'open' — first one's UPDATE rolled back
    async with store.db.execute(
        "SELECT id, status, realized_pnl FROM positions ORDER BY id",
    ) as cur:
        rows = await cur.fetchall()
    assert all(r[1] == "open" for r in rows), (
        f"BUG-4: positions UPDATE was not rolled back on mid-loop failure: {rows}"
    )
    assert all(r[2] is None for r in rows)

    # No settlements row leaked
    async with store.db.execute(
        "SELECT COUNT(*) FROM settlements WHERE event_id = ?", ("ev2",),
    ) as cur:
        (n_settle,) = await cur.fetchone()
    assert n_settle == 0

    # No daily_pnl row leaked
    async with store.db.execute("SELECT COUNT(*) FROM daily_pnl") as cur:
        (n_dpnl,) = await cur.fetchone()
    assert n_dpnl == 0
    await store.close()


@pytest.mark.asyncio
async def test_replay_after_rollback_is_idempotent():
    """Inject failure on first cycle, then on retry let it succeed.
    Final state must be exactly one settlement row (not duplicated)."""
    store = await _mk_store()
    pid1 = await _seed_open_position(store, "ev4", strategy="B")
    pid2 = await _seed_open_position(store, "ev4", strategy="C")
    assert pid1 and pid2

    async def _outcome(client, event_id):
        return await _outcome_no_wins(event_id)

    real_pnl = settler_mod._compute_position_pnl
    inject = {"on": True, "n": 0}

    def _pnl_maybe_raise(*args, **kwargs):
        inject["n"] += 1
        if inject["on"] and inject["n"] == 2:
            raise RuntimeError("first-cycle blip")
        return real_pnl(*args, **kwargs)

    with patch.object(settler_mod, "_fetch_settlement_outcome", _outcome), \
         patch.object(settler_mod, "_compute_position_pnl", _pnl_maybe_raise):
        # First cycle — fail
        first = await check_settlements(store)
        assert first == []
        # Second cycle — succeed (counter resets, no raise)
        inject["on"] = False
        inject["n"] = 0
        results = await check_settlements(store)
    assert len(results) == 1

    async with store.db.execute(
        "SELECT strategy FROM settlements WHERE event_id = ? ORDER BY strategy",
        ("ev4",),
    ) as cur:
        rows = await cur.fetchall()
    strategies = [r[0] for r in rows]
    # Both B and C settle on retry; idempotency guarantees no duplicates.
    assert strategies == ["B", "C"], (
        f"settlements should be one row per strategy after replay, got {strategies}"
    )
    await store.close()


@pytest.mark.asyncio
async def test_failure_at_daily_pnl_query_phase_does_not_corrupt_state():
    """If get_total_exposure (queried BEFORE the try block) fails, the
    settler must surface the error and skip the event.  No position
    updates should leak.  This is a defence-in-depth check on the
    pre-transaction phase."""
    store = await _mk_store()
    pid = await _seed_open_position(store, "ev5")
    assert pid

    async def _outcome(client, event_id):
        return await _outcome_no_wins(event_id)

    async def _broken_exposure():
        raise RuntimeError("exposure query died")

    with patch.object(settler_mod, "_fetch_settlement_outcome", _outcome), \
         patch.object(store, "get_total_exposure", _broken_exposure):
        # Outer except in check_settlements catches per-event errors and
        # continues; we just need the position to remain 'open'.
        await check_settlements(store)

    async with store.db.execute(
        "SELECT status FROM positions WHERE id = ?", (pid,),
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "open"
    await store.close()
