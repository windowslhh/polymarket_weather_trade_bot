"""Trade execution: send orders to Polymarket CLOB."""
from __future__ import annotations

import asyncio
import logging
import uuid

from src.markets.clob_client import ClobClient
from src.markets.models import Side, TradeSignal
from src.portfolio.tracker import PortfolioTracker

logger = logging.getLogger(__name__)


class Executor:
    """Execute trade signals by placing orders on Polymarket."""

    def __init__(self, clob: ClobClient, portfolio: PortfolioTracker) -> None:
        self._clob = clob
        self._portfolio = portfolio
        # FIX-09: tracks in-flight _execute_one calls so graceful shutdown
        # can await them before the process exits.  Tasks self-remove via
        # a done callback.
        self._in_flight: set[asyncio.Task] = set()

    async def wait_until_idle(self, timeout: float = 30.0) -> bool:
        """Block until all in-flight executions finish, up to `timeout` sec.

        Returns True if everything drained in time, False on timeout.
        Used by main.py's shutdown path so we don't cut a POST mid-flight.
        """
        if not self._in_flight:
            return True
        logger.info(
            "Executor: waiting up to %.0fs for %d in-flight trade(s) to drain",
            timeout, len(self._in_flight),
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._in_flight, return_exceptions=True),
                timeout=timeout,
            )
            return True
        except asyncio.TimeoutError:
            logger.error(
                "Executor: %d trade(s) still in-flight after %.0fs — abandoning",
                len(self._in_flight), timeout,
            )
            return False

    async def execute_signals(self, signals: list[TradeSignal]) -> None:
        """Execute a batch of trade signals sequentially.

        Entry signals (BUY) are placed as limit orders.
        Exit signals (SELL) are placed at best available price.
        """
        for signal in signals:
            # FIX-09: register each _execute_one as a tracked Task so
            # wait_until_idle() can join it during graceful shutdown.
            # Awaiting the task inline preserves the previous sequential
            # semantics of the executor.
            task = asyncio.create_task(self._execute_one(signal))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)
            try:
                await task
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

        # Review 🟡 #7: dry-run mode should produce no DB side effects beyond
        # the pre-existing "decision_log" trail — in particular it must NOT
        # write to the orders table, because every dry-run cycle would
        # append an orders row that's immediately marked 'failed'.  That
        # pollutes the table and breaks the reconciler's "pending =
        # orphan" invariant.
        store = self._portfolio.store
        # Use `is True` so a MagicMock auto-attribute (truthy by default)
        # doesn't accidentally flip test harnesses into the dry-run path.
        clob_config = getattr(self._clob, "_config", None)
        is_dry_run = getattr(clob_config, "dry_run", False) is True

        if is_dry_run:
            # Just send the signal to CLOB (which logs [DRY RUN]) and return.
            # No orders row, no position insert, no reconciler breadcrumb.
            await self._clob.place_limit_order(
                token_id=signal.token_id,
                side=signal.side.value,
                price=price,
                size=shares,
            )
            return

        # FIX-03: persist a pending order before hitting CLOB so a crash between
        # the CLOB fill and the position insert leaves a discoverable breadcrumb.
        idempotency_key = uuid.uuid4().hex
        await store.insert_pending_order(
            idempotency_key=idempotency_key,
            event_id=signal.event.event_id,
            token_id=signal.token_id,
            side=signal.side.value,
            price=price,
            size_usd=size_usd,
        )

        try:
            result = await self._clob.place_limit_order(
                token_id=signal.token_id,
                side=signal.side.value,
                price=price,
                size=shares,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            # Mark as failed with the exception message so the reconciler knows
            # this one was never confirmed by CLOB — it will probe CLOB status
            # on next startup before deciding.
            await store.mark_order_failed(idempotency_key, str(exc))
            raise

        if not result.success:
            await store.mark_order_failed(
                idempotency_key, result.message or "unknown CLOB failure"
            )
            logger.error("Order failed: %s", result.message)
            return

        if signal.side == Side.BUY:
            await self._portfolio.record_fill_atomic(
                idempotency_key=idempotency_key,
                order_id=result.order_id,
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
                # the absolute floor.
                entry_ev=signal.expected_value,
                entry_win_prob=signal.estimated_win_prob,
            )
        else:  # Side.SELL
            await store.finalize_sell_order(idempotency_key, result.order_id)
            closed = await self._portfolio.close_positions_for_token(
                event_id=signal.event.event_id,
                token_id=signal.token_id,
                strategy=signal.strategy,
                exit_reason=signal.reason,
                exit_price=price,
            )
            logger.info("Closed %d positions for %s", closed, signal.slot.outcome_label)
        logger.info("Order executed successfully: %s", result.order_id)
