"""Trade execution: send orders to Polymarket CLOB."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from src.markets.clob_client import ClobClient
from src.markets.models import Side, TradeSignal
from src.portfolio.tracker import PortfolioTracker

logger = logging.getLogger(__name__)


class Executor:
    """Execute trade signals by placing orders on Polymarket."""

    def __init__(
        self,
        clob: ClobClient,
        portfolio: PortfolioTracker,
        config: Any = None,
    ) -> None:
        """C-2 (2026-04-26): ``config`` (an ``AppConfig``) is now an
        explicit constructor argument so the executor doesn't reach
        into ``self._clob._config`` at runtime.  Pre-fix the executor
        peeked at ``getattr(self._clob, "_config", None).strategy.
        max_total_exposure_usd`` for the batch cap, and the same
        attribute for ``.dry_run`` — a circular dependency that broke
        any test harness mocking the CLOB and forced both classes to
        share a hidden contract.

        ``config=None`` is preserved for the legacy fallback path so
        existing call sites (some test fixtures, dry_run_offline) keep
        working without an immediate cascade of constructor changes.
        New code should always pass config explicitly.
        """
        self._clob = clob
        self._portfolio = portfolio
        self._config = config  # C-2: explicit injection
        # FIX-09: tracks in-flight _execute_one calls so graceful shutdown
        # can await them before the process exits.  Tasks self-remove via
        # a done callback.
        self._in_flight: set[asyncio.Task] = set()

    # ── C-2 helpers ───────────────────────────────────────────────────

    def _resolve_max_total_exposure(self) -> float | None:
        """Read max_total_exposure_usd from injected config (preferred)
        or fall back to the legacy ``self._clob._config`` path.  Returns
        None when neither source provides a numeric value (e.g. test
        harness with MagicMock attributes)."""
        for src in (self._config, getattr(self._clob, "_config", None)):
            limit = getattr(getattr(src, "strategy", None), "max_total_exposure_usd", None)
            if isinstance(limit, (int, float)):
                return float(limit)
        return None

    def _resolve_is_dry_run(self) -> bool:
        """Same dual-source resolution for the dry-run flag."""
        for src in (self._config, getattr(self._clob, "_config", None)):
            if getattr(src, "dry_run", False) is True:
                return True
        return False

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
        #
        # C-2 (2026-04-26): pre-fix the trim mechanism mutated input
        # signals (`s.suggested_size_usd = 0.0`) AND read the cap by
        # peeking at `self._clob._config` (reverse coupling).  Both
        # changed: the cap is now read via _resolve_max_total_exposure
        # (constructor injection preferred), and trimmed signals are
        # tracked in a `skipped_ids` set passed to _execute_one — the
        # caller's TradeSignal objects are never mutated, so they remain
        # auditable and replay-safe.  Each trimmed signal also gets a
        # decision_log REJECT entry with reason=BATCH_CAP_EXCEEDED so
        # the dashboard can show "why didn't this trade fire?".
        total_buy_cost = sum(
            s.suggested_size_usd for s in signals
            if s.side == Side.BUY and s.suggested_size_usd > 0
        )
        skipped_ids: set[int] = set()
        if total_buy_cost > 0:
            try:
                existing = await self._portfolio.get_total_exposure()
            except Exception:
                existing = 0.0  # fail-open; per-signal check still applies
            limit = self._resolve_max_total_exposure()
            if limit is not None and existing + total_buy_cost > limit:
                trim_target = max(limit - existing, 0.0)
                logger.warning(
                    "Executor: batch total_buy_cost=$%.2f would push exposure "
                    "to $%.2f (cap $%.2f) — trimming new BUYs to $%.2f",
                    total_buy_cost, existing + total_buy_cost, limit, trim_target,
                )
                # Walk BUYs in order, keeping each whole signal while
                # budget remains; collect the rest in skipped_ids so
                # _execute_one short-circuits without mutating the input.
                running = 0.0
                for s in signals:
                    if s.side != Side.BUY or s.suggested_size_usd <= 0:
                        continue
                    if running + s.suggested_size_usd > trim_target:
                        skipped_ids.add(id(s))
                        await self._log_batch_cap_reject(
                            s, existing, trim_target, limit,
                        )
                    else:
                        running += s.suggested_size_usd

        for signal in signals:
            if id(signal) in skipped_ids:
                continue  # C-2: skipped above with decision_log entry
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

    async def _log_batch_cap_reject(
        self, signal: TradeSignal,
        existing_exposure: float, trim_target: float, cap: float,
    ) -> None:
        """C-2: emit a decision_log REJECT entry for a batch-cap-trimmed
        signal so the dashboard can answer 'why didn't this trade fire?'.
        Wrapped in try/except: a logging failure must never block the
        rest of the batch."""
        try:
            cycle_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            await self._portfolio.store.insert_decision_log(
                cycle_at=cycle_at,
                city=signal.event.city,
                event_id=signal.event.event_id,
                signal_type="REJECT",
                slot_label=signal.slot.outcome_label,
                forecast_high_f=None,
                daily_max_f=None,
                trend_state="",
                win_prob=signal.estimated_win_prob,
                expected_value=signal.expected_value,
                price=signal.price,
                size_usd=signal.suggested_size_usd,
                action="SKIP",
                reason=(
                    f"[{signal.strategy}] REJECT: BATCH_CAP_EXCEEDED "
                    f"(existing=${existing_exposure:.2f}, "
                    f"trim_target=${trim_target:.2f}, cap=${cap:.2f})"
                ),
                strategy=signal.strategy,
            )
        except Exception:
            logger.exception(
                "Failed to log BATCH_CAP_EXCEEDED REJECT for %s",
                signal.slot.outcome_label,
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
        # C-2: prefer the injected config; fall back to legacy
        # ``self._clob._config`` for callers that haven't migrated.  The
        # `is True` check (rather than truthy) keeps a MagicMock auto-attr
        # from accidentally flipping the test harness into dry-run.
        is_dry_run = self._resolve_is_dry_run()

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
