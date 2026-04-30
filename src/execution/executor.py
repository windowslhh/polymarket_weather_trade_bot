"""Trade execution: send orders to Polymarket CLOB."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

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
        # FIX-M2: pre-compute total BUY cost across the whole batch and
        # cross-check against the max_total_exposure_usd cap BEFORE we
        # fire any orders.  The per-signal sizer (rebalancer.compute_size)
        # already respects the cap, but a batch of signals built from a
        # stale exposure snapshot could still cross the cap if every
        # signal independently looked fine.  This belt-and-braces check
        # trims the tail of the batch rather than rejecting it outright.
        total_buy_cost = sum(
            s.suggested_size_usd for s in signals
            if s.side == Side.BUY and s.suggested_size_usd > 0
        )
        if total_buy_cost > 0:
            try:
                existing = await self._portfolio.get_total_exposure()
            except Exception:
                existing = 0.0  # fail-open; per-signal check still applies
            max_total = getattr(
                getattr(self._clob, "_config", None), "strategy", None,
            )
            limit = getattr(max_total, "max_total_exposure_usd", None) if max_total else None
            # isinstance guard so a test harness using MagicMock (where the
            # attribute resolves to a truthy MagicMock instance) doesn't
            # trip the real comparison with a bogus numeric value.
            if isinstance(limit, (int, float)) and existing + total_buy_cost > limit:
                trim_target = max(limit - existing, 0.0)
                logger.warning(
                    "Executor: batch total_buy_cost=$%.2f would push exposure "
                    "to $%.2f (cap $%.2f) — trimming new BUYs to $%.2f",
                    total_buy_cost, existing + total_buy_cost, limit, trim_target,
                )
                # Walk BUYs in order, keeping each whole signal while
                # budget remains; mark the rest suggested_size_usd=0 so
                # _execute_one short-circuits.
                running = 0.0
                for s in signals:
                    if s.side != Side.BUY or s.suggested_size_usd <= 0:
                        continue
                    if running + s.suggested_size_usd > trim_target:
                        s.suggested_size_usd = 0.0
                    else:
                        running += s.suggested_size_usd

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
            db_shares = await self._portfolio.get_total_shares_for_token(
                signal.event.event_id, signal.token_id, signal.strategy,
            )
            # Bug C Phase 1 (2026-04-29): clamp SELL size by on-chain ERC1155
            # balance.  The DB ``shares`` column is computed at fill time as
            # ``size_usd / limit_price`` which ignores both fill slippage and
            # the Polymarket BUY taker fee (deducted in shares from the token
            # side).  When DB > chain, the matcher 400's "not enough balance"
            # — Denver 2026-04-29 SELL was the trigger.  Phase 2 fixes the DB
            # writer to record on-chain net shares directly, but legacy rows
            # still drift; this clamp is the permanent safety net.
            #
            # Paper-mode short-circuit: paper has no real chain position so
            # ``get_conditional_balance`` returns 0 (the wrapper currently
            # swallows errors and 0's as well — see clob_client.py:300-335).
            # Without this short-circuit, paper SELLs would silently skip
            # because ``min(db, 0) == 0``.  Trust DB shares in paper.
            clob_config = getattr(self._clob, "_config", None)
            is_paper = getattr(clob_config, "paper", False) is True
            if is_paper:
                shares = db_shares
            else:
                on_chain_raw = -1
                try:
                    on_chain_raw = await self._clob.get_conditional_balance(
                        signal.token_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "SELL chain balance query failed token=%s: %s "
                        "— fallback to db_shares=%.6f",
                        signal.token_id[:12], exc, db_shares,
                    )
                    on_chain_raw = -1  # sentinel for fallback
                if on_chain_raw < 0:
                    shares = db_shares
                else:
                    on_chain_shares = on_chain_raw / 1_000_000.0
                    shares = min(db_shares, on_chain_shares)
                    if on_chain_shares < db_shares:
                        drift = db_shares - on_chain_shares
                        drift_pct = (drift / db_shares * 100.0) if db_shares > 0 else 0.0
                        logger.warning(
                            "SELL clamped to chain bal token=%s db=%.6f "
                            "chain=%.6f drift=%.6f (%.2f%%)",
                            signal.token_id[:12], db_shares, on_chain_shares,
                            drift, drift_pct,
                        )
            if shares <= 0:
                logger.warning(
                    "SELL signal for %s but no shares to sell (db=%.4f), skipping",
                    signal.slot.outcome_label, db_shares,
                )
                return
            # size_usd for logging: approximate current market value
            size_usd = shares * price

            # Polymarket min-order gate (2026-04-29).  Q1's GTC→FAK
            # cutover changed SELL to taker semantics: the 5-share floor
            # is GTC-only, FAK uses the $1 notional minimum instead.
            # Keeping the share gate would permanently block legitimate
            # stop-loss exits of sub-5-share positions; if Polymarket
            # still 400's at the new minimum, the existing exception
            # path handles it cleanly.
            strat_cfg = getattr(
                getattr(self._clob, "_config", None), "strategy", None,
            )
            min_amount = getattr(strat_cfg, "min_order_amount_usd", 0.0)
            if isinstance(min_amount, (int, float)) and size_usd < min_amount:
                reason_code = "AMOUNT_BELOW_MIN_USD"
            else:
                reason_code = None
            if reason_code is not None:
                logger.warning(
                    "SELL skipped (%s): %.4f shares × %.4f = $%.4f (event=%s slot=%s)",
                    reason_code, shares, price, size_usd,
                    signal.event.event_id, signal.slot.outcome_label,
                )
                try:
                    cycle_at = datetime.now(timezone.utc).isoformat()
                    await self._portfolio.store.insert_decision_log(
                        cycle_at=cycle_at,
                        city=signal.event.city,
                        event_id=signal.event.event_id,
                        signal_type=signal.token_type.value,
                        slot_label=signal.slot.outcome_label,
                        forecast_high_f=None,
                        daily_max_f=None,
                        trend_state="",
                        win_prob=signal.estimated_win_prob,
                        expected_value=signal.expected_value,
                        price=price,
                        size_usd=size_usd,
                        action="SKIP",
                        reason=f"[{signal.strategy}] SELL_REJECT: {reason_code}",
                        strategy=signal.strategy,
                    )
                except Exception:
                    logger.debug(
                        "Failed to insert SELL_REJECT decision_log for %s",
                        signal.slot.outcome_label,
                    )
                return
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
        # H-1: include signal.strategy so the reconciler can match SELL orders
        # to the right variant's position when two variants hold the same token.
        idempotency_key = uuid.uuid4().hex
        order_created_at = int(time.time())
        await store.insert_pending_order(
            idempotency_key=idempotency_key,
            event_id=signal.event.event_id,
            token_id=signal.token_id,
            side=signal.side.value,
            price=price,
            size_usd=size_usd,
            strategy=signal.strategy,
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
            # No active cancel needed.  The wrapper sends FAK orders (see
            # ``clob_client.place_limit_order``), so the Polymarket server
            # has already killed any unfilled remainder server-side before
            # we observed success=False — there is no resting order to
            # cancel.  The earlier A1 best-effort cancel that ran here was
            # only correct for the GTC-era "unmatched-and-resting" failure
            # mode, which FAK eliminates by design.
            #
            # FAK-cross-pricing fix (2026-04-30): the wrapper now pre-flights
            # the order book and returns success=False with a structured
            # message ("THIN_LIQUIDITY_NO_BID/ASK", "SLIPPAGE_TOO_HIGH ...")
            # when the book gate would never cross.  These are NOT CLOB
            # rejects — the order was never submitted.  Surface them at
            # WARNING with the token id and reason so a retry storm is easy
            # to spot in tail-f.  decision_log persistence is intentionally
            # deferred (Patch C follow-up): writing from executor would need
            # a new evaluator/executor edge and the simpler observe-first
            # approach reveals whether retry storm is severe enough to
            # warrant the plumbing cost.
            failure_msg = result.message or ""
            if any(
                code in failure_msg
                for code in ("THIN_LIQUIDITY", "SLIPPAGE_TOO_HIGH")
            ):
                side_value = (
                    signal.side.value
                    if hasattr(signal.side, "value")
                    else str(signal.side)
                )
                logger.warning(
                    "Order rejected by book gate token=%s side=%s "
                    "reason=%s — same signal will likely re-trigger next cycle",
                    signal.token_id[:12], side_value, failure_msg,
                )
            return

        if signal.side == Side.BUY:
            # 2026-04-28: pull actual fill data from /data/trades so the
            # dashboard's "Entry" column reflects effective per-share cost
            # (limit-vs-fill slippage was previously hidden) and the fee
            # paid is captured for net-P&L reporting.  Best-effort —
            # ``get_fill_summary`` returns None on paper / dry-run, on
            # SDK error, or when the trade hasn't propagated yet; in
            # those cases match_price stays NULL and the dashboard falls
            # back to the limit price.
            match_price: float | None = None
            fee_paid_usd: float | None = None
            actual_shares: float | None = None
            try:
                summary = await self._clob.get_fill_summary(
                    token_id=signal.token_id,
                    order_id=result.order_id,
                    created_at_epoch=order_created_at,
                )
            except Exception:
                logger.exception(
                    "get_fill_summary raised for %s — falling back to limit price",
                    result.order_id,
                )
                summary = None
            if summary is not None:
                match_price = summary.match_price
                fee_paid_usd = summary.fee_paid_usd
                # Bug C (2026-04-29): record on-chain net shares so DB matches
                # ERC1155 balance.  Old formula ``size_usd / limit_price``
                # drifted by both fill slippage and the BUY taker fee
                # (Polymarket deducts fee in shares from the token side).
                actual_shares = summary.net_shares

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
                match_price=match_price,
                fee_paid_usd=fee_paid_usd,
                actual_shares=actual_shares,
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
