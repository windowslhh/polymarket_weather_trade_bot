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

        Returns simplified TempSlot objects (without full price data)
        for use in exit signal evaluation.
        """
        positions = await self._store.get_open_positions(event_id=event_id)
        slots = []
        for pos in positions:
            if pos["token_type"] == "NO" and pos["side"] == "BUY":
                slots.append(TempSlot(
                    token_id_yes="",
                    token_id_no=pos["token_id"],
                    outcome_label=pos["slot_label"],
                    temp_lower_f=None,  # not stored; evaluator uses label
                    temp_upper_f=None,
                    price_no=pos["entry_price"],
                ))
        return slots

    async def get_open_positions_for_city(self, city: str) -> list[dict]:
        """Get all open positions for a city."""
        return await self._store.get_open_positions(city=city)

    async def get_daily_pnl(self, day: date | None = None) -> float | None:
        """Get the realized P&L for a given day."""
        d = (day or date.today()).isoformat()
        return await self._store.get_daily_pnl(d)

    async def snapshot_pnl(self) -> None:
        """Take a daily P&L snapshot."""
        today = date.today().isoformat()
        exposure = await self._store.get_total_exposure()
        # Realized P&L would be computed from closed positions
        # For now, just record exposure
        await self._store.upsert_daily_pnl(today, 0, 0, exposure)
        logger.info("P&L snapshot: exposure=$%.2f", exposure)
