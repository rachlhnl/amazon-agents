"""Local warehouse. Agents read from here, never from live APIs.

SQLite keeps the scaffold dependency-free; swap for Postgres/BigQuery/DuckDB
in production by reimplementing this module's small surface.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku              TEXT PRIMARY KEY,
    asin             TEXT,
    title            TEXT,
    product_type     TEXT,     -- SP-API product type, needed for listing patches
    price            REAL,     -- current list price
    landed_cost      REAL,     -- COGS + inbound freight
    fees             REAL,     -- referral + FBA fees per unit
    units_on_hand    INTEGER,
    units_inbound    INTEGER,
    daily_velocity   REAL,     -- trailing 30-day units/day
    lead_time_days   INTEGER,
    buybox_price     REAL      -- lowest competing Buy Box price, if known
);

CREATE TABLE IF NOT EXISTS keywords (
    keyword_id   TEXT PRIMARY KEY,
    campaign_id  TEXT,
    ad_group_id  TEXT,
    sku          TEXT,
    keyword      TEXT,
    match_type   TEXT,
    bid          REAL,
    clicks       INTEGER,
    spend        REAL,
    sales        REAL,
    orders       INTEGER
);

CREATE TABLE IF NOT EXISTS search_terms (
    campaign_id  TEXT,
    ad_group_id  TEXT,
    sku          TEXT,
    search_term  TEXT,
    match_type   TEXT,     -- match type of the keyword that triggered it
    clicks       INTEGER,
    spend        REAL,
    sales        REAL,
    orders       INTEGER
);

CREATE TABLE IF NOT EXISTS reviews (
    asin      TEXT,
    rating    INTEGER,
    body      TEXT,
    created   TEXT
);

CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created     TEXT DEFAULT (datetime('now')),
    agent       TEXT NOT NULL,
    kind        TEXT NOT NULL,     -- e.g. update_bid, add_negative, update_price
    target      TEXT NOT NULL,     -- keyword_id, sku, ...
    before      TEXT,
    after       TEXT,
    reason      TEXT,
    status      TEXT NOT NULL,     -- proposed | pending_approval | approved | rejected | executed | failed
    notes       TEXT
);
"""


class Warehouse:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.lastrowid

    def insert_many(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        cols = list(rows[0].keys())
        sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        self.conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
