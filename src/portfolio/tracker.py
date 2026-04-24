"""Portfolio tracking: positions, exposure, and P&L."""
from __future__ import annotations

import logging
from datetime import date

from src.markets.models import TempSlot, TokenType
from src.portfolio.store import Store

logger = logging.getLogger(__name__)


class PortfolioTracker:
    """High-level portfolio operations over the store."""

    def __init__(self, store: Store) -> None:
        self._store = store

    @property
    def store(self) -> Store:
        """Public accessor for the underlying store."""
        return self._store

    async def record_fill(
        self,
        event_id: str,
        token_id: str,
        token_type: TokenType,
        city: str,
        slot_label: str,
        side: str,
        price: float,
        size_usd: float,
        strategy: str = "B",
        buy_reason: str = "",
        entry_ev: float | None = None,
        entry_win_prob: float | None = None,
    ) -> int:
        """Record a filled order as a new position.

        This path is kept for non-atomic callers (e.g. paper mode migrations).
        The executor uses `record_fill_atomic` to keep the orders/positions
        linkage consistent with the pending-order row. See FIX-03.
        """
        shares = size_usd / price if price > 0 else 0
        position_id = await self._store.insert_position(
            event_id=event_id,
            token_id=token_id,
            token_type=token_type.value,
            city=city,
            slot_label=slot_label,
            side=side,
            entry_price=price,
            size_usd=size_usd,
            shares=shares,
            strategy=strategy,
            buy_reason=buy_reason,
            entry_ev=entry_ev,
            entry_win_prob=entry_win_prob,
        )
        logger.info(
            "Position opened [%s]: %s %s %s @ %.4f ($%.2f, %.2f shares) [id=%d]",
            strategy, side, token_type.value, slot_label, price, size_usd, shares, position_id,
        )
        return position_id

    async def record_fill_atomic(
        self,
        idempotency_key: str,
        order_id: str,
        event_id: str,
        token_id: str,
        token_type: TokenType,
        city: str,
        slot_label: str,
        side: str,
        price: float,
        size_usd: float,
        strategy: str = "B",
        buy_reason: str = "",
        entry_ev: float | None = None,
        entry_win_prob: float | None = None,
    ) -> int:
        """Atomically promote a pending order to filled + insert the position.

        Raises if no pending order matches the idempotency_key.
        """
        shares = size_usd / price if price > 0 else 0
        position_id = await self._store.finalize_buy_order(
            idempotency_key=idempotency_key,
            order_id=order_id,
            event_id=event_id,
            token_id=token_id,
            token_type=token_type.value,
            city=city,
            slot_label=slot_label,
            side=side,
            entry_price=price,
            size_usd=size_usd,
            shares=shares,
            strategy=strategy,
            buy_reason=buy_reason,
            entry_ev=entry_ev,
            entry_win_prob=entry_win_prob,
        )
        logger.info(
            "Position opened [%s]: %s %s %s @ %.4f ($%.2f, %.2f shares) [id=%d src=%s]",
            strategy, side, token_type.value, slot_label, price, size_usd, shares,
            position_id, order_id,
        )
        return position_id

    async def get_total_exposure(self, strategy: str | None = None) -> float:
        """Total USD exposure across all open positions."""
        return await self._store.get_total_exposure(strategy)

    async def get_city_exposure(self, city: str, strategy: str | None = None) -> float:
        """Total USD exposure for a specific city."""
        return await self._store.get_city_exposure(city, strategy)

    async def get_held_no_slots(
        self,
        event_id: str,
        strategy: str | None = None,
        current_prices: dict[str, float] | None = None,
    ) -> list[TempSlot]:
        """Get TempSlot representations of held NO positions for an event.

        Parses temperature bounds from slot_label for accurate probability estimation.
        Uses current market prices when available (for accurate EV in trim/exit decisions),
        falling back to entry price if no current price is known.
        """
        from src.markets.discovery import _parse_temp_bounds

        positions = await self._store.get_open_positions(event_id=event_id, strategy=strategy)
        slots = []
        for pos in positions:
            if pos["token_type"] == "NO" and pos["side"] == "BUY":
                try:
                    lower, upper = _parse_temp_bounds(pos["slot_label"])
                except Exception:
                    lower, upper = None, None
                # Use current market price for EV calculation, fallback to entry price
                price = pos["entry_price"]
                if current_prices and pos["token_id"] in current_prices:
                    price = current_prices[pos["token_id"]]
                slots.append(TempSlot(
                    token_id_yes="",
                    token_id_no=pos["token_id"],
                    outcome_label=pos["slot_label"],
                    temp_lower_f=lower,
                    temp_upper_f=upper,
                    price_no=price,
                ))
        return slots

    async def close_positions_for_token(
        self,
        event_id: str,
        token_id: str,
        strategy: str | None = None,
        exit_reason: str = "",
        exit_price: float | None = None,
    ) -> int:
        """Close open positions matching event_id, token_id, and strategy.

        When strategy is provided, only closes positions for that strategy.
        This prevents a SELL signal from strategy A from closing B/C/D positions.
        Computes realized P&L = (exit_price - entry_price) * shares for NO positions.
        """
        positions = await self._store.get_open_positions(event_id=event_id, strategy=strategy)
        closed = 0
        for pos in positions:
            if pos["token_id"] == token_id and pos["status"] == "open":
                pnl: float | None = None
                if exit_price is not None:
                    pnl = (exit_price - pos["entry_price"]) * pos["shares"]
                await self._store.close_position(
                    pos["id"],
                    exit_reason=exit_reason,
                    exit_price=exit_price,
                    realized_pnl=pnl,
                )
                pnl_str = f" P&L=${pnl:.3f}" if pnl is not None else ""
                logger.info("Position closed: id=%d [%s] %s %s%s", pos["id"], pos.get("strategy", "?"), pos["slot_label"][:30], pos["token_type"], pnl_str)
                closed += 1
        return closed

    async def get_all_open_positions(self) -> list[dict]:
        """Get all open positions across all cities."""
        return await self._store.get_open_positions()

    async def get_open_positions_for_city(self, city: str) -> list[dict]:
        """Get all open positions for a city."""
        return await self._store.get_open_positions(city=city)

    async def get_open_positions_for_event(
        self, event_id: str, strategy: str | None = None,
    ) -> list[dict]:
        """Get open positions for a specific event (optionally filtered by strategy)."""
        return await self._store.get_open_positions(event_id=event_id, strategy=strategy)

    async def get_total_shares_for_token(
        self, event_id: str, token_id: str, strategy: str | None = None,
    ) -> float:
        """Return total open shares held for a specific token (used to size SELL orders).

        SELL signals carry suggested_size_usd=0 because position size is unknown at
        signal-generation time. The executor calls this to find the real share count.
        """
        positions = await self._store.get_open_positions(event_id=event_id, strategy=strategy)
        return sum(
            p["shares"]
            for p in positions
            if p["token_id"] == token_id and p["status"] == "open"
        )

    # ── Delegate methods for store operations ───────────────────────

    async def insert_edge_snapshot(self, **kwargs) -> None:
        """Delegate to store.insert_edge_snapshot()."""
        await self._store.insert_edge_snapshot(**kwargs)

    async def flush_edge_batch(self) -> None:
        """Delegate to store.flush_edge_batch()."""
        await self._store.flush_edge_batch()

    async def insert_decision_log(self, **kwargs) -> None:
        """Delegate to store.insert_decision_log()."""
        await self._store.insert_decision_log(**kwargs)

    async def get_daily_pnl(self, day: date | None = None) -> float | None:
        """Get the realized P&L for a given day."""
        d = (day or date.today()).isoformat()
        return await self._store.get_daily_pnl(d)

    async def compute_unrealized_pnl(
        self,
        clob_client=None,
        gamma_prices: dict[str, float] | None = None,
    ) -> float:
        """Compute unrealized P&L across all open positions.

        Price sources (in priority order):
        1. CLOB real-time prices (live mode)
        2. Gamma API prices from latest rebalance cycle (paper mode)
        3. Returns 0 if no prices available
        """
        positions = await self._store.get_open_positions()
        if not positions:
            return 0.0

        # Try CLOB first, then Gamma fallback
        current_prices: dict[str, float] = {}
        if clob_client:
            token_ids = [p["token_id"] for p in positions]
            current_prices = await clob_client.get_prices_batch(token_ids)

        # Merge with Gamma prices as fallback
        if gamma_prices:
            for tid, price in gamma_prices.items():
                if tid not in current_prices:
                    current_prices[tid] = price

        if not current_prices:
            return 0.0

        unrealized = 0.0
        for pos in positions:
            current = current_prices.get(pos["token_id"])
            if current is not None:
                unrealized += (current - pos["entry_price"]) * pos["shares"]
        return unrealized

    async def snapshot_pnl(
        self,
        clob_client=None,
        gamma_prices: dict[str, float] | None = None,
    ) -> None:
        """Take a daily P&L snapshot with unrealized PnL."""
        today = date.today().isoformat()
        exposure = await self._store.get_total_exposure()
        unrealized = await self.compute_unrealized_pnl(clob_client, gamma_prices)

        # Preserve existing realized_pnl (from settlements)
        existing_realized = await self._store.get_daily_pnl(today)
        realized = existing_realized or 0.0

        await self._store.upsert_daily_pnl(today, realized, unrealized, exposure)
        logger.info("P&L snapshot: exposure=$%.2f, unrealized=$%.2f, realized=$%.2f",
                     exposure, unrealized, realized)
