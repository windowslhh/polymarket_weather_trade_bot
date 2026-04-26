"""BUG-2: stuck-position watchdog.

A position open >48h past creation almost certainly indicates upstream
settlement is stuck (Gamma deprecated id, vendor outage, deprecated
event, etc.).  Pre-fix the bot would silently keep the row "open"
forever — the only signal was operators noticing exposure on the
dashboard.  Watchdog fires a warning alert each settlement cycle.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.portfolio.store import Store
from src.settlement.settler import check_stuck_positions


async def _mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    s = Store(tmp)
    await s.initialize()
    return s


async def _backdate(store: Store, position_id: int, hours_ago: int) -> None:
    """Push a row's created_at back so the watchdog sees it as stuck."""
    await store.db.execute(
        "UPDATE positions SET created_at = datetime('now', ?) WHERE id = ?",
        (f"-{hours_ago} hours", position_id),
    )
    await store.db.commit()


@pytest.mark.asyncio
async def test_watchdog_returns_empty_when_no_stuck_rows():
    store = await _mk_store()
    pid = await store.insert_position(
        event_id="ev-fresh", token_id="tok-1", token_type="NO",
        city="Miami", slot_label="80°F",
        side="BUY", entry_price=0.5, size_usd=5.0, shares=10.0,
        strategy="B",
    )
    assert pid
    alerter = AsyncMock()
    out = await check_stuck_positions(store, alerter)
    assert out == []
    alerter.send.assert_not_called()
    await store.close()


@pytest.mark.asyncio
async def test_watchdog_fires_warning_on_stuck_row():
    store = await _mk_store()
    pid = await store.insert_position(
        event_id="ev-stuck", token_id="tok-2", token_type="NO",
        city="Chicago", slot_label="60°F",
        side="BUY", entry_price=0.5, size_usd=5.0, shares=10.0,
        strategy="C",
    )
    await _backdate(store, pid, hours_ago=72)  # 3 days

    alerter = AsyncMock()
    out = await check_stuck_positions(store, alerter)
    assert len(out) == 1
    assert out[0]["id"] == pid
    assert out[0]["strategy"] == "C"
    alerter.send.assert_called_once()
    level, msg = alerter.send.call_args.args
    assert level == "warning"
    assert "Stuck position alert" in msg
    assert "id=%d" % pid in msg
    assert "C/Chicago" in msg
    await store.close()


@pytest.mark.asyncio
async def test_watchdog_threshold_is_inclusive_at_max_age_hours():
    """A row aged exactly max_age_hours+1h is stuck; max_age_hours-1h is not."""
    store = await _mk_store()
    p_old = await store.insert_position(
        event_id="e-old", token_id="t-old", token_type="NO",
        city="Miami", slot_label="x", side="BUY",
        entry_price=0.5, size_usd=1.0, shares=2.0, strategy="B",
    )
    p_young = await store.insert_position(
        event_id="e-young", token_id="t-young", token_type="NO",
        city="Miami", slot_label="x", side="BUY",
        entry_price=0.5, size_usd=1.0, shares=2.0, strategy="B",
    )
    await _backdate(store, p_old, hours_ago=49)
    await _backdate(store, p_young, hours_ago=47)

    alerter = AsyncMock()
    out = await check_stuck_positions(store, alerter, max_age_hours=48)
    ids = {r["id"] for r in out}
    assert p_old in ids
    assert p_young not in ids
    await store.close()


@pytest.mark.asyncio
async def test_watchdog_summarises_first_10_when_many_stuck():
    """When > 10 rows stuck, summary truncates with '+N more' suffix to
    avoid spamming the webhook."""
    store = await _mk_store()
    ids = []
    for i in range(13):
        pid = await store.insert_position(
            event_id=f"e{i}", token_id=f"t{i}", token_type="NO",
            city="Miami", slot_label="x", side="BUY",
            entry_price=0.5, size_usd=1.0, shares=2.0, strategy="B",
        )
        await _backdate(store, pid, hours_ago=72)
        ids.append(pid)

    alerter = AsyncMock()
    out = await check_stuck_positions(store, alerter)
    assert len(out) == 13
    msg = alerter.send.call_args.args[1]
    assert "13 positions" in msg
    assert "+3 more" in msg
    await store.close()


@pytest.mark.asyncio
async def test_watchdog_ignores_closed_and_settled_positions():
    """Only status='open' rows count; closed/settled are by definition resolved."""
    store = await _mk_store()
    pid_open = await store.insert_position(
        event_id="e-open", token_id="t-open", token_type="NO",
        city="Miami", slot_label="x", side="BUY",
        entry_price=0.5, size_usd=1.0, shares=2.0, strategy="B",
    )
    pid_closed = await store.insert_position(
        event_id="e-closed", token_id="t-closed", token_type="NO",
        city="Miami", slot_label="x", side="BUY",
        entry_price=0.5, size_usd=1.0, shares=2.0, strategy="B",
    )
    await _backdate(store, pid_open, hours_ago=72)
    await _backdate(store, pid_closed, hours_ago=72)
    await store.close_position(pid_closed, exit_reason="manual")

    alerter = AsyncMock()
    out = await check_stuck_positions(store, alerter)
    assert {r["id"] for r in out} == {pid_open}
    await store.close()
