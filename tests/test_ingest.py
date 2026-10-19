import gzip
import json
from datetime import date

import httpx
import pytest

from amz_agents.data.clients import (
    AdsClient,
    LWAToken,
    MetaAdsClient,
    ReportFailed,
    ShopifyClient,
    SPAPIClient,
)
from amz_agents.data.warehouse import Warehouse
from amz_agents.ingest import (
    _chunks,
    import_csv,
    ingest_ads,
    ingest_costs_csv,
    ingest_inventory,
    ingest_meta,
    ingest_metrics_daily,
    ingest_products,
    ingest_shopify,
)

TODAY = date(2026, 10, 19)

ORDERS_TSV = (
    "amazon-order-id\tpurchase-date\torder-status\tsku\tasin\tquantity\titem-price\n"
    "1\t2026-10-17T15:00:00+00:00\tShipped\tSKU1\tA1\t2\t59.98\n"
    "2\t2026-10-17T18:00:00+00:00\tShipped\tSKU1\tA1\t1\t29.99\n"
    "3\t2026-10-18T09:00:00+00:00\tCancelled\tSKU1\tA1\t5\t149.95\n"
    "4\t2026-10-18T10:00:00+00:00\tPending\tSKU2\tA2\t1\t\n"
)


class FakeAmazon:
    """Routes SP-API and Ads API calls; records what was asked."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.polls = 0
        self.reports: dict[str, dict] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        if req.url.host == "api.amazon.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if req.url.host == "files.example":
            body = self.reports[path.strip("/")]
            return httpx.Response(200, content=body["content"])

        # --- SP-API
        if path == "/reports/2021-06-30/reports" and req.method == "POST":
            rid = f"r{len(self.reports)}"
            self.reports[rid] = {"content": gzip.compress(ORDERS_TSV.encode())}
            return httpx.Response(202, json={"reportId": rid})
        if path.startswith("/reports/2021-06-30/reports/"):
            self.polls += 1
            rid = path.rsplit("/", 1)[1]
            done = self.polls % 2 == 0  # first poll IN_PROGRESS, then DONE
            return httpx.Response(200, json={
                "processingStatus": "DONE" if done else "IN_PROGRESS",
                **({"reportDocumentId": rid} if done else {}),
            })
        if path.startswith("/reports/2021-06-30/documents/"):
            rid = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"url": f"https://files.example/{rid}",
                                             "compressionAlgorithm": "GZIP"})
        if path.startswith("/listings/2021-08-01/items/"):
            page2 = req.url.params.get("pageToken") == "p2"
            items = [{"sku": "SKU2", "summaries": [{"asin": "A2", "itemName": "Two",
                                                     "productType": "BOTTLE"}],
                      "offers": [{"offerType": "B2C", "price": {"amount": "19.99"}}]}] if page2 \
                else [{"sku": "SKU1", "summaries": [{"asin": "A1", "itemName": "One",
                                                      "productType": "BOTTLE"}],
                       "offers": [{"offerType": "B2C", "price": {"amount": "29.99"}}]},
                      {"sku": "DEAD", "summaries": [{"asin": "A9"}], "offers": []}]
            return httpx.Response(200, json={
                "items": items, "pagination": {} if page2 else {"nextToken": "p2"}})
        if "/feesEstimate" in path:
            return httpx.Response(200, json={"payload": {"FeesEstimateResult": {
                "FeesEstimate": {"TotalFeesEstimate": {"Amount": 9.4}}}}})
        if path.startswith("/products/pricing/v0/items/"):
            return httpx.Response(200, json={"payload": {"Summary": {"BuyBoxPrices": [
                {"condition": "New", "LandedPrice": {"Amount": 28.5}}]}}})
        if path == "/fba/inventory/v1/summaries":
            return httpx.Response(200, json={"payload": {"inventorySummaries": [
                {"sellerSku": "SKU1", "inventoryDetails": {
                    "fulfillableQuantity": 100, "inboundWorkingQuantity": 10,
                    "inboundShippedQuantity": 20, "inboundReceivingQuantity": 5}}]}})

        # --- Ads API
        if path == "/reporting/reports" and req.method == "POST":
            cfg = json.loads(req.content)["configuration"]
            rid = f"ads{len(self.reports)}"
            self.reports[rid] = {"content": gzip.compress(json.dumps(
                ADS_ROWS[cfg["reportTypeId"]]).encode()), "cfg": cfg}
            return httpx.Response(200, json={"reportId": rid, "status": "PENDING"})
        if path.startswith("/reporting/reports/"):
            rid = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"status": "COMPLETED",
                                             "url": f"https://files.example/{rid}"})
        return httpx.Response(404, json={"path": path})


ADS_ROWS = {
    "spAdvertisedProduct": [
        {"date": "2026-10-17", "adGroupId": 11, "advertisedSku": "SKU1", "cost": 5.0,
         "sales7d": 30.0},
        {"date": "2026-10-18", "adGroupId": 11, "advertisedSku": "SKU1", "cost": 4.0,
         "sales7d": 0},
        {"date": "2026-10-18", "adGroupId": 11, "advertisedSku": "SKU2", "cost": 1.0,
         "sales7d": 0},
    ],
    "spTargeting": [
        {"campaignId": 1, "adGroupId": 11, "keywordId": 101, "keyword": "water bottle",
         "matchType": "EXACT", "keywordType": "EXACT", "keywordBid": 1.2, "clicks": 50,
         "cost": 40.0, "sales7d": 120.0, "purchases7d": 4},
        {"campaignId": 1, "adGroupId": 11, "keywordId": 102, "keyword": "asin=B0X",
         "matchType": "TARGETING_EXPRESSION", "keywordType": "TARGETING_EXPRESSION",
         "keywordBid": 0.5, "clicks": 5, "cost": 2.0, "sales7d": 0, "purchases7d": 0},
    ],
    "spSearchTerm": [
        {"campaignId": 1, "adGroupId": 11, "searchTerm": "steel water bottle",
         "matchType": "BROAD", "clicks": 20, "cost": 15.0, "sales7d": 60.0, "purchases7d": 2},
        {"campaignId": 1, "adGroupId": 11, "searchTerm": "b0abc123",
         "matchType": "TARGETING_EXPRESSION_PREDEFINED", "clicks": 3, "cost": 1.0,
         "sales7d": 0, "purchases7d": 0},
    ],
}


@pytest.fixture
def amazon():
    fake = FakeAmazon()
    http = httpx.Client(transport=httpx.MockTransport(fake))
    tok = LWAToken("id", "secret", "refresh")
    sp = SPAPIClient(tok, http=http)
    ads = AdsClient(LWAToken("id", "secret", "refresh"), profile_id="p1", http=http)
    for c in (sp, ads):
        c.sleep = lambda s: None
    return fake, sp, ads


@pytest.fixture
def wh():
    return Warehouse()


def test_chunks_cover_range_without_gaps():
    ch = _chunks(date(2026, 8, 20), date(2026, 10, 18))
    assert ch[0][0] == date(2026, 8, 20) and ch[-1][1] == date(2026, 10, 18)
    assert all((b - a).days < 30 for a, b in ch)
    assert all((ch[i + 1][0] - ch[i][1]).days == 1 for i in range(len(ch) - 1))


def test_products_paginates_and_keeps_own_costs(amazon, wh):
    _, sp, _ = amazon
    wh.insert_many("products", [{"sku": "SKU1", "landed_cost": 7.1, "lead_time_days": 45}])
    assert ingest_products(wh, sp, "SELLER") == 2  # DEAD has no offer
    p = {r["sku"]: r for r in wh.query("SELECT * FROM products")}
    assert p["SKU1"]["price"] == 29.99 and p["SKU1"]["fees"] == 9.4
    assert p["SKU1"]["buybox_price"] == 28.5 and p["SKU1"]["landed_cost"] == 7.1
    assert p["SKU2"]["title"] == "Two" and p["SKU2"]["landed_cost"] is None


def test_metrics_daily_from_orders_and_ads(amazon, wh):
    fake, sp, ads = amazon
    ingest_metrics_daily(wh, sp, ads, days=10, today=TODAY)
    m = {(r["date"], r["sku"]): r for r in wh.query("SELECT * FROM metrics_daily")}
    assert m[("2026-10-17", "SKU1")]["units"] == 3
    assert m[("2026-10-17", "SKU1")]["revenue"] == 89.97
    assert m[("2026-10-17", "SKU1")]["ad_spend"] == 5.0
    assert m[("2026-10-18", "SKU1")]["units"] == 0  # cancelled order excluded
    assert m[("2026-10-18", "SKU1")]["ad_spend"] == 4.0
    assert m[("2026-10-18", "SKU2")]["units"] == 1  # pending: units count, no price yet
    assert fake.polls >= 2  # waited for the report


def test_long_backfill_is_split_into_report_sized_chunks(amazon, wh):
    fake, sp, ads = amazon
    ingest_metrics_daily(wh, sp, ads, days=60, today=TODAY)
    sp_reports = [r for r in fake.requests
                  if r.url.path == "/reports/2021-06-30/reports" and r.method == "POST"]
    assert len(sp_reports) == 2


def test_inventory_and_velocity(amazon, wh):
    _, sp, ads = amazon
    wh.insert_many("products", [{"sku": "SKU1", "units_on_hand": 0, "units_inbound": 0}])
    ingest_metrics_daily(wh, sp, ads, days=10, today=date.today())
    ingest_inventory(wh, sp)
    p = wh.query("SELECT * FROM products WHERE sku='SKU1'")[0]
    assert p["units_on_hand"] == 100 and p["units_inbound"] == 35
    assert p["daily_velocity"] == pytest.approx(3 / 30)


def test_ads_keywords_and_search_terms(amazon, wh):
    fake, _, ads = amazon
    assert ingest_ads(wh, ads, today=TODAY) == (1, 2)  # product target skipped
    k = wh.query("SELECT * FROM keywords")[0]
    assert k["keyword_id"] == "101" and k["sku"] == "SKU1" and k["match_type"] == "exact"
    assert k["bid"] == 1.2 and k["orders"] == 4
    terms = {t["search_term"]: t for t in wh.query("SELECT * FROM search_terms")}
    assert terms["steel water bottle"]["match_type"] == "broad"
    assert terms["b0abc123"]["match_type"] == "auto"
    ads_post = next(r for r in fake.requests if r.url.path == "/reporting/reports")
    assert r"vnd.createasyncreportrequest.v3+json" in ads_post.headers["content-type"]
    assert ads_post.headers["amazon-advertising-api-scope"] == "p1"


def test_failed_report_raises(amazon):
    _, sp, _ = amazon
    sp.get = lambda path, **kw: (
        {"processingStatus": "FATAL"} if "/reports/" in path else {})
    sp.post = lambda path, body, **kw: {"reportId": "x"}
    with pytest.raises(ReportFailed):
        sp.report("GET_X", "a", "b")


def test_costs_csv(wh, tmp_path):
    wh.insert_many("products", [{"sku": "SKU1"}])
    f = tmp_path / "costs.csv"
    f.write_text("sku,landed_cost,lead_time_days\nSKU1,7.10,45\nNOPE,1,1\n")
    assert ingest_costs_csv(wh, f) == 1
    assert wh.query("SELECT landed_cost FROM products")[0]["landed_cost"] == 7.1


def test_import_csv_upserts_only_given_columns(wh, tmp_path):
    wh.insert_many("external_campaigns", [{"campaign_id": "m1", "platform": "meta",
                                           "spend_30d": 100.0, "orders_30d": 0}])
    f = tmp_path / "attribution.csv"
    f.write_text("campaign_id,orders_30d,sales_30d\nm1,7,209.93\n")
    assert import_csv(wh, "external_campaigns", f) == 1
    c = wh.query("SELECT * FROM external_campaigns")[0]
    assert (c["platform"], c["spend_30d"], c["orders_30d"]) == ("meta", 100.0, 7)


def test_import_csv_rejects_unknown_table_and_columns(wh, tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("lead_id,favorite_color\nL1,blue\n")
    with pytest.raises(ValueError):
        import_csv(wh, "actions", f)
    with pytest.raises(ValueError):
        import_csv(wh, "leads", f)


# ----------------------------------------------------------- outside Amazon


def test_shopify_prices_units_and_fees(wh, monkeypatch):
    monkeypatch.setenv("SHOPIFY_FULFILLMENT", "mcf")
    monkeypatch.setenv("SHOPIFY_FULFILLMENT_COST", "2.00")

    def handler(req: httpx.Request) -> httpx.Response:
        q = json.loads(req.content)
        if "orders(" in q["query"]:
            assert q["variables"]["q"] == "created_at:>=2026-09-19"
            nodes = [
                {"cancelledAt": None, "lineItems": {"nodes": [{"sku": "SKU1", "quantity": 2}]}},
                {"cancelledAt": "2026-10-01", "lineItems": {"nodes": [
                    {"sku": "SKU1", "quantity": 9}]}},
            ]
            return httpx.Response(200, json={"data": {"orders": {
                "nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}})
        page2 = q["variables"].get("after") == "c1"
        nodes = [{"id": "gid://v/2", "sku": "SKU2", "price": "9.99"}] if page2 else \
            [{"id": "gid://v/1", "sku": "SKU1", "price": "27.99"}, {"id": "gid://v/x", "sku": ""}]
        return httpx.Response(200, json={"data": {"productVariants": {
            "nodes": nodes, "pageInfo": {"hasNextPage": not page2, "endCursor": "c1"}}}})

    shop = ShopifyClient("x.myshopify.com", "tok",
                         http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert ingest_shopify(wh, shop, today=TODAY) == 2
    rows = {r["sku"]: r for r in wh.query("SELECT * FROM channel_listings")}
    assert rows["SKU1"]["units_30d"] == 2 and rows["SKU1"]["fulfillment"] == "mcf"
    assert rows["SKU1"]["fees"] == round(27.99 * 0.029 + 0.30 + 2.00, 2)
    assert rows["SKU2"]["external_id"] == "gid://v/2"


def test_meta_adsets_by_naming_convention(wh):
    wh.insert_many("external_campaigns", [{"campaign_id": "as1", "platform": "meta",
                                           "orders_30d": 12, "sales_30d": 359.88}])

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["access_token"] == "tok"
        if req.url.path.endswith("/insights"):
            return httpx.Response(200, json={"data": [
                {"adset_id": "as1", "spend": "300.5", "clicks": "800"},
                {"adset_id": "as2", "spend": "100", "clicks": "200",
                 "actions": [{"action_type": "purchase", "value": "5"}],
                 "action_values": [{"action_type": "purchase", "value": "139.95"}]},
            ]})
        return httpx.Response(200, json={"data": [
            {"id": "as1", "name": "Prospecting sku=BTL-32-BLK dest=amazon", "status": "ACTIVE",
             "daily_budget": "4000"},
            {"id": "as2", "name": "Retargeting sku=BTL-32-BLK dest=shopify",
             "status": "PAUSED", "daily_budget": "1500"},
            {"id": "as3", "name": "Brand awareness", "status": "ACTIVE", "daily_budget": "900"},
        ]})

    meta = MetaAdsClient("123", "tok", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert ingest_meta(wh, meta) == 2
    c = {r["campaign_id"]: r for r in wh.query("SELECT * FROM external_campaigns")}
    assert c["as1"]["daily_budget"] == 40.0 and c["as1"]["spend_30d"] == 300.5
    assert c["as1"]["orders_30d"] == 12  # Amazon sales kept for Attribution to fill
    assert c["as2"]["status"] == "paused" and c["as2"]["orders_30d"] == 5
    assert c["as2"]["sales_30d"] == 139.95 and c["as2"]["destination"] == "shopify"
