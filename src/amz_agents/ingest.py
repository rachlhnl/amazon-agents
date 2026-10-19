"""Ingestion jobs: pull from Amazon and other channels into the warehouse.

Run nightly (cron, Airflow, a scheduled job) with `amz-agents ingest`. Agents are
only as good as the data they read, so run these before `amz-agents run`.

Endpoint paths, report types and field names follow the SP-API, Ads API v3,
Shopify Admin GraphQL and Meta Marketing API docs as of this writing. Check a
first run against Seller Central / the ad consoles before trusting the numbers.

Some sources have no usable API for this purpose, or need a vendor library;
load those from a CSV export with `import_csv` (`amz-agents import TABLE FILE`).
"""

from __future__ import annotations

import csv
import io
import os
import re
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .data.clients import AdsClient, MetaAdsClient, ShopifyClient, SPAPIClient
from .data.warehouse import Warehouse

VELOCITY_DAYS = 30
BACKFILL_DAYS = 60  # metrics_daily history, so the chief of staff has a baseline
ADS_REPORT_DAYS = 30  # trailing window for keyword and search-term stats
MAX_REPORT_DAYS = 30  # both report APIs cap the date range per request


def _chunks(start: date, end: date, size: int = MAX_REPORT_DAYS) -> list[tuple[date, date]]:
    out = []
    while start <= end:
        stop = min(start + timedelta(days=size - 1), end)
        out.append((start, stop))
        start = stop + timedelta(days=1)
    return out


# ------------------------------------------------------------------ Amazon


def ingest_products(wh: Warehouse, sp: SPAPIClient, seller_id: str) -> int:
    """products: sku, asin, title, product_type, price -> Listings Items API
    (searchListingsItems); fees -> Product Fees API; buybox_price -> Product Pricing
    getItemOffers. landed_cost and lead_time_days come from your own data
    (`ingest_costs_csv`); existing values are kept."""
    existing = {r["sku"]: r for r in wh.query("SELECT * FROM products")}
    rows = []
    for item in sp.paginate(
        f"/listings/2021-08-01/items/{seller_id}",
        key=lambda b: b.get("items", []),
        marketplaceIds=sp.marketplace_id, includedData="summaries,offers",
        pageSize=20, _token_param="pageToken",
    ):
        summary = (item.get("summaries") or [{}])[0]
        price = _offer_price(item)
        if price is None:
            continue  # inactive or no offer; nothing to manage
        sku, old = item["sku"], existing.get(item["sku"], {})
        rows.append({
            "sku": sku,
            "asin": summary.get("asin"),
            "title": summary.get("itemName"),
            "product_type": summary.get("productType"),
            "price": price,
            "fees": _fees(sp, sku, price),
            "buybox_price": _buybox(sp, summary.get("asin")) if summary.get("asin") else None,
            "landed_cost": old.get("landed_cost"),
            "lead_time_days": old.get("lead_time_days"),
            "units_on_hand": old.get("units_on_hand", 0),
            "units_inbound": old.get("units_inbound", 0),
            "daily_velocity": old.get("daily_velocity", 0),
        })
    wh.insert_many("products", rows)
    return len(rows)


def _offer_price(item: dict[str, Any]) -> float | None:
    for o in item.get("offers") or []:
        if o.get("offerType", "B2C") == "B2C" and o.get("price"):
            return float(o["price"]["amount"])
    return None


def _fees(sp: SPAPIClient, sku: str, price: float) -> float | None:
    body = {"FeesEstimateRequest": {
        "MarketplaceId": sp.marketplace_id, "IsAmazonFulfilled": True, "Identifier": sku,
        "PriceToEstimateFees": {"ListingPrice": {"CurrencyCode": "USD", "Amount": price}},
    }}
    r = sp.post(f"/products/fees/v0/listings/{sku}/feesEstimate", body)
    est = r["payload"]["FeesEstimateResult"].get("FeesEstimate")
    return float(est["TotalFeesEstimate"]["Amount"]) if est else None


def _buybox(sp: SPAPIClient, asin: str) -> float | None:
    r = sp.get(f"/products/pricing/v0/items/{asin}/offers",
               MarketplaceId=sp.marketplace_id, ItemCondition="New")
    for b in (r.get("payload") or {}).get("Summary", {}).get("BuyBoxPrices") or []:
        if b.get("condition", "New").lower() == "new":
            return float(b["LandedPrice"]["Amount"])
    return None


def ingest_inventory(wh: Warehouse, sp: SPAPIClient) -> int:
    """units_on_hand / units_inbound -> FBA Inventory API (getInventorySummaries).
    Run after ingest_metrics_daily: daily_velocity is the trailing 30-day average
    of metrics_daily."""
    n = 0
    for s in sp.paginate(
        "/fba/inventory/v1/summaries",
        key=lambda b: (b.get("payload") or {}).get("inventorySummaries", []),
        details="true", granularityType="Marketplace", granularityId=sp.marketplace_id,
        marketplaceIds=sp.marketplace_id,
    ):
        d = s.get("inventoryDetails") or {}
        inbound = sum(d.get(k, 0) or 0 for k in (
            "inboundWorkingQuantity", "inboundShippedQuantity", "inboundReceivingQuantity"))
        n += wh.conn.execute(
            "UPDATE products SET units_on_hand=?, units_inbound=? WHERE sku=?",
            (d.get("fulfillableQuantity", 0), inbound, s["sellerSku"]),
        ).rowcount
    wh.conn.commit()
    update_velocity(wh)
    return n


def update_velocity(wh: Warehouse, today: date | None = None) -> None:
    start = ((today or date.today()) - timedelta(days=VELOCITY_DAYS)).isoformat()
    wh.execute(
        "UPDATE products SET daily_velocity = COALESCE((SELECT SUM(units) * 1.0 / ? "
        "FROM metrics_daily m WHERE m.sku = products.sku AND m.date >= ?), 0)",
        (VELOCITY_DAYS, start),
    )


def ingest_metrics_daily(
    wh: Warehouse, sp: SPAPIClient, ads: AdsClient, days: int = BACKFILL_DAYS,
    today: date | None = None,
) -> int:
    """metrics_daily (date, sku, units, revenue, ad_spend, ad_sales).
    Units and revenue -> GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL (one report
    for the whole window, cancelled orders excluded). Ad spend/sales -> Ads Reporting v3
    spAdvertisedProduct, daily by advertisedSku (7-day attribution)."""
    today = today or date.today()
    start, end = today - timedelta(days=days), today - timedelta(days=1)
    by_day: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"units": 0, "revenue": 0.0, "ad_spend": 0.0, "ad_sales": 0.0}
    )

    for a, b in _chunks(start, end):
        raw = sp.report(
            "GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL",
            f"{a.isoformat()}T00:00:00Z", f"{b.isoformat()}T23:59:59Z",
        )
        # purchase-date is UTC; close enough for daily trends.
        for r in csv.DictReader(io.StringIO(_decode(raw)), delimiter="\t"):
            if r.get("order-status") == "Cancelled" or not r.get("sku"):
                continue
            m = by_day[(r["purchase-date"][:10], r["sku"])]
            m["units"] += int(r.get("quantity") or 0)
            m["revenue"] += float(r.get("item-price") or 0)  # blank while pending

        for r in ads.report(
            "spAdvertisedProduct", a.isoformat(), b.isoformat(),
            group_by=["advertiser"], columns=["date", "advertisedSku", "cost", "sales7d"],
            time_unit="DAILY",
        ):
            m = by_day[(r["date"], r["advertisedSku"])]
            m["ad_spend"] += float(r.get("cost") or 0)
            m["ad_sales"] += float(r.get("sales7d") or 0)

    wh.insert_many("metrics_daily", [
        {"date": d, "sku": sku, **{k: round(v, 2) for k, v in m.items()}}
        for (d, sku), m in sorted(by_day.items())
    ])
    return len(by_day)


def _decode(raw: bytes) -> str:
    # Flat-file reports are UTF-8 in most marketplaces, Cp1252 in some.
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


_MATCH = {"BROAD": "broad", "PHRASE": "phrase", "EXACT": "exact"}


def _match_type(v: str | None) -> str:
    return _MATCH.get((v or "").upper(), "auto")  # targeting expressions = auto/product


def ingest_ads(wh: Warehouse, ads: AdsClient, today: date | None = None) -> tuple[int, int]:
    """keywords and search_terms -> Ads Reporting v3, trailing 30 days: spTargeting
    (keyword stats + current bid) and spSearchTerm. SKU per ad group comes from
    spAdvertisedProduct (the SKU with the most spend, if an ad group has several)."""
    today = today or date.today()
    start = (today - timedelta(days=ADS_REPORT_DAYS)).isoformat()
    end = (today - timedelta(days=1)).isoformat()

    spend: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in ads.report("spAdvertisedProduct", start, end, ["advertiser"],
                        ["adGroupId", "advertisedSku", "cost"]):
        spend[str(r["adGroupId"])][r["advertisedSku"]] += float(r.get("cost") or 0)
    sku_for = {ag: max(skus, key=skus.get) for ag, skus in spend.items()}

    keywords = []
    for r in ads.report("spTargeting", start, end, ["targeting"], [
        "campaignId", "adGroupId", "keywordId", "keyword", "matchType", "keywordType",
        "keywordBid", "clicks", "cost", "sales7d", "purchases7d",
    ]):
        if (r.get("keywordType") or "").upper() not in _MATCH:
            continue  # product/auto targets aren't keywords
        keywords.append({
            "keyword_id": str(r["keywordId"]), "campaign_id": str(r["campaignId"]),
            "ad_group_id": str(r["adGroupId"]), "sku": sku_for.get(str(r["adGroupId"])),
            "keyword": r["keyword"], "match_type": _match_type(r.get("matchType")),
            "bid": float(r.get("keywordBid") or 0), "clicks": int(r.get("clicks") or 0),
            "spend": float(r.get("cost") or 0), "sales": float(r.get("sales7d") or 0),
            "orders": int(r.get("purchases7d") or 0),
        })

    terms = [{
        "campaign_id": str(r["campaignId"]), "ad_group_id": str(r["adGroupId"]),
        "sku": sku_for.get(str(r["adGroupId"])), "search_term": r["searchTerm"],
        "match_type": _match_type(r.get("matchType")), "clicks": int(r.get("clicks") or 0),
        "spend": float(r.get("cost") or 0), "sales": float(r.get("sales7d") or 0),
        "orders": int(r.get("purchases7d") or 0),
    } for r in ads.report("spSearchTerm", start, end, ["searchTerm"], [
        "campaignId", "adGroupId", "searchTerm", "matchType", "clicks", "cost",
        "sales7d", "purchases7d",
    ])]

    wh.execute("DELETE FROM keywords")
    wh.execute("DELETE FROM search_terms")  # no natural key; replace the window
    wh.insert_many("keywords", keywords)
    wh.insert_many("search_terms", terms)
    return len(keywords), len(terms)


def ingest_costs_csv(wh: Warehouse, path: str | Path) -> int:
    """landed_cost and lead_time_days from your ERP/COGS sheet: columns
    sku, landed_cost, lead_time_days."""
    n = 0
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            n += wh.conn.execute(
                "UPDATE products SET landed_cost=?, lead_time_days=? WHERE sku=?",
                (float(r["landed_cost"]), int(r["lead_time_days"]), r["sku"]),
            ).rowcount
    wh.conn.commit()
    return n


def ingest_reviews(wh: Warehouse) -> None:
    """reviews -> no general review API exists for sellers. Options: Customer Feedback API
    (brand-registered, topic-level), Brand Analytics, or a licensed data vendor's export
    (load with `amz-agents import reviews FILE`: asin, rating, body, created). Do not
    scrape Amazon in violation of its terms."""
    raise NotImplementedError(__doc__)


# ---------------------------------------------------------- outside Amazon

_VARIANTS = """query($after: String) { productVariants(first: 250, after: $after) {
  nodes { id sku price } pageInfo { hasNextPage endCursor } } }"""

_ORDERS = """query($after: String, $q: String) { orders(first: 100, after: $after, query: $q) {
  nodes { cancelledAt lineItems(first: 100) { nodes { sku quantity } } }
  pageInfo { hasNextPage endCursor } } }"""


def ingest_shopify(wh: Warehouse, shop: ShopifyClient, today: date | None = None) -> int:
    """channel_listings for channel 'shopify': variant price and trailing 30-day units.
    Per-unit fees = SHOPIFY_FEE_PCT x price + SHOPIFY_FEE_FIXED + SHOPIFY_FULFILLMENT_COST.
    SHOPIFY_FULFILLMENT=mcf if orders ship from FBA stock (Multi-Channel Fulfillment)."""
    today = today or date.today()
    pct = float(os.getenv("SHOPIFY_FEE_PCT", "0.029"))
    fixed = float(os.getenv("SHOPIFY_FEE_FIXED", "0.30"))
    ship = float(os.getenv("SHOPIFY_FULFILLMENT_COST", "0"))
    units: dict[str, int] = defaultdict(int)
    since = (today - timedelta(days=30)).isoformat()
    for o in shop.nodes(_ORDERS, "orders", {"q": f"created_at:>={since}"}):
        if o.get("cancelledAt"):
            continue
        for li in o["lineItems"]["nodes"]:
            if li.get("sku"):
                units[li["sku"]] += int(li["quantity"])
    rows = []
    for v in shop.nodes(_VARIANTS, "productVariants"):
        if not v.get("sku"):
            continue
        price = float(v["price"])
        rows.append({
            "channel": "shopify", "sku": v["sku"], "external_id": v["id"], "price": price,
            "fees": round(price * pct + fixed + ship, 2), "units_30d": units.get(v["sku"], 0),
            "fulfillment": os.getenv("SHOPIFY_FULFILLMENT", "own"), "listed": 1,
        })
    wh.insert_many("channel_listings", rows)
    return len(rows)


_TAG = re.compile(r"(\w+)=([\w.-]+)")


def ingest_meta(wh: Warehouse, meta: MetaAdsClient) -> int:
    """external_campaigns for Meta ad sets, trailing 30 days.

    Naming convention (no other way to know which SKU an ad set sells):
    put `sku=BTL-32-BLK dest=amazon` (or dest=shopify, ...) in the ad set name.
    Ad sets without a sku= tag are skipped.

    For dest=shopify, purchases and revenue come from the Meta pixel. For dest=amazon,
    Meta can't see the sale: orders_30d and sales_30d are left at their previous values
    for `ingest_amazon_attribution` (or a CSV import) to fill.
    """
    existing = {r["campaign_id"]: r for r in wh.query(
        "SELECT * FROM external_campaigns WHERE platform='meta'")}
    insights = {i["adset_id"]: i for i in meta.list(
        "insights", level="adset", date_preset="last_30d", limit=500,
        fields="adset_id,spend,clicks,actions,action_values",
    )}
    rows = []
    for a in meta.list("adsets", fields="id,name,status,daily_budget", limit=200):
        tags = dict(_TAG.findall(a["name"]))
        if "sku" not in tags:
            continue
        i, dest = insights.get(a["id"], {}), tags.get("dest", "amazon")
        old = existing.get(a["id"], {})
        if dest == "amazon":
            orders, sales = old.get("orders_30d") or 0, old.get("sales_30d") or 0.0
        else:
            orders = int(_action(i.get("actions"), "purchase"))
            sales = _action(i.get("action_values"), "purchase")
        rows.append({
            "campaign_id": a["id"], "platform": "meta", "name": a["name"], "sku": tags["sku"],
            "destination": dest, "status": "enabled" if a["status"] == "ACTIVE" else "paused",
            "daily_budget": int(a.get("daily_budget") or 0) / 100,
            "spend_30d": float(i.get("spend") or 0), "clicks_30d": int(i.get("clicks") or 0),
            "orders_30d": orders, "sales_30d": sales,
        })
    wh.insert_many("external_campaigns", rows)
    return len(rows)


def _action(items: list[dict] | None, kind: str) -> float:
    return sum(float(x["value"]) for x in items or [] if x.get("action_type") == kind)


def ingest_amazon_attribution(wh: Warehouse) -> None:
    """orders_30d / sales_30d for dest=amazon campaigns -> Amazon Attribution API
    (Ads API, /attribution/report, by publisher/campaign tag). Until implemented,
    export the Attribution report and load it with
    `amz-agents import external_campaigns FILE` (campaign_id, orders_30d, sales_30d)."""
    raise NotImplementedError(__doc__)


def ingest_google_ads(wh: Warehouse) -> None:
    """external_campaigns for Google -> Google Ads API (GAQL over campaign metrics; needs
    the google-ads library and a developer token). Until implemented, export a campaign
    report and load it with `amz-agents import external_campaigns FILE`."""
    raise NotImplementedError(__doc__)


def ingest_leads(wh: Warehouse) -> None:
    """leads -> your CRM (HubSpot deals, Pipedrive, a sheet). Load an export with
    `amz-agents import leads FILE`: lead_id, company, contact, email, stage,
    est_annual_value, last_contact (YYYY-MM-DD), sku_interest, notes."""
    raise NotImplementedError(__doc__)


# ------------------------------------------------------------ CSV imports

IMPORTABLE = {"products", "reviews", "channel_listings", "external_campaigns", "leads",
              "metrics_daily"}


def import_csv(wh: Warehouse, table: str, path: str | Path) -> int:
    """Upsert rows from a CSV whose header names the table's columns. Rows that match an
    existing primary key update only the columns present in the file."""
    if table not in IMPORTABLE:
        raise ValueError(f"can't import into {table}; choose from {sorted(IMPORTABLE)}")
    info = wh.query(f"PRAGMA table_info({table})")
    cols = {c["name"] for c in info}
    pk = [c["name"] for c in sorted(info, key=lambda c: c["pk"]) if c["pk"]]
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        unknown = set(reader.fieldnames or []) - cols
        if unknown:
            raise ValueError(f"unknown columns for {table}: {sorted(unknown)}")
        rows = [{k: (v if v != "" else None) for k, v in r.items()} for r in reader]
    if not rows:
        return 0
    if pk and set(pk) <= set(rows[0]):
        fields = list(rows[0])
        updates = [c for c in fields if c not in pk] or fields[:1]
        sql = (f"INSERT INTO {table} ({','.join(fields)}) VALUES ({','.join('?' * len(fields))}) "
               f"ON CONFLICT({','.join(pk)}) DO UPDATE SET "
               + ",".join(f"{c}=excluded.{c}" for c in updates))
        wh.conn.executemany(sql, [tuple(r[c] for c in fields) for r in rows])
        wh.conn.commit()
    else:
        wh.insert_many(table, rows)
    return len(rows)


def run_all(wh: Warehouse, only: set[str] | None = None) -> dict[str, Any]:
    """Run every job whose credentials are configured. Returns {job: result or error}."""
    results: dict[str, Any] = {}

    def want(name: str) -> bool:
        return not only or name in only

    def run(name: str, fn: Any) -> None:
        try:
            results[name] = fn()
        except Exception as e:  # noqa: BLE001 - report and continue with other jobs
            results[name] = f"failed: {e!r}"

    if os.getenv("SP_API_REFRESH_TOKEN") and os.getenv("ADS_API_REFRESH_TOKEN"):
        sp, ads = SPAPIClient.from_env(), AdsClient.from_env()
        seller = os.getenv("SP_API_SELLER_ID", "")
        if want("products"):
            run("products", lambda: ingest_products(wh, sp, seller))
        if want("metrics"):
            run("metrics", lambda: ingest_metrics_daily(wh, sp, ads))
        if want("inventory"):
            run("inventory", lambda: ingest_inventory(wh, sp))
        if want("ads"):
            run("ads", lambda: ingest_ads(wh, ads))
    if os.getenv("COSTS_CSV") and want("costs"):
        run("costs", lambda: ingest_costs_csv(wh, os.environ["COSTS_CSV"]))
    if os.getenv("SHOPIFY_ACCESS_TOKEN") and want("shopify"):
        run("shopify", lambda: ingest_shopify(wh, ShopifyClient.from_env()))
    if os.getenv("META_ACCESS_TOKEN") and want("meta"):
        run("meta", lambda: ingest_meta(wh, MetaAdsClient.from_env()))
    results["finished"] = datetime.now(UTC).isoformat(timespec="seconds")
    return results
