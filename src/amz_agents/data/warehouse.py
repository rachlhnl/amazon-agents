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
    status      TEXT NOT NULL,     -- proposed | pending_approval | approved | rejected | executed | failed | acknowledged | done
    notes       TEXT,
    sku         TEXT,              -- product the action affects, for outcome tracking
    impact_usd  REAL,              -- chief of staff's estimate of 30-day profit impact
    confidence  TEXT,              -- low | medium | high
    goal        TEXT,              -- goal this action serves, if any
    executed_at TEXT,
    team        TEXT,              -- team that owns the work
    effort      TEXT,              -- XS | S | M | L | XL
    roi         REAL,              -- impact_usd / cost of the effort
    priority    TEXT,              -- now | high | medium | low | judgment
    done_at     TEXT               -- when a person marked human-only work done
);

-- One row per run, and one per agent per run, for the tracker.
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started    TEXT DEFAULT (datetime('now')),
    finished   TEXT,
    mode       TEXT,
    submitted  INTEGER,
    skipped    INTEGER
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id     INTEGER,
    agent      TEXT,
    team       TEXT,
    proposed   INTEGER,
    problems   TEXT,               -- JSON list; empty = healthy
    PRIMARY KEY (run_id, agent)
);

-- Daily per-SKU history (Sales & Traffic + Ads reports). The chief of staff
-- uses it for goal progress, anomaly detection, and judging past actions.
CREATE TABLE IF NOT EXISTS metrics_daily (
    date       TEXT,
    sku        TEXT,
    units      INTEGER,
    revenue    REAL,
    ad_spend   REAL,
    ad_sales   REAL,
    PRIMARY KEY (date, sku)
);

CREATE TABLE IF NOT EXISTS outcomes (
    action_id  INTEGER PRIMARY KEY,
    evaluated  TEXT DEFAULT (datetime('now')),
    metric     TEXT,
    before     REAL,
    after      REAL,
    verdict    TEXT                -- helped | hurt | neutral
);

-- ---- Outside Amazon -------------------------------------------------------

-- One row per SKU per non-Amazon sales channel (own store, other marketplaces).
CREATE TABLE IF NOT EXISTS channel_listings (
    channel      TEXT,     -- e.g. shopify, walmart, tiktok
    sku          TEXT,
    external_id  TEXT,     -- the channel's own ID (Shopify variant GID, ...)
    price        REAL,
    fees         REAL,     -- channel fees + fulfillment per unit
    units_30d    INTEGER,
    fulfillment  TEXT,     -- mcf (ships from FBA stock) | own
    listed       INTEGER,  -- 1 if live on the channel
    PRIMARY KEY (channel, sku)
);

-- Paid campaigns outside Amazon (Meta, Google, TikTok...), trailing 30 days.
-- destination=amazon campaigns get sales from Amazon Attribution.
CREATE TABLE IF NOT EXISTS external_campaigns (
    campaign_id   TEXT PRIMARY KEY,  -- platform's ID of the unit whose budget we change
    platform      TEXT,              -- meta | google | tiktok
    name          TEXT,
    sku           TEXT,
    destination   TEXT,              -- amazon | <channel>, e.g. shopify
    status        TEXT,              -- enabled | paused
    daily_budget  REAL,
    spend_30d     REAL,
    clicks_30d    INTEGER,
    orders_30d    INTEGER,
    sales_30d     REAL
);

-- Wholesale / B2B pipeline (from the CRM).
CREATE TABLE IF NOT EXISTS leads (
    lead_id           TEXT PRIMARY KEY,
    company           TEXT,
    contact           TEXT,
    email             TEXT,
    stage             TEXT,     -- new | contacted | negotiating | won | lost
    est_annual_value  REAL,
    last_contact      TEXT,     -- ISO date
    sku_interest      TEXT,     -- comma-separated SKUs
    notes             TEXT
);

-- ---- Creative and UX -------------------------------------------------------

-- Ad-level results outside Amazon. Two windows so fatigue shows up: the last
-- 7 days vs. days 8-28 before that.
CREATE TABLE IF NOT EXISTS creatives (
    ad_id             TEXT PRIMARY KEY,
    platform          TEXT,
    campaign_id       TEXT,     -- external_campaigns.campaign_id (Meta: the ad set)
    name              TEXT,
    sku               TEXT,
    status            TEXT,     -- enabled | paused
    landing_url       TEXT,
    impressions_7d    INTEGER,
    clicks_7d         INTEGER,
    spend_7d          REAL,
    frequency_7d      REAL,     -- average times each person saw it
    purchases_7d      INTEGER,
    impressions_prior INTEGER,  -- days 8-28
    clicks_prior      INTEGER,
    spend_prior       REAL
);

-- What's on each Amazon detail page.
CREATE TABLE IF NOT EXISTS listing_assets (
    sku           TEXT PRIMARY KEY,
    image_count   INTEGER,
    has_video     INTEGER,    -- 1/0, NULL if unknown
    has_aplus     INTEGER,
    bullet_count  INTEGER,
    title_length  INTEGER
);

-- Amazon detail-page traffic and conversion, trailing 30 days (Sales & Traffic).
CREATE TABLE IF NOT EXISTS asin_traffic (
    sku               TEXT PRIMARY KEY,
    sessions_30d      INTEGER,
    page_views_30d    INTEGER,
    units_30d         INTEGER,
    buy_box_pct       REAL     -- 0-1
);

-- Own store, daily funnel by device (GA4 or a Shopify analytics export).
CREATE TABLE IF NOT EXISTS web_funnel (
    date         TEXT,
    device       TEXT,       -- mobile | desktop | tablet
    sessions     INTEGER,
    add_to_cart  INTEGER,
    checkout     INTEGER,
    purchases    INTEGER,
    revenue      REAL,
    PRIMARY KEY (date, device)
);

-- Own-store pages: speed/accessibility (PageSpeed) and landing-page results (GA4).
CREATE TABLE IF NOT EXISTS web_pages (
    url              TEXT PRIMARY KEY,   -- no query string
    page_type        TEXT,               -- home | product | collection | landing
    sku              TEXT,
    perf_mobile      INTEGER,            -- Lighthouse performance 0-100
    perf_desktop     INTEGER,
    accessibility    INTEGER,            -- Lighthouse accessibility 0-100
    lcp_ms_mobile    REAL,               -- largest contentful paint
    cls_mobile       REAL,               -- cumulative layout shift
    sessions_30d     INTEGER,            -- as a landing page
    purchases_30d    INTEGER,
    checked          TEXT                -- date PageSpeed last ran
);
"""


class Warehouse:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The tracker server answers one request at a time, possibly off the thread
        # that opened the warehouse.
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a warehouse was first created."""
        added = {
            "actions": [("sku", "TEXT"), ("impact_usd", "REAL"), ("confidence", "TEXT"),
                        ("goal", "TEXT"), ("executed_at", "TEXT"), ("team", "TEXT"),
                        ("effort", "TEXT"), ("roi", "REAL"), ("priority", "TEXT"),
                        ("done_at", "TEXT")],
        }
        for table, cols in added.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols:
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.conn.commit()

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
