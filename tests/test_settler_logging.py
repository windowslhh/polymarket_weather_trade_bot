"""BUG-1: settler exception logging + heartbeat.

Pre-fix the settler swallowed every exception path at debug level — a
broken Gamma response, a 404, or an internal raise inside
check_settlements all looked identical to "nothing to settle".  The
2026-04-26 audit only caught the silent no-op because we directly
queried Gamma and confirmed the events weren't closed yet; if there
HAD been a bug we'd never have known.

These tests pin:
  - 404 from Gamma → logger.warning (was silent return None)
  - Network/JSON exception → logger.exception (was logger.debug)
  - check_settlements always emits a one-line heartbeat with scan +
    skip counters, even when there's literally nothing to do
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.portfolio.store import Store
from src.settlement.settler import (
    _fetch_settlement_outcome,
    check_settlements,
)


async def _mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "bot.db"
    s = Store(tmp)
    await s.initialize()
    return s


# ──────────────────────────────────────────────────────────────────────
# Heartbeat
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_settlements_emits_heartbeat_when_no_open_positions(caplog):
    store = await _mk_store()
    caplog.set_level(logging.INFO, logger="src.settlement.settler")
    results = await check_settlements(store)
    assert results == []
    msgs = [r.message for r in caplog.records]
    assert any("Settlement check" in m for m in msgs), (
        "BUG-1: heartbeat must fire even with no open positions"
    )
    await store.close()


@pytest.mark.asyncio
async def test_check_settlements_heartbeat_counts_skipped_unclosed(caplog, monkeypatch):
    """One open position, Gamma reports closed=False → heartbeat shows
    skipped_unclosed=1, settled=0."""
    store = await _mk_store()
    pid = await store.insert_position(
        event_id="ev-unclosed", token_id="tok-1", token_type="NO",
        city="Miami", slot_label="80-81°F on April 25?",
        side="BUY", entry_price=0.5, size_usd=5.0, shares=10.0,
        strategy="B",
    )
    assert pid

    async def _fake_outcome(client, event_id):
        return None  # not closed yet

    caplog.set_level(logging.INFO, logger="src.settlement.settler")
    with patch("src.settlement.settler._fetch_settlement_outcome", _fake_outcome):
        results = await check_settlements(store)
    assert results == []
    msgs = [r.message for r in caplog.records]
    heartbeat = [m for m in msgs if "Settlement check" in m]
    assert heartbeat, "no heartbeat emitted"
    assert "skipped_unclosed=1" in heartbeat[-1]
    assert "settled 0" in heartbeat[-1]
    await store.close()


@pytest.mark.asyncio
async def test_check_settlements_heartbeat_counts_skipped_error(caplog, monkeypatch):
    """When _fetch_settlement_outcome raises, heartbeat counts as
    skipped_error and the exception is logged at exception level."""
    store = await _mk_store()
    await store.insert_position(
        event_id="ev-err", token_id="tok-2", token_type="NO",
        city="Chicago", slot_label="60-61°F on April 25?",
        side="BUY", entry_price=0.5, size_usd=5.0, shares=10.0,
        strategy="B",
    )

    async def _raise(client, event_id):
        raise RuntimeError("simulated network blip")

    caplog.set_level(logging.DEBUG, logger="src.settlement.settler")
    with patch("src.settlement.settler._fetch_settlement_outcome", _raise):
        results = await check_settlements(store)
    assert results == []
    error_records = [
        r for r in caplog.records
        if r.levelno >= logging.ERROR and "Could not fetch" in r.message
    ]
    assert error_records, "exception path must surface at ERROR level (BUG-1)"
    # Heartbeat should attribute it to skipped_error, not skipped_unclosed.
    heartbeat = [r.message for r in caplog.records if "Settlement check" in r.message]
    assert heartbeat
    assert "skipped_error=1" in heartbeat[-1]
    await store.close()


# ──────────────────────────────────────────────────────────────────────
# 404 + fetch-failure logging
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_settlement_outcome_404_warns(caplog):
    """Gamma 404 surfaces as warning (not silent return)."""
    class _Resp:
        status_code = 404

        def raise_for_status(self):
            pass

        def json(self):
            return {}

    class _Client:
        async def get(self, url):
            return _Resp()

    caplog.set_level(logging.WARNING, logger="src.settlement.settler")
    out = await _fetch_settlement_outcome(_Client(), "ev-missing")
    assert out is None
    assert any("404" in r.message and "ev-missing" in r.message for r in caplog.records), (
        "BUG-1: 404 must emit a warning naming the event"
    )


@pytest.mark.asyncio
async def test_fetch_settlement_outcome_network_error_logs_exception(caplog):
    """Generic httpx error escalates to logger.exception (was debug)."""
    class _Client:
        async def get(self, url):
            raise httpx.ConnectError("dns dead")

    caplog.set_level(logging.DEBUG, logger="src.settlement.settler")
    out = await _fetch_settlement_outcome(_Client(), "ev-net")
    assert out is None
    error_records = [
        r for r in caplog.records
        if r.levelno >= logging.ERROR and "Gamma fetch failed" in r.message
    ]
    assert error_records, "BUG-1: network error must reach ERROR level"
    assert any(r.exc_info for r in error_records), (
        "logger.exception must include traceback"
    )
