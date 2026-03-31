"""Async SQLite database helpers for trading bots."""

from __future__ import annotations

import aiosqlite
from pathlib import Path
from datetime import datetime
from typing import Any

import structlog

logger = structlog.get_logger()


class Database:
    """Async SQLite database wrapper.

    Usage::

        db = Database("data/bot.db")
        await db.init(extra_schema="CREATE TABLE IF NOT EXISTS ...")
        async with db.connection() as conn:
            await conn.execute(...)
    """

    def __init__(self, path: str = "data/bot.db"):
        self.path = path

    async def init(self, extra_schema: str = "") -> None:
        """Create tables. Call once at startup."""
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(COMMON_SCHEMA)
            if extra_schema:
                await db.executescript(extra_schema)
            await db.commit()
        logger.info("database_initialized", path=self.path)

    async def connection(self) -> aiosqlite.Connection:
        """Get a connection with Row factory enabled."""
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        return db

    async def migrate(self, table: str, column: str, col_type: str) -> None:
        """Safe ALTER TABLE ADD COLUMN — no-op if column exists."""
        async with aiosqlite.connect(self.path) as db:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                await db.commit()
                logger.info("migration_applied", table=table, column=column)
            except Exception:
                pass  # Column already exists

    async def upsert(
        self,
        table: str,
        data: dict[str, Any],
        conflict_columns: list[str],
        update_columns: list[str] | None = None,
    ) -> None:
        """INSERT ... ON CONFLICT DO UPDATE for the given dict."""
        columns = list(data.keys())
        placeholders = ", ".join("?" for _ in columns)
        col_names = ", ".join(columns)
        conflict = ", ".join(conflict_columns)

        if update_columns is None:
            update_columns = [c for c in columns if c not in conflict_columns]
        update_clause = ", ".join(f"{c}=excluded.{c}" for c in update_columns)

        sql = (
            f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict}) DO UPDATE SET {update_clause}"
        )
        async with aiosqlite.connect(self.path) as db:
            await db.execute(sql, list(data.values()))
            await db.commit()

    async def get_latest(self, table: str, order_col: str = "timestamp", limit: int = 1) -> list[dict]:
        """Get the most recent rows from a table."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT ?", (limit,))
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_range(
        self,
        table: str,
        start: str | datetime,
        end: str | datetime,
        time_col: str = "timestamp",
    ) -> list[dict]:
        """Get rows within a time range."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM {table} WHERE {time_col} >= ? AND {time_col} <= ? ORDER BY {time_col}",
                (str(start), str(end)),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def insert_batch(self, table: str, rows: list[dict]) -> int:
        """Insert multiple rows. Returns count inserted."""
        if not rows:
            return 0
        columns = list(rows[0].keys())
        placeholders = ", ".join("?" for _ in columns)
        col_names = ", ".join(columns)
        sql = f"INSERT OR IGNORE INTO {table} ({col_names}) VALUES ({placeholders})"
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(sql, [list(r.values()) for r in rows])
            await db.commit()
            return len(rows)


COMMON_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    signal_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL,
    exit_price REAL,
    size REAL,
    pnl REAL,
    fees REAL DEFAULT 0,
    funding_pnl REAL DEFAULT 0,
    paper_trade BOOLEAN DEFAULT 0,
    status TEXT DEFAULT 'open',
    opened_at TIMESTAMP,
    closed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence REAL,
    entry_price REAL,
    stop_loss REAL,
    take_profit REAL,
    source TEXT,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP,
    expires_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_summary (
    date TEXT PRIMARY KEY,
    trades_opened INTEGER DEFAULT 0,
    trades_closed INTEGER DEFAULT 0,
    realized_pnl REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0,
    fees_paid REAL DEFAULT 0,
    equity REAL,
    max_drawdown_pct REAL
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol, opened_at);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status, created_at);
"""
