"""Async wrapper around py-clob-client for Polymarket CLOB API."""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

from src.config import AppConfig

logger = logging.getLogger(__name__)

# FIX-04: network resilience knobs. Kept module-level so tests can monkeypatch
# them without touching the client.
ORDER_TIMEOUT_S = 30.0
ORDER_MAX_ATTEMPTS = 3
# 429 rate-limit: sleep min(2**n + jitter, cap). Polymarket doesn't publish a
# Retry-After contract on the CLOB, so we rely on exponential-with-jitter and
# cap to keep the bot responsive rather than stalling for minutes.
RATE_LIMIT_CAP_S = 30.0


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Best-effort heuristic: py-clob-client wraps requests, so 429s arrive as
    generic exceptions whose stringified form contains '429' or 'rate limit'.
    """
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "rate_limit" in msg or "too many" in msg


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
        from py_clob_client.order_builder.constants import BUY, SELL

        order_side = BUY if side.upper() == "BUY" else SELL
        order_payload = {
            "tokenID": token_id,
            "price": price,
            "side": order_side,
            "size": size,
        }

        # FIX-04: bounded timeout, retry with exponential backoff on transient
        # failures, distinct longer backoff on 429.  Without the timeout, a hung
        # call to create_and_post_order freezes the asyncio.to_thread executor
        # indefinitely — in prod we've seen 60+ minute hangs.
        last_exc: Exception | None = None
        for attempt in range(ORDER_MAX_ATTEMPTS):
            try:
                async with asyncio.timeout(ORDER_TIMEOUT_S):
                    order = await asyncio.to_thread(
                        client.create_and_post_order, order_payload,
                    )
                order_id = (
                    order.get("orderID", "") if isinstance(order, dict) else str(order)
                )
                logger.info(
                    "Order placed: %s %s @ %.4f x %.2f -> %s%s (attempt=%d)",
                    side, token_id, price, size, order_id, key_suffix, attempt + 1,
                )
                # FIX-M4: empty order_id is treated as failure (see above).
                if not order_id:
                    return OrderResult(
                        order_id="", success=False,
                        message="CLOB returned empty order_id",
                    )
                return OrderResult(order_id=order_id, success=True)
            except TimeoutError as e:
                last_exc = e
                logger.warning(
                    "place_limit_order timed out after %.0fs (attempt=%d/%d)",
                    ORDER_TIMEOUT_S, attempt + 1, ORDER_MAX_ATTEMPTS,
                )
            except Exception as e:
                last_exc = e
                if _is_rate_limit_error(e):
                    sleep_s = min(2 ** attempt + random.random(), RATE_LIMIT_CAP_S)
                    logger.warning(
                        "CLOB 429 on place_limit_order (attempt=%d/%d), "
                        "backing off %.2fs",
                        attempt + 1, ORDER_MAX_ATTEMPTS, sleep_s,
                    )
                    await asyncio.sleep(sleep_s)
                    continue
                logger.warning(
                    "place_limit_order failed (attempt=%d/%d): %s",
                    attempt + 1, ORDER_MAX_ATTEMPTS, e,
                )
            # Non-rate-limit retriable path: short exponential backoff between
            # retries so we don't hammer the CLOB. Skip the sleep on the final
            # attempt since we're about to return anyway.
            if attempt < ORDER_MAX_ATTEMPTS - 1:
                await asyncio.sleep(0.5 * (2 ** attempt) + random.random() * 0.25)

        logger.exception("Order placement exhausted retries", exc_info=last_exc)
        return OrderResult(
            order_id="", success=False, message=str(last_exc) if last_exc else "retries exhausted",
        )

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
