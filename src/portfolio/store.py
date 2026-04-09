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
    strategy TEXT NOT NULL DEFAULT 'B',   -- 'A', 'B', 'C' strategy group
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
    strategy TEXT NOT NULL DEFAULT 'B',
    winning_outcome TEXT,
    pnl REAL NOT NULL DEFAULT 0,
    settled_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_at TEXT NOT NULL DEFAULT (datetime('now')),
    city TEXT NOT NULL,
    event_id TEXT,
    signal_type TEXT,
    slot_label TEXT,
    forecast_high_f REAL,
    daily_max_f REAL,
    trend_state TEXT,
    win_prob REAL,
    expected_value REAL,
    price REAL,
    size_usd REAL,
    action TEXT,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_event ON positions(event_id);
CREATE INDEX IF NOT EXISTS idx_positions_city ON positions(city);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE TABLE IF NOT EXISTS edge_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_at TEXT NOT NULL DEFAULT (datetime('now')),
    city TEXT NOT NULL,
    market_date TEXT NOT NULL,
    slot_label TEXT NOT NULL,
    forecast_high_f REAL,
    price_yes REAL,
    price_no REAL,
    win_prob REAL,
    ev REAL,
    distance_f REAL,
    trend_state TEXT
);

CREATE INDEX IF NOT EXISTS idx_decision_log_cycle ON decision_log(cycle_at);
CREATE INDEX IF NOT EXISTS idx_edge_history_cycle ON edge_history(cycle_at);
CREATE INDEX IF NOT EXISTS idx_edge_history_city ON edge_history(city);
"""


class Store:
    """Async SQLite store for bot data."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Create database and tables, applying migrations for missing columns."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

        # Migrate existing tables: add columns that may be missing
        await self._migrate_columns()

        logger.info("Database initialized at %s", self._db_path)

    async def _migrate_columns(self) -> None:
        """Add columns to existing tables if they don't exist yet."""
        migrations = [
            ("positions", "strategy", "ALTER TABLE positions ADD COLUMN strategy TEXT NOT NULL DEFAULT 'B'"),
            ("decision_log", "reason", "ALTER TABLE decision_log ADD COLUMN reason TEXT DEFAULT ''"),
            ("settlements", "strategy", "ALTER TABLE settlements ADD COLUMN strategy TEXT NOT NULL DEFAULT 'B'"),
        ]
        for table, column, sql in migrations:
            try:
                async with self.db.execute(f"PRAGMA table_info({table})") as cursor:
                    columns = {row[1] async for row in cursor}
                if column not in columns:
                    await self.db.execute(sql)
                    await self.db.commit()
                    logger.info("Migration: added column '%s' to table '%s'", column, table)
            except Exception:
                logger.exception("Migration failed for %s.%s", table, column)

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
        strategy: str = "B",
    ) -> int:
        cursor = await self.db.execute(
            """INSERT INTO positions (event_id, token_id, token_type, city, slot_label, side, entry_price, size_usd, shares, strategy)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, token_id, token_type, city, slot_label, side, entry_price, size_usd, shares, strategy),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def close_position(self, position_id: int) -> None:
        await self.db.execute(
            "UPDATE positions SET status = 'closed', closed_at = datetime('now') WHERE id = ?",
            (position_id,),
        )
        await self.db.commit()

    async def get_open_positions(self, event_id: str | None = None, city: str | None = None, strategy: str | None = None) -> list[dict]:
        query = "SELECT * FROM positions WHERE status = 'open'"
        params: list = []
        if event_id:
            query += " AND event_id = ?"
            params.append(event_id)
        if city:
            query += " AND city = ?"
            params.append(city)
        if strategy:
            query += " AND strategy = ?"
            params.append(strategy)
        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_total_exposure(self, strategy: str | None = None) -> float:
        if strategy:
            query = "SELECT COALESCE(SUM(size_usd), 0) FROM positions WHERE status = 'open' AND strategy = ?"
            params = (strategy,)
        else:
            query = "SELECT COALESCE(SUM(size_usd), 0) FROM positions WHERE status = 'open'"
            params = ()
        async with self.db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row else 0.0

    async def get_city_exposure(self, city: str, strategy: str | None = None) -> float:
        if strategy:
            query = "SELECT COALESCE(SUM(size_usd), 0) FROM positions WHERE status = 'open' AND city = ? AND strategy = ?"
            params = (city, strategy)
        else:
            query = "SELECT COALESCE(SUM(size_usd), 0) FROM positions WHERE status = 'open' AND city = ?"
            params = (city,)
        async with self.db.execute(query, params) as cursor:
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

    async def insert_decision_log(
        self, cycle_at: str, city: str, event_id: str, signal_type: str,
        slot_label: str, forecast_high_f: float | None, daily_max_f: float | None,
        trend_state: str, win_prob: float, expected_value: float,
        price: float, size_usd: float, action: str, reason: str = "",
    ) -> None:
        await self.db.execute(
            """INSERT INTO decision_log (cycle_at, city, event_id, signal_type, slot_label,
               forecast_high_f, daily_max_f, trend_state, win_prob, expected_value, price, size_usd, action, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cycle_at, city, event_id, signal_type, slot_label,
             forecast_high_f, daily_max_f, trend_state, win_prob, expected_value, price, size_usd, action, reason),
        )
        await self.db.commit()

    async def get_decision_log(self, limit: int = 50) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM decision_log ORDER BY id DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def insert_edge_snapshot(
        self, cycle_at: str, city: str, market_date: str, slot_label: str,
        forecast_high_f: float, price_yes: float, price_no: float,
        win_prob: float, ev: float, distance_f: float, trend_state: str,
    ) -> None:
        await self.db.execute(
            """INSERT INTO edge_history (cycle_at, city, market_date, slot_label,
               forecast_high_f, price_yes, price_no, win_prob, ev, distance_f, trend_state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cycle_at, city, market_date, slot_label,
             forecast_high_f, price_yes, price_no, win_prob, ev, distance_f, trend_state),
        )

    async def flush_edge_batch(self) -> None:
        """Commit pending edge inserts."""
        await self.db.commit()

    async def get_edge_history(self, city: str | None = None, limit: int = 200) -> list[dict]:
        if city:
            query = "SELECT * FROM edge_history WHERE city = ? ORDER BY id DESC LIMIT ?"
            params = (city, limit)
        else:
            query = "SELECT * FROM edge_history ORDER BY id DESC LIMIT ?"
            params = (limit,)
        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_edge_summary(self) -> list[dict]:
        """Aggregate edge stats per city: avg EV, max EV, opportunities count."""
        async with self.db.execute("""
            SELECT city,
                   COUNT(*) as total_scans,
                   SUM(CASE WHEN ev > 0.02 THEN 1 ELSE 0 END) as edge_opportunities,
                   ROUND(AVG(ev), 4) as avg_ev,
                   ROUND(MAX(ev), 4) as max_ev,
                   ROUND(AVG(win_prob), 3) as avg_win_prob,
                   MIN(cycle_at) as first_scan,
                   MAX(cycle_at) as last_scan
            FROM edge_history
            GROUP BY city
            ORDER BY edge_opportunities DESC
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_strategy_summary(self) -> list[dict]:
        """Get P&L and exposure summary per strategy group."""
        async with self.db.execute("""
            SELECT strategy,
                   COUNT(*) as total_positions,
                   SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open_positions,
                   SUM(CASE WHEN status = 'settled' THEN 1 ELSE 0 END) as settled_positions,
                   ROUND(SUM(CASE WHEN status = 'open' THEN size_usd ELSE 0 END), 2) as exposure,
                   ROUND(SUM(CASE WHEN status = 'open' THEN size_usd ELSE 0 END), 2) as open_cost
            FROM positions
            GROUP BY strategy
            ORDER BY strategy
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_strategy_settlements(self) -> list[dict]:
        """Get settlement P&L per strategy from the settlements table."""
        async with self.db.execute("""
            SELECT strategy,
                   COUNT(*) as settled_count,
                   ROUND(SUM(pnl), 2) as realized_pnl
            FROM settlements
            GROUP BY strategy
            ORDER BY strategy
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_strategy_realized_pnl(self) -> dict[str, float]:
        """Get realized P&L per strategy as a simple dict."""
        result = {"A": 0.0, "B": 0.0, "C": 0.0, "D": 0.0, "E": 0.0, "F": 0.0}
        async with self.db.execute("""
            SELECT strategy, ROUND(SUM(pnl), 4) as total_pnl
            FROM settlements
            GROUP BY strategy
        """) as cursor:
            async for row in cursor:
                s = row[0]
                if s in result:
                    result[s] = float(row[1])
        return result

    async def get_closed_positions(self, limit: int = 20) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM positions WHERE status = 'closed' ORDER BY closed_at DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_pnl_history(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT 30"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def insert_settlement(
        self, event_id: str, city: str, winning_outcome: str, pnl: float,
        strategy: str = "B",
    ) -> None:
        # Check if already exists for this event+strategy to prevent duplicate P&L
        async with self.db.execute(
            "SELECT COUNT(*) FROM settlements WHERE event_id = ? AND strategy = ?",
            (event_id, strategy),
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0] > 0:
                return  # Already settled, skip

        await self.db.execute(
            """INSERT INTO settlements (event_id, city, strategy, winning_outcome, pnl)
               VALUES (?, ?, ?, ?, ?)""",
            (event_id, city, strategy, winning_outcome, pnl),
        )
        await self.db.commit()

    async def get_settlements(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM settlements ORDER BY settled_at DESC LIMIT 50"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
