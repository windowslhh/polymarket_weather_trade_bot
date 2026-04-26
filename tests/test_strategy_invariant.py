"""Y6 (2026-04-26): strategy field invariant — every persisted
``strategy`` value across positions / orders / settlements must be
in {'A', 'B', 'C', 'D'}.

Three layers of defense:
  1. SQLite triggers RAISE(ABORT) on bad INSERT/UPDATE.
  2. Startup scan flags pre-existing bad rows + critical alert.
  3. Dashboard defensive routing — any unknown strategy lands in the
     legacy_a_pnl bucket so the headline still reconciles.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.portfolio.store import Store


async def _mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    s = Store(tmp)
    await s.initialize()
    return s


# ──────────────────────────────────────────────────────────────────────
# DB triggers
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inserting_bad_strategy_into_positions_raises():
    """Y6: trigger blocks an INSERT with strategy='X' (not in A/B/C/D)."""
    store = await _mk_store()
    with pytest.raises(Exception) as excinfo:
        await store.db.execute(
            """INSERT INTO positions
               (event_id, token_id, token_type, city, slot_label, side,
                entry_price, size_usd, shares, strategy)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("e1", "t1", "NO", "Miami", "x", "BUY", 0.5, 1.0, 2.0, "X"),
        )
    assert "Y6" in str(excinfo.value) or "strategy" in str(excinfo.value).lower()
    await store.close()


@pytest.mark.asyncio
async def test_inserting_lowercase_strategy_blocked():
    """Y6: 'b' (lowercase) is also a typo bug we want to catch."""
    store = await _mk_store()
    with pytest.raises(Exception):
        await store.db.execute(
            """INSERT INTO positions
               (event_id, token_id, token_type, city, slot_label, side,
                entry_price, size_usd, shares, strategy)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("e1", "t1", "NO", "Miami", "x", "BUY", 0.5, 1.0, 2.0, "b"),
        )
    await store.close()


@pytest.mark.asyncio
async def test_legitimate_strategies_pass_through():
    """Y6 sanity: A/B/C/D all insert successfully (regression guard)."""
    store = await _mk_store()
    for strat in ("A", "B", "C", "D"):
        pid = await store.insert_position(
            event_id=f"e_{strat}", token_id=f"t_{strat}", token_type="NO",
            city="Miami", slot_label="x", side="BUY",
            entry_price=0.5, size_usd=1.0, shares=2.0, strategy=strat,
        )
        assert pid
    await store.close()


@pytest.mark.asyncio
async def test_update_to_bad_strategy_blocked():
    """Y6: an UPDATE that flips strategy to 'Q' is also rejected."""
    store = await _mk_store()
    pid = await store.insert_position(
        event_id="e1", token_id="t1", token_type="NO",
        city="Miami", slot_label="x", side="BUY",
        entry_price=0.5, size_usd=1.0, shares=2.0, strategy="B",
    )
    with pytest.raises(Exception):
        await store.db.execute(
            "UPDATE positions SET strategy = 'Q' WHERE id = ?", (pid,),
        )
    await store.close()


# ──────────────────────────────────────────────────────────────────────
# Startup invariant scan
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_strategy_invariant_clean_db():
    store = await _mk_store()
    # Insert a clean row
    await store.insert_position(
        event_id="e1", token_id="t1", token_type="NO",
        city="Miami", slot_label="x", side="BUY",
        entry_price=0.5, size_usd=1.0, shares=2.0, strategy="B",
    )
    offenders = await store.validate_strategy_invariant()
    assert offenders == []
    await store.close()


@pytest.mark.asyncio
async def test_validate_strategy_invariant_flags_pre_existing_bad_rows():
    """Y6: triggers prevent NEW bad rows, but a pre-existing bad row
    (manual SQL, partial migration) should still be flagged at startup.
    Simulate by temporarily disabling the trigger, inserting, re-enabling,
    then running the invariant scan."""
    store = await _mk_store()
    # Drop triggers, insert a bad row, re-create triggers.
    await store.db.execute("DROP TRIGGER IF EXISTS trg_positions_strategy_check")
    await store.db.execute(
        """INSERT INTO positions
           (event_id, token_id, token_type, city, slot_label, side,
            entry_price, size_usd, shares, strategy)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("e1", "t1", "NO", "Miami", "x", "BUY", 0.5, 1.0, 2.0, "X"),
    )
    await store.db.commit()

    alerter = AsyncMock()
    offenders = await store.validate_strategy_invariant(alerter=alerter)
    assert ("positions", "X") in offenders
    # Critical alert was sent
    alerter.send.assert_called_once()
    args = alerter.send.call_args.args
    assert args[0] == "critical"
    assert "strategy invariant" in args[1]
    await store.close()


@pytest.mark.asyncio
async def test_validate_strategy_invariant_no_alerter_silent():
    """When no alerter is supplied, the function still returns the
    offender list — caller decides what to do."""
    store = await _mk_store()
    await store.db.execute("DROP TRIGGER IF EXISTS trg_positions_strategy_check")
    await store.db.execute(
        """INSERT INTO positions
           (event_id, token_id, token_type, city, slot_label, side,
            entry_price, size_usd, shares, strategy)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("e1", "t1", "NO", "Miami", "x", "BUY", 0.5, 1.0, 2.0, "Z"),
    )
    await store.db.commit()
    offenders = await store.validate_strategy_invariant()
    assert offenders == [("positions", "Z")]
    await store.close()


# ──────────────────────────────────────────────────────────────────────
# Dashboard defensive routing
# ──────────────────────────────────────────────────────────────────────


def test_dashboard_routes_unknown_strategy_to_legacy_bucket():
    """Y6 dashboard defense: a row with strategy='X' (not A/B/C/D)
    still lands in legacy_a_pnl, NOT in total_realized.  Pin via
    a static check on the source so a future revert is caught."""
    from pathlib import Path
    body = (Path(__file__).resolve().parents[1] / "src" / "web" / "app.py").read_text()
    assert "active_strats | legacy_strats" in body, (
        "Y6 defensive: dashboard must catch values outside {A,B,C,D}"
    )
