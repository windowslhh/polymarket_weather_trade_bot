"""Tests for alert system."""
from __future__ import annotations

import pytest

from src.alerts import Alerter


@pytest.mark.asyncio
async def test_alerter_logs_without_webhook(caplog):
    """Alerter should work fine without webhook URL."""
    alerter = Alerter()
    import logging
    with caplog.at_level(logging.INFO, logger="src.alerts"):
        await alerter.send("info", "Test message")
    assert "Test message" in caplog.text


@pytest.mark.asyncio
async def test_circuit_breaker_alert(caplog):
    alerter = Alerter()
    import logging
    with caplog.at_level(logging.CRITICAL, logger="src.alerts"):
        await alerter.circuit_breaker(-55.0)
    assert "Circuit breaker" in caplog.text


@pytest.mark.asyncio
async def test_rebalance_summary(caplog):
    alerter = Alerter()
    import logging
    with caplog.at_level(logging.INFO, logger="src.alerts"):
        await alerter.rebalance_summary(15, 5)
    assert "15 signals" in caplog.text


@pytest.mark.asyncio
async def test_trade_executed(caplog):
    alerter = Alerter()
    import logging
    with caplog.at_level(logging.INFO, logger="src.alerts"):
        await alerter.trade_executed("BUY", "NO", "78-81°F", "NYC", 3.50, 0.04)
    assert "BUY NO" in caplog.text


@pytest.mark.asyncio
async def test_webhook_failure_does_not_crash():
    """Even with a bad webhook URL, alerter should not crash."""
    alerter = Alerter(webhook_url="https://invalid.example.com/webhook")
    # Should not raise
    await alerter.send("info", "Test")
