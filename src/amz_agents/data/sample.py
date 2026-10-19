"""Synthetic sample data so the suite runs end-to-end without credentials."""

from __future__ import annotations

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


def seed(wh: Warehouse) -> None:
    cols_p = ["sku", "asin", "title", "price", "landed_cost", "fees", "units_on_hand",
              "units_inbound", "daily_velocity", "lead_time_days", "buybox_price"]
    cols_k = ["keyword_id", "campaign_id", "ad_group_id", "sku", "keyword", "match_type",
              "bid", "clicks", "spend", "sales", "orders"]
    cols_s = ["campaign_id", "ad_group_id", "sku", "search_term", "match_type",
              "clicks", "spend", "sales", "orders"]
    cols_r = ["asin", "rating", "body", "created"]
    wh.execute("DELETE FROM search_terms")
    wh.execute("DELETE FROM reviews")
    wh.insert_many(
        "products", [{**dict(zip(cols_p, r)), "product_type": "BOTTLE"} for r in PRODUCTS]
    )
    wh.insert_many("keywords", [dict(zip(cols_k, r)) for r in KEYWORDS])
    wh.insert_many("search_terms", [dict(zip(cols_s, r)) for r in SEARCH_TERMS])
    wh.insert_many("reviews", [dict(zip(cols_r, r)) for r in REVIEWS])
