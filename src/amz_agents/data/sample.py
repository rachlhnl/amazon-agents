"""Synthetic sample data so the suite runs end-to-end without credentials."""

from __future__ import annotations

import json
from datetime import date, timedelta

from .warehouse import Warehouse

PRODUCTS = [
    # sku, asin, title, price, landed_cost, fees, on_hand, inbound, velocity, lead_time, buybox
    ("BTL-32-BLK", "B0SAMPLE01", "Insulated Water Bottle 32oz Black", 29.99, 7.10, 9.40, 420, 600, 18.0, 45, 27.49),
    ("BTL-32-SGE", "B0SAMPLE02", "Insulated Water Bottle 32oz Sage", 29.99, 7.10, 9.40, 95, 0, 11.0, 45, None),
    ("LID-STRAW", "B0SAMPLE03", "Replacement Straw Lid", 12.99, 1.80, 5.20, 1600, 0, 6.5, 30, 12.99),
    ("BTL-64-BLK", "B0SAMPLE04", "Insulated Water Bottle 64oz Black", 39.99, 10.20, 11.80, 60, 300, 4.0, 45, 36.00),
]

KEYWORDS = [
    # keyword_id, campaign, ad_group, sku, keyword, match, bid, clicks, spend, sales, orders
    ("kw1", "c1", "ag1", "BTL-32-BLK", "insulated water bottle", "exact", 1.40, 520, 728.0, 2399.2, 80),
    ("kw2", "c1", "ag1", "BTL-32-BLK", "water bottle 32 oz", "exact", 1.10, 310, 341.0, 659.8, 22),
    ("kw3", "c1", "ag1", "BTL-32-BLK", "metal water bottle", "phrase", 0.95, 200, 190.0, 179.9, 6),
    ("kw4", "c2", "ag2", "BTL-32-SGE", "green water bottle", "exact", 0.80, 90, 72.0, 479.8, 16),
    ("kw5", "c2", "ag2", "BTL-32-SGE", "aesthetic water bottle", "broad", 0.70, 140, 98.0, 0.0, 0),
]

SEARCH_TERMS = [
    # campaign, ad_group, sku, term, match, clicks, spend, sales, orders
    ("c3", "ag3", "BTL-32-BLK", "insulated water bottle with straw lid", "broad", 45, 40.5, 239.9, 8),
    ("c3", "ag3", "BTL-32-BLK", "gym water bottle 32oz", "broad", 30, 27.0, 89.97, 3),
    ("c3", "ag3", "BTL-32-BLK", "glass water bottle", "broad", 38, 34.2, 0.0, 0),
    ("c3", "ag3", "BTL-32-BLK", "kids water bottle", "broad", 25, 21.0, 0.0, 0),
    ("c3", "ag3", "BTL-32-BLK", "water bottle 40 oz", "broad", 12, 10.8, 29.99, 1),
    ("c1", "ag1", "BTL-32-BLK", "insulated water bottle", "exact", 520, 728.0, 2399.2, 80),
]

REVIEWS = [
    ("B0SAMPLE01", 5, "Keeps water cold all day. Love it.", "2026-09-30"),
    ("B0SAMPLE01", 2, "Lid leaked in my bag after two weeks.", "2026-10-02"),
    ("B0SAMPLE01", 1, "Leaks from the lid. Returned.", "2026-10-08"),
    ("B0SAMPLE01", 4, "Great bottle but doesn't fit my car cup holder.", "2026-10-10"),
    ("B0SAMPLE02", 5, "Beautiful color, exactly as pictured.", "2026-10-05"),
]


def _history(today: date) -> list[dict]:
    """28 days of per-SKU history ending yesterday, shaped to exercise the chief
    of staff: a bid cut 10 days ago and a price cut 12 days ago that both helped,
    flat Sage sales against a growth goal, and a sudden drop on the 64oz yesterday."""
    rows = []
    for ago in range(28, 0, -1):
        d = (today - timedelta(days=ago)).isoformat()
        wobble = (ago % 3) - 1  # deterministic -1/0/+1
        blk_units = 19 + wobble if ago <= 7 else 18 + wobble
        lid_units = (6 if ago % 2 else 7) if ago <= 11 else 5
        lid_price = 12.99 if ago <= 11 else 13.99
        big_units = 0 if ago == 1 else 4 + wobble
        rows += [
            {"date": d, "sku": "BTL-32-BLK", "units": blk_units, "revenue": blk_units * 29.99,
             "ad_spend": 42.0 if ago <= 9 else 50.0, "ad_sales": 180.0 + 10 * wobble},
            {"date": d, "sku": "BTL-32-SGE", "units": 11 + wobble, "revenue": (11 + wobble) * 29.99,
             "ad_spend": 6.0, "ad_sales": 25.0},
            {"date": d, "sku": "LID-STRAW", "units": lid_units, "revenue": lid_units * lid_price,
             "ad_spend": 2.0, "ad_sales": 10.0},
            {"date": d, "sku": "BTL-64-BLK", "units": big_units, "revenue": big_units * 39.99,
             "ad_spend": 5.0, "ad_sales": 40.0 if big_units else 0.0},
        ]
    return rows


def _past_actions(today: date) -> list[dict]:
    """Executed actions from earlier runs, for the chief of staff to evaluate."""
    def ago(n: int) -> str:
        return (today - timedelta(days=n)).isoformat()

    return [
        {"agent": "ads", "kind": "update_bid", "target": "kw3", "sku": "BTL-32-BLK",
         "before": json.dumps({"bid": 1.19, "keyword": "metal water bottle"}),
         "after": json.dumps({"bid": 0.95}), "reason": "ACoS 98% vs target 25%",
         "status": "executed", "created": ago(10), "executed_at": ago(10)},
        {"agent": "pricing", "kind": "update_price", "target": "LID-STRAW", "sku": "LID-STRAW",
         "before": json.dumps({"price": 13.99}), "after": json.dumps({"price": 12.99}),
         "reason": "overstock: speed up sell-through",
         "status": "executed", "created": ago(12), "executed_at": ago(12)},
    ]


def seed(wh: Warehouse, today: date | None = None) -> None:
    today = today or date.today()
    cols_p = ["sku", "asin", "title", "price", "landed_cost", "fees", "units_on_hand",
              "units_inbound", "daily_velocity", "lead_time_days", "buybox_price"]
    cols_k = ["keyword_id", "campaign_id", "ad_group_id", "sku", "keyword", "match_type",
              "bid", "clicks", "spend", "sales", "orders"]
    cols_s = ["campaign_id", "ad_group_id", "sku", "search_term", "match_type",
              "clicks", "spend", "sales", "orders"]
    cols_r = ["asin", "rating", "body", "created"]
    for table in ("search_terms", "reviews", "metrics_daily", "actions", "outcomes"):
        wh.execute(f"DELETE FROM {table}")
    wh.execute("DELETE FROM sqlite_sequence WHERE name='actions'")  # restart action IDs at 1
    wh.insert_many("metrics_daily", _history(today))
    wh.insert_many("actions", _past_actions(today))
    wh.insert_many(
        "products", [{**dict(zip(cols_p, r)), "product_type": "BOTTLE"} for r in PRODUCTS]
    )
    wh.insert_many("keywords", [dict(zip(cols_k, r)) for r in KEYWORDS])
    wh.insert_many("search_terms", [dict(zip(cols_s, r)) for r in SEARCH_TERMS])
    wh.insert_many("reviews", [dict(zip(cols_r, r)) for r in REVIEWS])
