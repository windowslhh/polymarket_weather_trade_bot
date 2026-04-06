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
        )
        logger.info(
            "Position opened: %s %s %s @ %.4f ($%.2f, %.2f shares) [id=%d]",
            side, token_type.value, slot_label, price, size_usd, shares, position_id,
        )
        return position_id

    async def get_total_exposure(self) -> float:
        """Total USD exposure across all open positions."""
        return await self._store.get_total_exposure()

    async def get_city_exposure(self, city: str) -> float:
        """Total USD exposure for a specific city."""
        return await self._store.get_city_exposure(city)

    async def get_held_no_slots(self, event_id: str) -> list[TempSlot]:
        """Get TempSlot representations of held NO positions for an event.

        Parses temperature bounds from slot_label for accurate probability estimation.
        """
        from src.markets.discovery import _parse_temp_bounds

        positions = await self._store.get_open_positions(event_id=event_id)
        slots = []
        for pos in positions:
            if pos["token_type"] == "NO" and pos["side"] == "BUY":
                lower, upper = _parse_temp_bounds(pos["slot_label"])
                slots.append(TempSlot(
                    token_id_yes="",
                    token_id_no=pos["token_id"],
                    outcome_label=pos["slot_label"],
                    temp_lower_f=lower,
                    temp_upper_f=upper,
                    price_no=pos["entry_price"],
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

    async def compute_unrealized_pnl(self, clob_client=None) -> float:
        """Compute unrealized P&L across all open positions.

        If clob_client is provided, fetches current market prices.
        Otherwise uses entry prices (unrealized = 0).
        """
        positions = await self._store.get_open_positions()
        if not positions or clob_client is None:
            return 0.0

        token_ids = [p["token_id"] for p in positions]
        current_prices = await clob_client.get_prices_batch(token_ids)

        unrealized = 0.0
        for pos in positions:
            current = current_prices.get(pos["token_id"])
            if current is not None:
                unrealized += (current - pos["entry_price"]) * pos["shares"]
        return unrealized

    async def snapshot_pnl(self, clob_client=None) -> None:
        """Take a daily P&L snapshot with unrealized PnL."""
        today = date.today().isoformat()
        exposure = await self._store.get_total_exposure()
        unrealized = await self.compute_unrealized_pnl(clob_client)
        await self._store.upsert_daily_pnl(today, 0, unrealized, exposure)
        logger.info("P&L snapshot: exposure=$%.2f, unrealized=$%.2f", exposure, unrealized)
