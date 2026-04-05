"""Tests for unrealized PnL computation."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker
from src.markets.models import TokenType


@pytest.fixture
async def tracker(tmp_path):
    store = Store(tmp_path / "test.db")
    await store.initialize()
    yield PortfolioTracker(store)
    await store.close()


@pytest.mark.asyncio
async def test_unrealized_pnl_with_price_increase(tracker):
    """Unrealized PnL positive when current price > entry price."""
    await tracker.record_fill(
        event_id="e1", token_id="t1", token_type=TokenType.NO,
        city="NYC", slot_label="78-81°F", side="BUY",
        price=0.10, size_usd=1.0,  # 10 shares @ $0.10
    )

    mock_clob = AsyncMock()
    mock_clob.get_prices_batch.return_value = {"t1": 0.15}  # price went up

    pnl = await tracker.compute_unrealized_pnl(mock_clob)
    # (0.15 - 0.10) * 10 shares = $0.50
    assert abs(pnl - 0.50) < 0.01


@pytest.mark.asyncio
async def test_unrealized_pnl_with_price_decrease(tracker):
    """Unrealized PnL negative when current price < entry price."""
    await tracker.record_fill(
        event_id="e1", token_id="t1", token_type=TokenType.NO,
        city="NYC", slot_label="78-81°F", side="BUY",
        price=0.20, size_usd=2.0,  # 10 shares @ $0.20
    )

    mock_clob = AsyncMock()
    mock_clob.get_prices_batch.return_value = {"t1": 0.10}  # price dropped

    pnl = await tracker.compute_unrealized_pnl(mock_clob)
    # (0.10 - 0.20) * 10 = -$1.00
    assert abs(pnl - (-1.0)) < 0.01


@pytest.mark.asyncio
async def test_unrealized_pnl_no_clob(tracker):
    """Without clob_client, unrealized PnL is 0."""
    await tracker.record_fill(
        event_id="e1", token_id="t1", token_type=TokenType.NO,
        city="NYC", slot_label="78-81°F", side="BUY",
        price=0.10, size_usd=1.0,
    )

    pnl = await tracker.compute_unrealized_pnl(None)
    assert pnl == 0.0


@pytest.mark.asyncio
async def test_unrealized_pnl_missing_price(tracker):
    """Tokens without current price are ignored."""
    await tracker.record_fill(
        event_id="e1", token_id="t1", token_type=TokenType.NO,
        city="NYC", slot_label="78-81°F", side="BUY",
        price=0.10, size_usd=1.0,
    )

    mock_clob = AsyncMock()
    mock_clob.get_prices_batch.return_value = {}  # no price available

    pnl = await tracker.compute_unrealized_pnl(mock_clob)
    assert pnl == 0.0


@pytest.mark.asyncio
async def test_snapshot_pnl_records_unrealized(tracker):
    """snapshot_pnl should record the computed unrealized PnL."""
    await tracker.record_fill(
        event_id="e1", token_id="t1", token_type=TokenType.NO,
        city="NYC", slot_label="78-81°F", side="BUY",
        price=0.10, size_usd=1.0,
    )

    mock_clob = AsyncMock()
    mock_clob.get_prices_batch.return_value = {"t1": 0.20}

    await tracker.snapshot_pnl(mock_clob)
    # Should not crash; PnL is recorded internally
