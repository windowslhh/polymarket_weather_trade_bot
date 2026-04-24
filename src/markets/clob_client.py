"""Async wrapper around py-clob-client for Polymarket CLOB API."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from src.config import AppConfig

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    order_id: str
    success: bool
    message: str = ""


@dataclass
class Position:
    token_id: str
    size: float
    avg_price: float
    side: str  # "BUY"


class ClobClient:
    """Async wrapper for Polymarket CLOB operations.

    Uses py-clob-client under the hood, wrapping sync calls with asyncio.to_thread.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._client = None

    def _get_client(self):
        """Lazy-init the py-clob-client."""
        if self._client is None:
            try:
                from py_clob_client.client import ClobClient as _ClobClient
                from py_clob_client.clob_types import ApiCreds

                creds = ApiCreds(
                    api_key=self._config.polymarket_api_key,
                    api_secret=self._config.polymarket_secret,
                    api_passphrase=self._config.polymarket_passphrase,
                )
                self._client = _ClobClient(
                    "https://clob.polymarket.com",
                    key=self._config.eth_private_key,
                    chain_id=137,  # Polygon
                    creds=creds,
                )
            except ImportError:
                logger.error("py-clob-client not installed. Install with: pip install py-clob-client")
                raise
        return self._client

    async def get_orderbook(self, token_id: str) -> dict:
        """Get the order book for a token."""
        client = self._get_client()
        return await asyncio.to_thread(client.get_order_book, token_id)

    async def get_midpoint(self, token_id: str) -> float | None:
        """Get the midpoint price for a token."""
        client = self._get_client()
        try:
            result = await asyncio.to_thread(client.get_midpoint, token_id)
            return float(result)
        except Exception:
            logger.exception("Failed to get midpoint for %s", token_id)
            return None

    async def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        idempotency_key: str | None = None,
    ) -> OrderResult:
        """Place a limit order.

        `idempotency_key` is not accepted by py-clob-client itself but is threaded
        through so logs/paper-returns carry the breadcrumb the reconciler (FIX-05)
        keys off of.
        """
        key_suffix = f" key={idempotency_key[:8]}" if idempotency_key else ""
        if self._config.dry_run:
            logger.info(
                "[DRY RUN] Would place %s order: token=%s price=%.4f size=%.2f%s",
                side, token_id, price, size, key_suffix,
            )
            return OrderResult(order_id="dry_run", success=False, message="dry run — no positions recorded")

        if self._config.paper:
            logger.info(
                "[PAPER] Simulated %s fill: token=%s price=%.4f size=%.2f%s",
                side, token_id, price, size, key_suffix,
            )
            # Paper order_ids must be unique per order so the orders-table UNIQUE
            # index (and the reconciler) can tell them apart.  Using the
            # idempotency key (first 12 hex chars) gives a stable, collision-free
            # handle without leaking the full uuid into logs.
            suffix = idempotency_key[:12] if idempotency_key else token_id[:8]
            return OrderResult(order_id=f"paper_{suffix}", success=True, message="paper trade")

        client = self._get_client()
        try:
            from py_clob_client.order_builder.constants import BUY, SELL

            order_side = BUY if side.upper() == "BUY" else SELL
            order = await asyncio.to_thread(
                client.create_and_post_order,
                {
                    "tokenID": token_id,
                    "price": price,
                    "side": order_side,
                    "size": size,
                },
            )
            order_id = order.get("orderID", "") if isinstance(order, dict) else str(order)
            logger.info(
                "Order placed: %s %s @ %.4f x %.2f -> %s%s",
                side, token_id, price, size, order_id, key_suffix,
            )
            # FIX-M4: empty order_id from CLOB means the API accepted the call
            # but returned no handle — treat as failure so the executor does
            # not record a fill without a traceable source_order_id.
            if not order_id:
                return OrderResult(
                    order_id="", success=False,
                    message="CLOB returned empty order_id",
                )
            return OrderResult(order_id=order_id, success=True)
        except Exception as e:
            logger.exception("Failed to place order")
            return OrderResult(order_id="", success=False, message=str(e))

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if self._config.dry_run:
            logger.info("[DRY RUN] Would cancel order %s", order_id)
            return True

        client = self._get_client()
        try:
            await asyncio.to_thread(client.cancel, order_id)
            logger.info("Order cancelled: %s", order_id)
            return True
        except Exception:
            logger.exception("Failed to cancel order %s", order_id)
            return False

    async def get_last_trade_price(self, token_id: str) -> float | None:
        """Get the last trade price for a token via CLOB API."""
        if self._config.dry_run:
            return None  # no real prices in dry-run
        client = self._get_client()
        try:
            result = await asyncio.to_thread(client.get_last_trade_price, token_id)
            return float(result)
        except Exception:
            logger.debug("Failed to get last trade price for %s", token_id)
            return None

    async def get_prices_batch(self, token_ids: list[str]) -> dict[str, float]:
        """Get current prices for multiple tokens. Returns available prices.

        In dry-run/paper mode, returns empty dict (no CLOB auth available).
        Callers should fallback to Gamma API prices.
        """
        if self._config.dry_run or self._config.paper:
            return {}

        prices: dict[str, float] = {}
        for token_id in token_ids:
            price = await self.get_midpoint(token_id) or await self.get_last_trade_price(token_id)
            if price is not None:
                prices[token_id] = price
        return prices

    async def get_positions(self) -> list[Position]:
        """Get all open positions (simplified)."""
        # Note: py-clob-client doesn't have a direct get_positions method.
        # Positions are typically tracked locally via our portfolio module.
        # This is a placeholder for any future API integration.
        return []
