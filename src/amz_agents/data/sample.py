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
    ("BTL-64-BLK", "B0SAMPLE04", "Insulated Water Bottle 64oz Black - The BEST Leakproof Jug", 39.99, 10.20, 11.80, 60, 300, 4.0, 45, 36.00),
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

# ---- Outside Amazon ----

CHANNEL_LISTINGS = [
    # channel, sku, external_id, price, fees, units_30d, fulfillment, listed
    ("shopify", "BTL-32-BLK", "gid://shopify/ProductVariant/1001", 27.99, 5.50, 160, "mcf", 1),
    ("shopify", "BTL-32-SGE", "gid://shopify/ProductVariant/1002", 29.99, 5.50, 45, "mcf", 1),
    ("shopify", "LID-STRAW", "gid://shopify/ProductVariant/1003", 9.99, 4.00, 30, "mcf", 1),
    ("walmart", "BTL-32-BLK", "WM-88213", 30.99, 6.50, 70, "own", 1),
]

EXTERNAL_CAMPAIGNS = [
    # id, platform, name, sku, destination, status, budget, spend, clicks, orders, sales
    ("m1", "meta", "Black bottle - prospecting", "BTL-32-BLK", "amazon", "enabled",
     40.0, 1200.0, 2400, 70, 2099.3),
    ("g1", "google", "Black bottle - search", "BTL-32-BLK", "shopify", "enabled",
     25.0, 600.0, 900, 60, 1679.4),
    ("m2", "meta", "Sage launch", "BTL-32-SGE", "amazon", "enabled",
     20.0, 450.0, 800, 25, 749.75),
    ("t1", "tiktok", "Black bottle - creators", "BTL-32-BLK", "shopify", "enabled",
     10.0, 300.0, 400, 0, 0.0),
]

# lead_id, company, contact, email, stage, est_annual_value, days since contact, skus, notes
LEADS = [
    ("L1", "Peak Outfitters", "Dana Ruiz", "dana@peakoutfitters.example", "negotiating", 48000,
     5, "BTL-32-SGE,BTL-32-BLK", "Wants 600 units/quarter, asked for net-30 terms"),
    ("L2", "Campus Store Co", "Sam Lee", "sam@campusstore.example", "contacted", 15000,
     3, "BTL-32-BLK", "Sent line sheet"),
    ("L3", "Trailhead Gyms", "Alex Kim", "alex@trailheadgyms.example", "new", 12000,
     None, "BTL-64-BLK", "Inbound form: gym merch"),
    ("L4", "Lakeside Gifts", "Jo Park", "jo@lakesidegifts.example", "won", 9000,
     20, "LID-STRAW", "Reorders quarterly"),
]

# ---- Creative and UX ----

STORE = "https://yourstore.example"

CREATIVES = [
    # ad_id, platform, campaign, name, sku, landing, imp7, clk7, spend7, freq7, buy7,
    # imp_prior, clk_prior, spend_prior
    ("m1-a1", "meta", "m1", "Cold for 24h - video", "BTL-32-BLK",
     "https://www.amazon.com/dp/B0SAMPLE01", 30000, 210, 120.0, 3.4, 6, 70000, 840, 260.0),
    ("m1-a2", "meta", "m1", "Fits the gym bag - static", "BTL-32-BLK",
     "https://www.amazon.com/dp/B0SAMPLE01", 20000, 330, 80.0, 1.8, 9, 50000, 700, 200.0),
    ("m1-a3", "meta", "m1", "20% off banner", "BTL-32-BLK",
     "https://www.amazon.com/dp/B0SAMPLE01", 15000, 45, 40.0, 2.0, 0, 30000, 100, 90.0),
    ("g1-a1", "google", "g1", "Search - insulated bottle", "BTL-32-BLK",
     f"{STORE}/products/insulated-bottle-32oz?utm_source=google", 9000, 400, 140.0, 1.0, 14,
     27000, 1150, 420.0),
    ("t1-a1", "tiktok", "t1", "Creator unboxing", "BTL-32-BLK",
     f"{STORE}/pages/creators?utm_source=tiktok", 40000, 380, 70.0, 1.6, 0,
     80000, 720, 160.0),
]

LISTING_ASSETS = [
    # sku, images, video, A+, bullets, title length
    ("BTL-32-BLK", 9, 1, 1, 5, 33),
    ("BTL-32-SGE", 5, 0, 1, 5, 32),
    ("LID-STRAW", 4, 0, 0, 3, 21),
    ("BTL-64-BLK", 7, None, 1, 5, 58),
]

ASIN_TRAFFIC = [
    # sku, sessions, page views, units, Buy Box share
    ("BTL-32-BLK", 4200, 6100, 540, 0.95),
    ("BTL-32-SGE", 2400, 3300, 330, 1.00),
    ("LID-STRAW", 1500, 1900, 195, 0.98),
    ("BTL-64-BLK", 2000, 2600, 120, 0.55),
]

WEB_PAGES = [
    # url, type, sku, perf mobile, perf desktop, a11y, LCP ms, CLS, sessions, purchases
    (f"{STORE}", "home", None, 38, 82, 74, 5200, 0.05, 3000, 45),
    (f"{STORE}/products/insulated-bottle-32oz", "product", "BTL-32-BLK", 71, 93, 92, 2600, 0.04,
     2200, 60),
    (f"{STORE}/pages/creators", "landing", "BTL-32-BLK", 55, 88, 88, 3800, 0.31, 400, 0),
]


def _web_funnel(today: date) -> list[dict]:
    """28 days by device. Mobile converts at a fraction of desktop, and mobile
    checkout -> purchase broke two weeks ago."""
    rows = []
    for ago in range(28, 0, -1):
        d = (today - timedelta(days=ago)).isoformat()
        mobile_buys = 4 if ago <= 14 else 8
        rows += [
            {"date": d, "device": "mobile", "sessions": 600, "add_to_cart": 27, "checkout": 11,
             "purchases": mobile_buys, "revenue": mobile_buys * 31.0},
            {"date": d, "device": "desktop", "sessions": 250, "add_to_cart": 20, "checkout": 11,
             "purchases": 7, "revenue": 7 * 31.0},
        ]
    return rows


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
             "ad_spend": 0.0, "ad_sales": 0.0},  # not advertised: a data-agent opportunity
            {"date": d, "sku": "BTL-64-BLK", "units": big_units, "revenue": big_units * 39.99,
             "ad_spend": 5.0, "ad_sales": 40.0 if big_units else 0.0},
        ]
    return rows


def _past_actions(today: date) -> list[dict]:
    """Actions from earlier runs: two executed ones for the chief of staff to judge,
    and work at other stages for the tracker."""
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
        # Work at other stages, so the tracker has history to show.
        {"agent": "ux", "kind": "improve_page_speed",
         "target": "https://yourstore.example/products/insulated-bottle-32oz",
         "sku": "BTL-32-BLK", "team": "ux", "effort": "M", "priority": "medium",
         "before": json.dumps({"perf_mobile": 41}),
         "after": json.dumps({"title": "Speed up product /products/insulated-bottle-32oz",
                              "impact_usd": 180.0}),
         "reason": "mobile performance 41/100", "status": "done", "created": ago(14),
         "done_at": ago(6)},
        {"agent": "listing", "kind": "update_listing_copy", "target": "BTL-32-SGE",
         "sku": "BTL-32-SGE", "team": "amazon_ops", "effort": "M", "priority": "judgment",
         "before": json.dumps({"title": "Insulated Water Bottle 32oz Sage"}),
         "after": json.dumps({"missing_terms": ["gym"], "review_themes": [], "draft": None}),
         "reason": "1 converting search-term word absent from title",
         "status": "acknowledged", "created": ago(5)},
        {"agent": "sales", "kind": "send_outreach", "target": "L4", "sku": "LID-STRAW",
         "team": "sales", "effort": "S", "priority": "judgment",
         "before": json.dumps({"stage": "negotiating"}),
         "after": json.dumps({"company": "Lakeside Gifts", "contact": "Jo Park",
                              "est_annual_value": 9000, "draft": None}),
         "reason": "Lakeside Gifts (negotiating): no contact in 4 days",
         "status": "done", "created": ago(21), "done_at": ago(20)},
        {"agent": "ads", "kind": "add_negative", "target": "c1/ag1", "sku": "BTL-32-BLK",
         "team": "amazon_ops", "effort": "XS", "priority": "medium", "impact_usd": 18.0,
         "before": "null", "after": json.dumps({"keyword": "cheap water bottle",
                                                 "match_type": "negativeExact"}),
         "reason": "'cheap water bottle': 22 clicks, $18.00 spend, 0 orders",
         "status": "rejected", "created": ago(6)},
        {"agent": "marketing", "kind": "update_campaign_budget", "target": "m2",
         "sku": "BTL-32-SGE", "team": "marketing", "effort": "XS", "priority": "medium",
         "impact_usd": 40.0,
         "before": json.dumps({"daily_budget": 16.0, "name": "Sage launch", "platform": "meta",
                               "destination": "amazon", "profit_per_dollar": 1.6}),
         "after": json.dumps({"daily_budget": 19.2}),
         "reason": "$1.60 profit per $1 spent over 30 days (scale)",
         "status": "failed", "notes": "no API executor for meta: META_ACCESS_TOKEN not set",
         "created": ago(9)},
    ]


# What the market tracker would report for the sample brand.
MARKET_RECS = [
    (1, "Fit & returns", "Low", "Straw Lid: buyers complain about leaks",
     "5 of 7 1-2\u2605 reviews of the Straw Lid mention leaking.",
     "A leak is a return and a 1\u2605 review; fixing the seal protects the bottle's rating too.",
     ["Pull the latest lid batch and test the seal.", "Add a fit note to the listing."],
     "Share of 1-2\u2605 reviews about leaks", "71%", "\u2264 30%"),
    (3, "Product", "High", "No new colors in 90 days",
     "0 new colors in 90 days; similar brands launched a median of 3.",
     "New colors bring back past buyers and give ads something new to say.",
     ["Pick 2 colors from the best-selling family.", "Plan a launch email and ad set."],
     "New colors in 90 days", "0", "\u2265 2"),
    (2, "Marketing", "Low", "The best-rated bottles are ready to be pushed",
     "The 32oz Black and Sage rate 4.6\u2605+ on 200+ reviews.",
     "Proven products convert paid traffic best.",
     ["Feature them in the next email.", "Give them their own ad set."],
     "Share of ad spend on 4.5\u2605+ products", "40%", "\u2265 60%"),
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
    for table in ("search_terms", "reviews", "metrics_daily", "actions", "outcomes",
                  "channel_listings", "external_campaigns", "leads", "creatives",
                  "listing_assets", "asin_traffic", "web_funnel", "web_pages", "runs",
                  "agent_runs"):
        wh.execute(f"DELETE FROM {table}")
    wh.execute("DELETE FROM sqlite_sequence WHERE name='actions'")  # restart action IDs at 1
    wh.insert_many("metrics_daily", _history(today))
    for row in _past_actions(today):  # rows carry different columns
        wh.insert_many("actions", [row])
    wh.insert_many(
        "products", [{**dict(zip(cols_p, r)), "product_type": "BOTTLE"} for r in PRODUCTS]
    )
    wh.insert_many("keywords", [dict(zip(cols_k, r)) for r in KEYWORDS])
    wh.insert_many("search_terms", [dict(zip(cols_s, r)) for r in SEARCH_TERMS])
    wh.insert_many("reviews", [dict(zip(cols_r, r)) for r in REVIEWS])

    cols_c = ["channel", "sku", "external_id", "price", "fees", "units_30d", "fulfillment", "listed"]
    cols_e = ["campaign_id", "platform", "name", "sku", "destination", "status", "daily_budget",
              "spend_30d", "clicks_30d", "orders_30d", "sales_30d"]
    wh.insert_many("channel_listings", [dict(zip(cols_c, r)) for r in CHANNEL_LISTINGS])
    wh.insert_many("external_campaigns", [dict(zip(cols_e, r)) for r in EXTERNAL_CAMPAIGNS])
    wh.insert_many("leads", [
        {"lead_id": i, "company": co, "contact": n, "email": e, "stage": st,
         "est_annual_value": v, "sku_interest": s, "notes": nt,
         "last_contact": (today - timedelta(days=d)).isoformat() if d is not None else None}
        for i, co, n, e, st, v, d, s, nt in LEADS
    ])

    def rows(cols: str, data: list[tuple]) -> list[dict]:
        return [dict(zip(cols.split(), r)) for r in data]

    wh.insert_many("creatives", [{**r, "status": "enabled"} for r in rows(
        "ad_id platform campaign_id name sku landing_url impressions_7d clicks_7d spend_7d "
        "frequency_7d purchases_7d impressions_prior clicks_prior spend_prior", CREATIVES)])
    wh.insert_many("listing_assets", rows(
        "sku image_count has_video has_aplus bullet_count title_length", LISTING_ASSETS))
    wh.insert_many("asin_traffic", rows(
        "sku sessions_30d page_views_30d units_30d buy_box_pct", ASIN_TRAFFIC))
    wh.insert_many("web_pages", [{**r, "checked": today.isoformat()} for r in rows(
        "url page_type sku perf_mobile perf_desktop accessibility lcp_ms_mobile cls_mobile "
        "sessions_30d purchases_30d", WEB_PAGES)])
    wh.insert_many("web_funnel", _web_funnel(today))
    wh.insert_many("market_recs", [
        {"date": today.isoformat(), "rank": rank, "area": area, "effort": effort, "title": t,
         "see": see, "why": why, "actions": json.dumps(steps), "kpi": kpi, "kpi_now": now,
         "kpi_target": target, "url": None}
        for rank, area, effort, t, see, why, steps, kpi, now, target in MARKET_RECS])
