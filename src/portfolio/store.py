"""SQLite persistence layer for positions, orders, and P&L."""
from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    token_type TEXT NOT NULL,  -- 'YES' or 'NO'
    city TEXT NOT NULL,
    slot_label TEXT NOT NULL,
    side TEXT NOT NULL,        -- 'BUY' or 'SELL'
    entry_price REAL NOT NULL,
    size_usd REAL NOT NULL,
    shares REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',  -- 'open', 'closed', 'settled'
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size_usd REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending', 'filled', 'cancelled', 'failed'
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    filled_at TEXT
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    date TEXT PRIMARY KEY,
    realized_pnl REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    total_exposure REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    city TEXT NOT NULL,
    winning_outcome TEXT,
    pnl REAL NOT NULL DEFAULT 0,
    settled_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_positions_event ON positions(event_id);
CREATE INDEX IF NOT EXISTS idx_positions_city ON positions(city);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""


class Store:
    """Async SQLite store for bot data."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Create database and tables."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        logger.info("Database initialized at %s", self._db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Store not initialized. Call initialize() first."
        return self._db

    async def insert_position(
        self,
        event_id: str,
        token_id: str,
        token_type: str,
        city: str,
        slot_label: str,
        side: str,
        entry_price: float,
        size_usd: float,
        shares: float,
    ) -> int:
        cursor = await self.db.execute(
            """INSERT INTO positions (event_id, token_id, token_type, city, slot_label, side, entry_price, size_usd, shares)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, token_id, token_type, city, slot_label, side, entry_price, size_usd, shares),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def close_position(self, position_id: int) -> None:
        await self.db.execute(
            "UPDATE positions SET status = 'closed', closed_at = datetime('now') WHERE id = ?",
            (position_id,),
        )
        await self.db.commit()

    async def get_open_positions(self, event_id: str | None = None, city: str | None = None) -> list[dict]:
        query = "SELECT * FROM positions WHERE status = 'open'"
        params: list = []
        if event_id:
            query += " AND event_id = ?"
            params.append(event_id)
        if city:
            query += " AND city = ?"
            params.append(city)
        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_total_exposure(self) -> float:
        async with self.db.execute(
            "SELECT COALESCE(SUM(size_usd), 0) FROM positions WHERE status = 'open'"
        ) as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row else 0.0

    async def get_city_exposure(self, city: str) -> float:
        async with self.db.execute(
            "SELECT COALESCE(SUM(size_usd), 0) FROM positions WHERE status = 'open' AND city = ?",
            (city,),
        ) as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row else 0.0

    async def insert_order(
        self, order_id: str, event_id: str, token_id: str, side: str, price: float, size_usd: float,
    ) -> None:
        await self.db.execute(
            """INSERT INTO orders (order_id, event_id, token_id, side, price, size_usd)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (order_id, event_id, token_id, side, price, size_usd),
        )
        await self.db.commit()

    async def update_order_status(self, order_id: str, status: str) -> None:
        filled_clause = ", filled_at = datetime('now')" if status == "filled" else ""
        await self.db.execute(
            f"UPDATE orders SET status = ?{filled_clause} WHERE order_id = ?",
            (status, order_id),
        )
        await self.db.commit()

    async def get_daily_pnl(self, date_str: str) -> float | None:
        async with self.db.execute(
            "SELECT realized_pnl FROM daily_pnl WHERE date = ?", (date_str,)
        ) as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row else None

    async def upsert_daily_pnl(self, date_str: str, realized: float, unrealized: float, exposure: float) -> None:
        await self.db.execute(
            """INSERT INTO daily_pnl (date, realized_pnl, unrealized_pnl, total_exposure, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(date) DO UPDATE SET
                   realized_pnl = ?, unrealized_pnl = ?, total_exposure = ?, updated_at = datetime('now')""",
            (date_str, realized, unrealized, exposure, realized, unrealized, exposure),
        )
        await self.db.commit()
