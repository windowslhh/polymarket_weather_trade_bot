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
    ) -> int:
        """Record a filled order as a new position."""
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
        )
        logger.info(
            "Position opened [%s]: %s %s %s @ %.4f ($%.2f, %.2f shares) [id=%d]",
            strategy, side, token_type.value, slot_label, price, size_usd, shares, position_id,
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

    async def close_positions_for_token(self, event_id: str, token_id: str) -> int:
        """Close all open positions matching event_id and token_id.

        Returns number of positions closed.
        """
        positions = await self._store.get_open_positions(event_id=event_id)
        closed = 0
        for pos in positions:
            if pos["token_id"] == token_id and pos["status"] == "open":
                await self._store.close_position(pos["id"])
                logger.info("Position closed: id=%d %s %s", pos["id"], pos["slot_label"], pos["token_type"])
                closed += 1
        return closed

    async def get_all_open_positions(self) -> list[dict]:
        """Get all open positions across all cities."""
        return await self._store.get_open_positions()

    async def get_open_positions_for_city(self, city: str) -> list[dict]:
        """Get all open positions for a city."""
        return await self._store.get_open_positions(city=city)

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
