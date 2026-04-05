"""Alert system for trade notifications.

Supports stdout logging + optional webhook (Telegram/Discord/Slack).
"""
from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


class Alerter:
    """Send alerts via logging and optional webhook."""

    def __init__(self, webhook_url: str = "") -> None:
        self._webhook_url = webhook_url.strip()

    async def send(self, level: str, message: str) -> None:
        """Send an alert at the given level (info/warning/critical)."""
        log_fn = {
            "info": logger.info,
            "warning": logger.warning,
            "critical": logger.critical,
        }.get(level, logger.info)
        log_fn("[ALERT] %s", message)

        if self._webhook_url:
            asyncio.create_task(self._send_webhook(level, message))

    async def _send_webhook(self, level: str, message: str) -> None:
        """Fire-and-forget webhook notification."""
        icon = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level, "")
        payload = {"content": f"{icon} **[{level.upper()}]** {message}"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(self._webhook_url, json=payload)
        except Exception:
            logger.debug("Webhook alert failed (non-critical)")

    async def trade_executed(self, side: str, token_type: str, slot: str, city: str, size: float, ev: float) -> None:
        """Alert on trade execution."""
        await self.send("info", f"{side} {token_type} {slot} in {city} (${size:.2f}, EV={ev:.4f})")

    async def trade_failed(self, slot: str, city: str, error: str) -> None:
        """Alert on trade failure."""
        await self.send("warning", f"Order failed: {slot} in {city} — {error}")

    async def circuit_breaker(self, daily_pnl: float) -> None:
        """Alert on circuit breaker trigger."""
        await self.send("critical", f"Circuit breaker triggered! Daily P&L: ${daily_pnl:.2f}")

    async def rebalance_summary(self, num_signals: int, num_events: int) -> None:
        """Alert with rebalance cycle summary."""
        await self.send("info", f"Rebalance complete: {num_signals} signals across {num_events} events")
