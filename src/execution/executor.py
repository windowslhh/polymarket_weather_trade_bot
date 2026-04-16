"""Trade execution: send orders to Polymarket CLOB."""
from __future__ import annotations

import logging

from src.markets.clob_client import ClobClient
from src.markets.models import Side, TradeSignal
from src.portfolio.tracker import PortfolioTracker

logger = logging.getLogger(__name__)


class Executor:
    """Execute trade signals by placing orders on Polymarket."""

    def __init__(self, clob: ClobClient, portfolio: PortfolioTracker) -> None:
        self._clob = clob
        self._portfolio = portfolio

    async def execute_signals(self, signals: list[TradeSignal]) -> None:
        """Execute a batch of trade signals sequentially.

        Entry signals (BUY) are placed as limit orders.
        Exit signals (SELL) are placed at best available price.
        """
        for signal in signals:
            try:
                await self._execute_one(signal)
            except Exception:
                logger.exception(
                    "Failed to execute signal: %s %s %s",
                    signal.side.value, signal.token_type.value, signal.slot.outcome_label,
                )

    async def _execute_one(self, signal: TradeSignal) -> None:
        price = signal.price
        size_usd = signal.suggested_size_usd

        if signal.side == Side.BUY and size_usd <= 0:
            return

        if signal.side == Side.SELL:
            # SELL signals carry suggested_size_usd=0 (sizing is unknown at signal time).
            # Look up the actual held shares so we sell the real position, not 0 shares.
            shares = await self._portfolio.get_total_shares_for_token(
                signal.event.event_id, signal.token_id, signal.strategy,
            )
            if shares <= 0:
                logger.warning(
                    "SELL signal for %s but no open shares found (already closed?), skipping",
                    signal.slot.outcome_label,
                )
                return
            # size_usd for logging: approximate current market value
            size_usd = shares * price
        else:
            shares = size_usd / price if price > 0 else 0

        logger.info(
            "Executing: %s %s %s @ %.4f ($%.2f, ~%.2f shares) EV=%.4f city=%s",
            signal.side.value,
            signal.token_type.value,
            signal.slot.outcome_label,
            price,
            size_usd,
            shares,
            signal.expected_value,
            signal.event.city,
        )

        result = await self._clob.place_limit_order(
            token_id=signal.token_id,
            side=signal.side.value,
            price=price,
            size=shares,
        )

        if result.success:
            if signal.side == Side.BUY:
                await self._portfolio.record_fill(
                    event_id=signal.event.event_id,
                    token_id=signal.token_id,
                    token_type=signal.token_type,
                    city=signal.event.city,
                    slot_label=signal.slot.outcome_label,
                    side=signal.side.value,
                    price=price,
                    size_usd=size_usd,
                    strategy=signal.strategy,
                    buy_reason=signal.reason,
                    # Fix 4: persist entry EV so the TRIM rule can use a relative
                    # decay threshold (EV decayed > X% of entry) in addition to
                    # the absolute floor. See docs/fixes/2026-04-16-strategy-p0-fixes.md#fix-4.
                    entry_ev=signal.expected_value,
                    entry_win_prob=signal.estimated_win_prob,
                )
            elif signal.side == Side.SELL:
                closed = await self._portfolio.close_positions_for_token(
                    event_id=signal.event.event_id,
                    token_id=signal.token_id,
                    strategy=signal.strategy,
                    exit_reason=signal.reason,
                    exit_price=price,
                )
                logger.info("Closed %d positions for %s", closed, signal.slot.outcome_label)
            logger.info("Order executed successfully: %s", result.order_id)
        else:
            logger.error("Order failed: %s", result.message)
