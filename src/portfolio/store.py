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
    order_id TEXT NOT NULL DEFAULT '',
    event_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size_usd REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending', 'filled', 'cancelled', 'failed'
    idempotency_key TEXT,
    failure_reason TEXT,
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
CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency_key ON orders(idempotency_key) WHERE idempotency_key IS NOT NULL;
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_settlements_unique ON settlements(event_id, strategy);
"""


class Store:
    """Async SQLite store for bot data."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Create database and tables, applying migrations for missing columns."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path), timeout=30.0)
        self._db.row_factory = aiosqlite.Row
        # WAL mode allows concurrent readers while a writer is active, dramatically
        # reducing "database is locked" errors when the web dashboard reads while the
        # bot writes.  Must be set before schema creation in case it changes locking.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")  # safe with WAL
        await self._db.executescript(SCHEMA)
        await self._db.commit()

        # Migrate existing tables: add columns that may be missing
        await self._migrate_columns()
        # Migrate missing indexes (best-effort; fails silently if duplicates exist)
        await self._migrate_indexes()

        logger.info("Database initialized at %s", self._db_path)

    async def _migrate_columns(self) -> None:
        """Add columns to existing tables if they don't exist yet."""
        migrations = [
            ("positions", "strategy", "ALTER TABLE positions ADD COLUMN strategy TEXT NOT NULL DEFAULT 'B'"),
            ("decision_log", "reason", "ALTER TABLE decision_log ADD COLUMN reason TEXT DEFAULT ''"),
            ("settlements", "strategy", "ALTER TABLE settlements ADD COLUMN strategy TEXT NOT NULL DEFAULT 'B'"),
            ("positions", "buy_reason", "ALTER TABLE positions ADD COLUMN buy_reason TEXT DEFAULT ''"),
            ("positions", "exit_reason", "ALTER TABLE positions ADD COLUMN exit_reason TEXT DEFAULT ''"),
            ("positions", "exit_price", "ALTER TABLE positions ADD COLUMN exit_price REAL"),
            ("positions", "realized_pnl", "ALTER TABLE positions ADD COLUMN realized_pnl REAL"),
            ("decision_log", "strategy", "ALTER TABLE decision_log ADD COLUMN strategy TEXT DEFAULT ''"),
            ("edge_history", "ensemble_spread_f", "ALTER TABLE edge_history ADD COLUMN ensemble_spread_f REAL"),
            # Fix 4: relative EV-decay TRIM — see docs/fixes/2026-04-16-strategy-p0-fixes.md#fix-4
            ("positions", "entry_ev", "ALTER TABLE positions ADD COLUMN entry_ev REAL"),
            ("positions", "entry_win_prob", "ALTER TABLE positions ADD COLUMN entry_win_prob REAL"),
            # FIX-03: orders↔positions linkage. idempotency_key uniquely names the
            # pending order across crashes so the reconciler can match it back to
            # a CLOB fill. source_order_id on positions points to the concrete CLOB
            # order_id that opened the row — existing rows are tagged 'legacy' so
            # the invariant "every position has a source" holds without backfill.
            ("orders", "idempotency_key", "ALTER TABLE orders ADD COLUMN idempotency_key TEXT"),
            ("orders", "failure_reason", "ALTER TABLE orders ADD COLUMN failure_reason TEXT"),
            ("positions", "source_order_id", "ALTER TABLE positions ADD COLUMN source_order_id TEXT DEFAULT 'legacy'"),
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

    async def _migrate_indexes(self) -> None:
        """Create indexes that may be missing on older databases.

        Uses IF NOT EXISTS so re-runs are safe.  Logs a warning (not error) if
        a UNIQUE index cannot be created because existing rows violate it — the
        bot can still run; deduplication just falls to application logic.
        """
        index_migrations = [
            (
                "idx_positions_no_dup",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_no_dup "
                "ON positions(event_id, token_id, strategy) WHERE status = 'open'",
                "deduplication index on open positions(event_id, token_id, strategy)",
            ),
            (
                "idx_orders_idempotency_key",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency_key "
                "ON orders(idempotency_key) WHERE idempotency_key IS NOT NULL",
                "unique idempotency_key on orders — reconciler keys off this",
            ),
        ]
        async with self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ) as cursor:
            existing = {row[0] async for row in cursor}

        for idx_name, sql, description in index_migrations:
            if idx_name not in existing:
                try:
                    await self.db.execute(sql)
                    await self.db.commit()
                    logger.info("Migration: created %s", description)
                except Exception as exc:
                    logger.warning(
                        "Migration: could not create %s — %s. "
                        "Duplicate-position guard is application-side only.",
                        description, exc,
                    )

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
        buy_reason: str = "",
        entry_ev: float | None = None,
        entry_win_prob: float | None = None,
    ) -> int:
        # entry_ev / entry_win_prob support the relative EV-decay TRIM
        # rule (fix 4) — allow None so older call sites remain compatible.
        cursor = await self.db.execute(
            """INSERT INTO positions (event_id, token_id, token_type, city, slot_label, side,
                                       entry_price, size_usd, shares, strategy, buy_reason,
                                       entry_ev, entry_win_prob)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, token_id, token_type, city, slot_label, side,
             entry_price, size_usd, shares, strategy, buy_reason,
             entry_ev, entry_win_prob),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def close_position(
        self,
        position_id: int,
        exit_reason: str = "",
        exit_price: float | None = None,
        realized_pnl: float | None = None,
    ) -> None:
        """Close a position, storing exit price and realized P&L."""
        await self.db.execute(
            """UPDATE positions
               SET status = 'closed', closed_at = datetime('now'),
                   exit_reason = COALESCE(?, exit_reason),
                   exit_price = COALESCE(?, exit_price),
                   realized_pnl = COALESCE(?, realized_pnl)
               WHERE id = ?""",
            (exit_reason or None, exit_price, realized_pnl, position_id),
        )
        await self.db.commit()

    async def update_exit_reason(self, position_id: int, exit_reason: str) -> None:
        """Set the exit reason on a position (standalone update, kept for backward compat)."""
        await self.db.execute(
            "UPDATE positions SET exit_reason = ? WHERE id = ?",
            (exit_reason, position_id),
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

    # ── FIX-03: pending-order lifecycle ────────────────────────────────
    # The executor flow is: persist pending row → hit CLOB → on success atomically
    # promote row to 'filled' + INSERT position. If the executor crashes between
    # steps, the 'pending' row survives and the reconciler (FIX-05) matches it
    # against CLOB state via idempotency_key.

    async def insert_pending_order(
        self,
        idempotency_key: str,
        event_id: str,
        token_id: str,
        side: str,
        price: float,
        size_usd: float,
    ) -> int:
        cursor = await self.db.execute(
            """INSERT INTO orders (order_id, event_id, token_id, side, price, size_usd,
                                   status, idempotency_key)
               VALUES ('', ?, ?, ?, ?, ?, 'pending', ?)""",
            (event_id, token_id, side, price, size_usd, idempotency_key),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def finalize_buy_order(
        self,
        idempotency_key: str,
        order_id: str,
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
        buy_reason: str = "",
        entry_ev: float | None = None,
        entry_win_prob: float | None = None,
    ) -> int:
        """Promote a pending BUY order to filled AND insert the position row atomically.

        Returns the new position id.  Raises if idempotency_key has no pending row.
        """
        # aiosqlite commits on close of execute_script or explicit commit;
        # we wrap the two writes in a single commit so a crash between them
        # is impossible — either both land or neither does.
        async with self.db.execute(
            "SELECT id FROM orders WHERE idempotency_key = ? AND status = 'pending'",
            (idempotency_key,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError(
                f"finalize_buy_order: no pending order for key={idempotency_key}"
            )
        await self.db.execute(
            "UPDATE orders SET status = 'filled', order_id = ?, filled_at = datetime('now') "
            "WHERE idempotency_key = ?",
            (order_id, idempotency_key),
        )
        pos_cursor = await self.db.execute(
            """INSERT INTO positions (event_id, token_id, token_type, city, slot_label, side,
                                       entry_price, size_usd, shares, strategy, buy_reason,
                                       entry_ev, entry_win_prob, source_order_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, token_id, token_type, city, slot_label, side,
             entry_price, size_usd, shares, strategy, buy_reason,
             entry_ev, entry_win_prob, order_id),
        )
        await self.db.commit()
        return pos_cursor.lastrowid  # type: ignore[return-value]

    async def finalize_sell_order(
        self, idempotency_key: str, order_id: str,
    ) -> None:
        """Promote a pending SELL order to filled. Position closure is done separately."""
        await self.db.execute(
            "UPDATE orders SET status = 'filled', order_id = ?, filled_at = datetime('now') "
            "WHERE idempotency_key = ?",
            (order_id, idempotency_key),
        )
        await self.db.commit()

    async def mark_order_failed(
        self, idempotency_key: str, reason: str,
    ) -> None:
        await self.db.execute(
            "UPDATE orders SET status = 'failed', failure_reason = ? "
            "WHERE idempotency_key = ?",
            (reason[:500], idempotency_key),
        )
        await self.db.commit()

    async def get_pending_orders(self) -> list[dict]:
        """Fetch all orders stuck in 'pending' — used by FIX-05 reconciler on startup."""
        async with self.db.execute(
            "SELECT * FROM orders WHERE status = 'pending' ORDER BY id"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

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
        strategy: str = "",
    ) -> None:
        await self.db.execute(
            """INSERT INTO decision_log (cycle_at, city, event_id, signal_type, slot_label,
               forecast_high_f, daily_max_f, trend_state, win_prob, expected_value, price, size_usd, action, reason, strategy)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cycle_at, city, event_id, signal_type, slot_label,
             forecast_high_f, daily_max_f, trend_state, win_prob, expected_value, price, size_usd, action, reason, strategy),
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
        ensemble_spread_f: float | None = None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO edge_history (cycle_at, city, market_date, slot_label,
               forecast_high_f, price_yes, price_no, win_prob, ev, distance_f, trend_state,
               ensemble_spread_f)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cycle_at, city, market_date, slot_label,
             forecast_high_f, price_yes, price_no, win_prob, ev, distance_f, trend_state,
             ensemble_spread_f),
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
        result = {"A": 0.0, "B": 0.0, "C": 0.0, "D": 0.0}
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
            "SELECT * FROM positions WHERE status IN ('closed', 'settled') ORDER BY closed_at DESC LIMIT ?", (limit,)
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
        # P0-2 FIX: idx_settlements_unique enforces (event_id, strategy) uniqueness,
        # so INSERT OR IGNORE alone prevents duplicates — no need for a prior SELECT.
        await self.db.execute(
            """INSERT OR IGNORE INTO settlements (event_id, city, strategy, winning_outcome, pnl)
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
