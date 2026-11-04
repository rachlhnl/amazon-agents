"""Data sources for the Dagacci KPI pipeline.

Both sources return the same normalized tables (dict of lists of dicts), so the
KPI layer never cares where the data came from:

    customer_trends  weekly brand totals (sales, orders, new-to-brand, repeat)
    sqp              Search Query Performance, one row per query per week
    repeat_asin      Repeat Purchase Behavior, one row per ASIN per week
    basket           Market Basket, one row per ASIN per week (top-3 combos)
    listing_health   Voice of the Customer listing summary, one row per SKU

Tables a source can't provide yet are simply absent; the dashboard shows them
as "awaiting connection".
"""
import csv
import datetime as dt
import glob
import gzip
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request


def _f(v):
    """Parse numbers from Amazon CSVs ('12.3%', '1,234', '', '--', 'Not available')."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def _i(v):
    x = _f(v)
    return int(x) if x is not None else 0


def _read_report(path):
    """Brand Analytics CSVs have a one-line filter header before the column row."""
    with open(path, encoding="utf-8-sig") as fh:
        first = fh.readline()
        if "=[" not in first:
            fh.seek(0)
        return list(csv.DictReader(fh))


def _week_from_name(path):
    m = re.search(r"Week_(\d{4})_(\d{2})_(\d{2})", path)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


# --------------------------------------------------------------------------- CSV

class CsvSource:
    name = "csv"

    def __init__(self, folder):
        self.folder = folder

    def _files(self, pattern):
        return sorted(glob.glob(os.path.join(self.folder, pattern)))

    def load(self):
        t = {}
        loaders = {
            "customer_trends": self._customer_trends,
            "sqp": self._sqp,
            "repeat_asin": self._repeat_asin,
            "basket": self._basket,
            "listing_health": self._listing_health,
        }
        for key, fn in loaders.items():
            rows = fn()
            if rows:
                t[key] = rows
        return t

    def _customer_trends(self):
        by_week = {}
        for p in self._files("Trends_*.csv"):  # later files overwrite overlapping weeks
            for r in _read_report(p):
                week = dt.datetime.strptime(r["Date"], "%B %d, %Y").date().isoformat()
                by_week[week] = {
                    "week_end": week,
                    "customers": _i(r["Total Customers"]),
                    "sales": _f(r["Total Sales"]),
                    "orders": _i(r["Total Orders"]),
                    "ntb_customers": _i(r["New to Brand Customers"]),
                    "ntb_sales": _f(r["New to Brand Customer Sales"]),
                    "ntb_orders": _i(r["New to Brand Customer Orders"]),
                    "repeat_customers": _i(r["Repeat Customers"]),
                    "repeat_sales": _f(r["Repeat Sales"]),
                    "repeat_orders": _i(r["Repeat Orders"]),
                    "repeat_rate": _f(r["Repeat Purchase Rate"]),
                }
        return [by_week[k] for k in sorted(by_week)]

    def _sqp(self):
        out = []
        for p in self._files("*Search_Query_Performance_Brand_View*.csv"):
            for r in _read_report(p):
                out.append({
                    "week_end": r.get("Reporting Date") or _week_from_name(p),
                    "query": r["Search Query"],
                    "rank": _i(r["Search Query Score"]),
                    "volume": _i(r["Search Query Volume"]),
                    "impr_total": _i(r["Impressions: Total Count"]),
                    "impr_brand": _i(r["Impressions: Brand Count"]),
                    "clicks_total": _i(r["Clicks: Total Count"]),
                    "clicks_brand": _i(r["Clicks: Brand Count"]),
                    "cart_total": _i(r["Cart Adds: Total Count"]),
                    "cart_brand": _i(r["Cart Adds: Brand Count"]),
                    "purch_total": _i(r["Purchases: Total Count"]),
                    "purch_brand": _i(r["Purchases: Brand Count"]),
                    "click_price_mkt": _f(r["Clicks: Price (Median)"]),
                    "click_price_brand": _f(r["Clicks: Brand Price (Median)"]),
                    "purch_price_mkt": _f(r["Purchases: Price (Median)"]),
                    "purch_price_brand": _f(r["Purchases: Brand Price (Median)"]),
                })
        return out

    def _repeat_asin(self):
        out = []
        for p in self._files("*Repeat_Purchase_Behavior_ASIN_View*.csv"):
            for r in _read_report(p):
                out.append({
                    "week_end": r.get("Reporting Date") or _week_from_name(p),
                    "asin": r["ASIN"],
                    "title": r["Product Title"],
                    "orders": _i(r["Number of Orders"]),
                    "customers": _i(r["Unique Customer Count"]),
                    "repeat_sales": _f(r["Repeat Ordered Product Sales: Sales"]) or 0.0,
                    "repeat_customers": _i(r["Repeat Customer Count: Count"]),
                })
        return out

    def _basket(self):
        out = []
        for p in self._files("*Market_Basket_Analysis*.csv"):
            for r in _read_report(p):
                combos = []
                for n in (1, 2, 3):
                    a = r.get(f"#{n} Purchase Combination: ASIN")
                    if a:
                        combos.append({
                            "asin": a,
                            "title": r[f"#{n} Purchase Combination: Product Title"],
                            "pct": _f(r[f"#{n} Purchase Combination: Combination %"]) or 0.0,
                        })
                out.append({
                    "week_end": r.get("Reporting Date") or _week_from_name(p),
                    "asin": r["ASIN"],
                    "title": r["Product Title"],
                    "orders": _i(r["Number of Orders"]),
                    "combos": combos,
                })
        return out

    def _listing_health(self):
        out = []
        for p in self._files("Listing_Summary_Report*.csv"):
            for r in _read_report(p):
                reason, _, share = r["Top NCX reason"].partition("\n")
                out.append({
                    "asin": r["ASIN"],
                    "sku": r["SKU"],
                    "title": r["Product name"],
                    "condition": r["Condition"].lower(),
                    "fulfilled_by": r["Fulfilled by"],
                    "orders": _i(r["Total orders"]),
                    "ncx_orders": _i(r["NCX orders"]),
                    "ncx_rate": _f(r["NCX rate"]),
                    "return_rate": _f(r["Return rate"]),
                    "stars": _f(r["Star rating"]),
                    "top_ncx_reason": None if reason.strip() in ("", "--") else reason.strip(),
                    "top_ncx_share": _f(share),
                    "cx_health": r["CX Health"],
                    "return_badge": r["Return Badge Displayed"],
                })
        return out


# ------------------------------------------------------------------------ SP-API

SPAPI_ENDPOINTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}
US_MARKETPLACE = "ATVPDKIKX0DER"


def _read_env(path):
    """KEY=value lines from a .env file; blank values are skipped."""
    out = {}
    if path and os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                k, sep, v = line.strip().partition("=")
                v = re.sub(r"\s+#.*$", "", v)  # inline comments: KEY=na   # na | eu | fe
                if sep and not k.startswith("#") and v.strip():
                    out[k.strip()] = v.strip().strip("'\"")
    return out

SPAPI_TRIES = 8            # attempts for one call
SPAPI_RETRY_SECONDS = 65   # a little over the createReport refill time

# What each dashboard table needs from the Reports API. Tables without an API
# report (customer_trends, listing_health) stay as CSV drop-ins.
SPAPI_REPORTS = {
    "sqp": {
        "reportType": "GET_BRAND_ANALYTICS_SEARCH_QUERY_PERFORMANCE_REPORT",
        "options": {"reportPeriod": "WEEK"},  # plus "asin": space-separated list, <=200 chars
        "per_asin_batch": True,
    },
    "repeat_asin": {
        "reportType": "GET_BRAND_ANALYTICS_REPEAT_PURCHASE_REPORT",
        "options": {"reportPeriod": "WEEK"},
    },
    "basket": {
        "reportType": "GET_BRAND_ANALYTICS_MARKET_BASKET_REPORT",
        "options": {"reportPeriod": "WEEK"},
    },
    "sales_traffic": {
        "reportType": "GET_SALES_AND_TRAFFIC_REPORT",
        "options": {"dateGranularity": "WEEK", "asinGranularity": "CHILD"},
        "optional": True,  # not on the page yet, so a failure here must not stop the refresh
    },
    "returns": {
        "reportType": "GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA",
        "options": {},
    },
    "inventory": {
        "reportType": "GET_FBA_INVENTORY_PLANNING_DATA",
        "options": {},
    },
}


class SpApiSource:
    """Pulls the same tables through the Selling Partner API Reports endpoints.

    Credentials come from a local JSON file (see config.example.json) or the
    amazon-agents .env (SP_API_* keys, which win), never from the command line.
    Every raw report is saved under raw_dir so the JSON->table mappings below
    can be checked against the first live pull.
    """
    name = "spapi"

    def __init__(self, config_path, raw_dir, week_end, asins=None, dry_run=False, env_path=None, known_titles=None):
        with open(config_path) as fh:
            self.cfg = json.load(fh)
        env = _read_env(env_path)
        for key in ("client_id", "client_secret", "refresh_token", "region", "marketplace_id"):
            if env.get("SP_API_" + key.upper()):
                self.cfg[key] = env["SP_API_" + key.upper()]
        self.endpoint = SPAPI_ENDPOINTS[self.cfg.get("region", "na")]
        self.marketplace = self.cfg.get("marketplace_id", US_MARKETPLACE)
        self.raw_dir = raw_dir
        self.week_end = dt.date.fromisoformat(week_end)  # Saturday that ends the week
        self.asins = asins or self.cfg.get("asins", [])
        self.dry_run = dry_run
        self.known_titles = known_titles or {}
        self._token, self._token_at = None, 0.0

    # -- HTTP plumbing ----------------------------------------------------------
    def _access_token(self):
        # Sign-in tokens last an hour and a full pull runs longer than that.
        if self._token and time.time() - self._token_at < 3000:
            return self._token
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self.cfg["refresh_token"],
            "client_id": self.cfg["client_id"],
            "client_secret": self.cfg["client_secret"],
        }).encode()
        req = urllib.request.Request("https://api.amazon.com/auth/o2/token", data=body)
        with urllib.request.urlopen(req, timeout=30) as resp:
            self._token = json.load(resp)["access_token"]
        self._token_at = time.time()
        return self._token

    def _call(self, method, path, payload=None):
        # createReport allows about one request a minute after a burst of 15, so a full
        # week's search batches need to wait rather than fail. Only throttling and server
        # errors are retried; anything else is a real failure.
        for attempt in range(SPAPI_TRIES):
            req = urllib.request.Request(
                self.endpoint + path,
                method=method,
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={"x-amz-access-token": self._access_token(),
                         "content-type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt == SPAPI_TRIES - 1:
                    raise
                time.sleep(SPAPI_RETRY_SECONDS)

    def _run_report(self, report_type, options, start, end):
        payload = {
            "reportType": report_type,
            "marketplaceIds": [self.marketplace],
            "dataStartTime": start.isoformat(),
            "dataEndTime": end.isoformat(),
        }
        if options:
            payload["reportOptions"] = options
        if self.dry_run:
            print("  POST /reports/2021-06-30/reports", json.dumps(payload))
            return None
        batch = (options or {}).get("asin")
        suffix = "_" + hashlib.sha1(batch.encode()).hexdigest()[:10] if batch else ""
        saved = os.path.join(self.raw_dir, f"{report_type}_{end.date()}{suffix}.json")
        if batch and os.path.exists(saved):  # finished search batches aren't requested twice
            with open(saved) as fh:
                return json.load(fh)
        rid = self._call("POST", "/reports/2021-06-30/reports", payload)["reportId"]
        for _ in range(120):  # Brand Analytics reports can take several minutes
            rep = self._call("GET", f"/reports/2021-06-30/reports/{rid}")
            status = rep["processingStatus"]
            if status == "DONE":
                break
            if status in ("CANCELLED", "FATAL"):
                raise RuntimeError(f"{report_type} {status}")
            time.sleep(15)
        else:
            raise TimeoutError(report_type)
        doc = self._call("GET", f"/reports/2021-06-30/documents/{rep['reportDocumentId']}")
        with urllib.request.urlopen(doc["url"], timeout=120) as resp:
            data = resp.read()
        if doc.get("compressionAlgorithm") == "GZIP":
            data = gzip.decompress(data)
        os.makedirs(self.raw_dir, exist_ok=True)
        ext = "json" if data[:1] in (b"{", b"[") else "tsv"
        with open(os.path.join(self.raw_dir, f"{report_type}_{end.date()}{suffix}.{ext}"), "wb") as fh:
            fh.write(data)
        return json.loads(data) if ext == "json" else data.decode("utf-8", "replace")

    # -- tables -----------------------------------------------------------------
    def load(self):
        start = dt.datetime.combine(self.week_end - dt.timedelta(days=6), dt.time.min)
        end = dt.datetime.combine(self.week_end, dt.time.max).replace(microsecond=0)
        week = self.week_end.isoformat()
        t = {}
        for key, spec in SPAPI_REPORTS.items():
            if not hasattr(self, f"_map_{key}"):
                print(f"[spapi] {key}: skipped, no table mapping yet")
                continue
            print(f"[spapi] {key}: {spec['reportType']}")
            try:
                if spec.get("per_asin_batch"):
                    docs = [self._run_report(spec["reportType"], {**spec["options"], "asin": b}, start, end)
                            for b in self._asin_batches()]
                else:
                    docs = [self._run_report(spec["reportType"], spec["options"], start, end)]
                docs = [d for d in docs if d is not None]
                mapper = getattr(self, f"_map_{key}", None)
                if docs and mapper:
                    t[key] = [row for d in docs for row in mapper(d, week)]
            except Exception as e:
                if not spec.get("optional"):
                    raise
                print(f"[spapi] {key}: skipped, {e}")
        return t if self.dry_run else self._complete(t)

    def _complete(self, t):
        """The API leaves out what the CSVs carry: product titles, which the dashboard
        groups by, and orders per ASIN in the basket report, which it weights by."""
        orders = {r["asin"]: r["orders"] for r in t.get("repeat_asin", [])}
        for r in t.get("basket", []):
            r["orders"] = orders.get(r["asin"], 0)
        wanted = {r["asin"] for k in ("repeat_asin", "basket") for r in t.get(k, [])}
        wanted |= {c["asin"] for r in t.get("basket", []) for c in r["combos"]}
        titles = self.titles(wanted)
        for r in t.get("repeat_asin", []) + t.get("basket", []):
            r["title"] = titles.get(r["asin"], r["asin"])
        for r in t.get("basket", []):
            for c in r["combos"]:
                c["title"] = titles.get(c["asin"], c["asin"])
        return t

    def load_daily(self, days=60):
        """Daily sales, units and refunds for the whole account: one report, any length."""
        end = dt.datetime.combine(self.week_end, dt.time.max).replace(microsecond=0)
        start = dt.datetime.combine(self.week_end - dt.timedelta(days=days - 1), dt.time.min)
        doc = self._run_report("GET_SALES_AND_TRAFFIC_REPORT", {"dateGranularity": "DAY", "asinGranularity": "CHILD"}, start, end)
        return map_sales_daily(doc) if doc else []

    def titles(self, asins):
        """Titles from earlier exports first, then the Catalog API, cached so each ASIN
        is only looked up once. A failed lookup leaves the ASIN as its own title."""
        path = os.path.join(self.raw_dir, "asin_titles.json")
        known = dict(self.known_titles)
        if os.path.exists(path):
            with open(path) as fh:
                known.update(json.load(fh))
        for asin in sorted(set(asins) - set(known)):
            try:
                doc = self._call("GET", f"/catalog/2022-04-01/items/{asin}?marketplaceIds={self.marketplace}"
                                        "&includedData=summaries")
                name = next((s.get("itemName") for s in doc.get("summaries", []) if s.get("itemName")), None)
            except urllib.error.HTTPError:
                continue
            if name:
                known[asin] = name
            time.sleep(0.5)
        os.makedirs(self.raw_dir, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(known, fh, indent=0)
        return known

    def _asin_batches(self):
        batch, out = [], []
        for a in self.asins:
            if len(" ".join(batch + [a])) > 200:
                out.append(" ".join(batch))
                batch = []
            batch.append(a)
        if batch:
            out.append(" ".join(batch))
        return out or [""]

    # JSON field names below follow the published Brand Analytics report schemas.
    # Verify against the files in raw_dir on the first live pull.
    @staticmethod
    def _map_sqp(doc, week):
        # API is ASIN-level; roll up to brand-level per query (purchase/cart/click
        # counts sum cleanly; impressions may double-count multi-ASIN results).
        agg = {}
        for r in doc.get("dataByAsin", []):
            q = r["searchQueryData"]
            row = agg.setdefault(q["searchQuery"], {
                "week_end": week, "query": q["searchQuery"],
                "rank": q.get("searchQueryScore", 0), "volume": q.get("searchQueryVolume", 0),
                "impr_total": r["impressionData"].get("totalQueryImpressionCount", 0),
                "clicks_total": r["clickData"].get("totalClickCount", 0),
                "cart_total": r["cartAddData"].get("totalCartAddCount", 0),
                "purch_total": r["purchaseData"].get("totalPurchaseCount", 0),
                "impr_brand": 0, "clicks_brand": 0, "cart_brand": 0, "purch_brand": 0,
                "click_price_mkt": (r["clickData"].get("totalMedianClickPrice") or {}).get("amount"),
                "click_price_brand": (r["clickData"].get("asinMedianClickPrice") or {}).get("amount"),
                "purch_price_mkt": (r["purchaseData"].get("totalMedianPurchasePrice") or {}).get("amount"),
                "purch_price_brand": (r["purchaseData"].get("asinMedianPurchasePrice") or {}).get("amount"),
            })
            row["impr_brand"] += r["impressionData"].get("asinImpressionCount", 0)
            row["clicks_brand"] += r["clickData"].get("asinClickCount", 0)
            row["cart_brand"] += r["cartAddData"].get("asinCartAddCount", 0)
            row["purch_brand"] += r["purchaseData"].get("asinPurchaseCount", 0)
        return list(agg.values())

    @staticmethod
    def _map_repeat_asin(doc, week):
        # Amazon sends nulls for ASINs too small to report, and repeatCustomersPctTotal as
        # a fraction (0.1 for 10%), despite its name. Checked against the CSV for 2026-10-10.
        return [{
            "week_end": week, "asin": r["asin"], "title": r["asin"],
            "orders": r.get("orders") or 0, "customers": r.get("uniqueCustomers") or 0,
            "repeat_sales": (r.get("repeatPurchaseRevenue") or {}).get("amount") or 0.0,
            "repeat_customers": round((r.get("uniqueCustomers") or 0) * (r.get("repeatCustomersPctTotal") or 0)),
        } for r in doc.get("dataByAsin", [])]

    @staticmethod
    def _map_basket(doc, week):
        rows = {}
        for r in doc.get("dataByAsin", []):
            row = rows.setdefault(r["asin"], {"week_end": week, "asin": r["asin"], "title": r["asin"],
                                              "orders": 0, "combos": []})
            row["combos"].append({"asin": r["purchasedWithAsin"], "title": r["purchasedWithAsin"],
                                  "pct": round((r.get("combinationPct") or 0.0) * 100, 2),  # a fraction in the API
                                  "rank": r.get("purchasedWithRank")})
        return list(rows.values())

    @staticmethod
    def _map_sales_traffic(doc, week):
        rows = doc.get("salesAndTrafficByDate", [])
        if not rows:
            return []
        out = {"week_end": week, "sales": 0.0, "units": 0, "order_items": 0, "sessions": 0, "page_views": 0}
        for r in rows:
            sales, traffic = r.get("salesByDate") or {}, r.get("trafficByDate") or {}
            out["sales"] += (sales.get("orderedProductSales") or {}).get("amount") or 0.0
            out["units"] += sales.get("unitsOrdered") or 0
            out["order_items"] += sales.get("totalOrderItems") or 0
            out["sessions"] += traffic.get("sessions") or 0
            out["page_views"] += traffic.get("pageViews") or 0
        return [out]


def map_sales_daily(doc):
    out = []
    for r in doc.get("salesAndTrafficByDate", []):
        s, t = r.get("salesByDate") or {}, r.get("trafficByDate") or {}
        out.append({"date": r["date"], "sales": (s.get("orderedProductSales") or {}).get("amount") or 0.0,
                    "units": s.get("unitsOrdered") or 0, "order_items": s.get("totalOrderItems") or 0,
                    "units_refunded": s.get("unitsRefunded"), "sessions": t.get("sessions") or 0})
    return out


def save_sales_daily(folder, rows):
    """Keep every day seen; a later pull replaces the same day, since refunds keep arriving."""
    path = os.path.join(folder, "sales_daily.json")
    days = {r["date"]: r for r in load_sales_daily(folder)}
    days.update({r["date"]: r for r in rows})
    os.makedirs(folder, exist_ok=True)
    with open(path, "w") as fh:
        json.dump([days[d] for d in sorted(days)], fh, indent=0)


def load_sales_daily(folder):
    path = os.path.join(folder, "sales_daily.json")
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return json.load(fh)


def business_report_daily(folder):
    """Daily exports from Seller Central's Business Reports (amazon_daily_*.csv). The same
    measure as the API, so they give the weekly sales series its history. No refunds column."""
    days = {}
    for path in sorted(glob.glob(os.path.join(folder, "amazon_daily_*.csv")), key=os.path.getmtime):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                day = dt.datetime.strptime(r["Date"], "%m/%d/%y").date().isoformat()
                days[day] = {"date": day, "sales": _f(r["Ordered Product Sales"].replace("$", "")) or 0.0,
                             "units": _i(r["Units Ordered"]), "order_items": _i(r["Total Order Items"]),
                             "units_refunded": None, "sessions": _i(r["Sessions - Total"])}
    return [days[d] for d in sorted(days)]


def merge_daily(exports, api):
    """API days win: they're the fresher copy and the only ones with refunds."""
    days = {r["date"]: r for r in exports}
    days.update({r["date"]: r for r in api})
    return [days[d] for d in sorted(days)]


def titles_from(tables):
    """ASIN -> title from the CSV exports already on disk, to avoid Catalog API calls."""
    out = {}
    for key in ("repeat_asin", "basket", "listing_health"):
        for r in tables.get(key, []):
            if r.get("title"):
                out[r["asin"]] = r["title"]
            for c in r.get("combos", []):
                if c.get("title"):
                    out[c["asin"]] = c["title"]
    return out


def save_api_tables(folder, tables):
    """Keep each week the API returned, one file per table and week. Nothing else stores these
    tables, and the CSV folder won't have the weeks, so without this they vanish on the next run."""
    os.makedirs(folder, exist_ok=True)
    for key, rows in tables.items():
        by_week = {}
        for r in rows:
            by_week.setdefault(r["week_end"], []).append(r)
        for week, week_rows in by_week.items():
            path = os.path.join(folder, f"{key}_{week}.json")
            with open(path + ".tmp", "w") as fh:
                json.dump(week_rows, fh)
            os.replace(path + ".tmp", path)


def load_api_tables(folder):
    tables = {}
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        key = os.path.basename(path).rsplit("_", 1)[0]
        with open(path) as fh:
            tables.setdefault(key, []).extend(json.load(fh))
    return tables


def merge_weekly(csv_tables, api_tables):
    """Add API weeks to the CSV history. A week that has a CSV keeps the CSV: those exports are
    what the dashboard was validated against, while the API rows are rolled up from ASIN level."""
    merged = dict(csv_tables)
    for key, rows in api_tables.items():
        have = {r.get("week_end") for r in csv_tables.get(key, [])}
        new = [r for r in rows if r.get("week_end") not in have]
        if new:
            merged[key] = sorted(csv_tables.get(key, []) + new, key=lambda r: r["week_end"])
    return merged


class WarehouseSource:
    """Sales, traffic, stock, ads and store tables from the amazon-agents warehouse
    (~/Documents/amazon-agents/data/warehouse.db), read-only. The agents fill it from
    Seller Central report downloads (amz-agents seller), a Quartile export
    (amz-agents quartile) and Shopify, so this dashboard and the agent tracker show
    the same numbers.

        sales_daily  date, units, revenue                  (Amazon.com, all SKUs)
        sku_30d      sku, asin, title, units, revenue      (last 30 days of data)
        traffic      sku, asin, title, sessions, page_views, units, buy_box
        stock        sku, asin, title, on_hand, inbound, velocity
        ads          one row: period, ad_types, sales, ad_sales, spend
        store        one row: shopify variants, units_30d
    """
    name = "warehouse"

    def __init__(self, path):
        self.path = path

    def load(self):
        if not os.path.exists(self.path):
            return {}
        import sqlite3

        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        q = lambda sql, *a: [dict(r) for r in db.execute(sql, a)]
        t = {}
        try:
            t["sales_daily"] = q("SELECT date, SUM(units) AS units, SUM(revenue) AS revenue "
                                 "FROM metrics_daily GROUP BY date ORDER BY date")
            last = t["sales_daily"][-1]["date"] if t["sales_daily"] else None
            if last:
                start = (dt.date.fromisoformat(last) - dt.timedelta(days=29)).isoformat()
                t["sku_30d"] = q("SELECT m.sku, p.asin, p.title, SUM(m.units) AS units, "
                                 "SUM(m.revenue) AS revenue FROM metrics_daily m LEFT JOIN products p "
                                 "ON p.sku = m.sku WHERE m.date >= ? GROUP BY m.sku", start)
            t["traffic"] = q("SELECT a.sku, p.asin, p.title, a.sessions_30d AS sessions, "
                             "a.page_views_30d AS page_views, a.units_30d AS units, "
                             "a.buy_box_pct AS buy_box FROM asin_traffic a "
                             "LEFT JOIN products p ON p.sku = a.sku")
            t["stock"] = q("SELECT sku, asin, title, units_on_hand AS on_hand, "
                           "units_inbound AS inbound, daily_velocity AS velocity FROM products "
                           "WHERE units_on_hand IS NOT NULL")
            ads = q("SELECT period_start, period_end, ad_types, SUM(total_sales) AS sales, "
                    "SUM(ad_sales) AS ad_sales, SUM(ad_spend) AS spend FROM ad_products "
                    "GROUP BY period_start, period_end ORDER BY period_end DESC, period_start LIMIT 1")
            t["ads"] = ads
            t["store"] = q("SELECT COUNT(*) AS variants, SUM(units_30d) AS units_30d "
                           "FROM channel_listings WHERE channel = 'shopify'")
        except sqlite3.OperationalError:
            pass  # an older warehouse without these tables
        finally:
            db.close()
        return {k: v for k, v in t.items() if v and not (len(v) == 1 and not any(v[0].values()))}


class WalmartSource:
    """Walmart Marketplace snapshot read from Seller Center (no API connection yet):
    <folder>/walmart_snapshot.json. Lives with the business data, outside the repo."""
    name = "walmart"

    def __init__(self, folder):
        self.path = os.path.join(folder, "walmart_snapshot.json")

    def load(self):
        if not os.path.exists(self.path):
            return {}
        with open(self.path) as fh:
            return {"walmart": json.load(fh)}
