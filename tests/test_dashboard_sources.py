"""SP-API weeks must add to the dashboard's CSV history, never replace it."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "dashboard_sources", Path(__file__).parent.parent / "dashboard" / "sources.py")
sources = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sources)


def rows(*weeks, tag="csv"):
    return [{"week_end": w, "tag": tag} for w in weeks]


def test_api_week_adds_to_csv_history():
    merged = sources.merge_weekly({"sqp": rows("2026-10-17", "2026-10-24")},
                                  {"sqp": rows("2026-10-31", tag="api")})
    assert [r["week_end"] for r in merged["sqp"]] == ["2026-10-17", "2026-10-24", "2026-10-31"]


def test_week_with_a_csv_keeps_the_csv():
    merged = sources.merge_weekly({"sqp": rows("2026-10-24")}, {"sqp": rows("2026-10-24", tag="api")})
    assert [r["tag"] for r in merged["sqp"]] == ["csv"]


def test_merge_keeps_other_tables_and_accepts_api_only_tables():
    merged = sources.merge_weekly({"listing_health": [{"asin": "A"}]}, {"sales_traffic": rows("2026-10-31", tag="api")})
    assert merged["listing_health"] == [{"asin": "A"}]
    assert merged["sales_traffic"][0]["tag"] == "api"


def test_saved_weeks_survive_and_overwrite_the_same_week(tmp_path):
    sources.save_api_tables(tmp_path, {"sales_traffic": rows("2026-10-24", tag="old")})
    sources.save_api_tables(tmp_path, {"sales_traffic": rows("2026-10-24", tag="new"),
                                       "repeat_asin": rows("2026-10-31", tag="new")})
    loaded = sources.load_api_tables(tmp_path)
    assert loaded["sales_traffic"] == rows("2026-10-24", tag="new")
    assert loaded["repeat_asin"] == rows("2026-10-31", tag="new")


def test_map_sales_traffic_sums_the_report():
    doc = {"salesAndTrafficByDate": [
        {"salesByDate": {"orderedProductSales": {"amount": 100.5}, "unitsOrdered": 3, "totalOrderItems": 2},
         "trafficByDate": {"sessions": 40, "pageViews": 60}},
        {"salesByDate": {"orderedProductSales": {"amount": 50.0}, "unitsOrdered": 1, "totalOrderItems": 1},
         "trafficByDate": {"sessions": 10, "pageViews": 15}},
    ]}
    assert sources.SpApiSource._map_sales_traffic(doc, "2026-10-31") == [
        {"week_end": "2026-10-31", "sales": 150.5, "units": 4, "order_items": 3, "sessions": 50, "page_views": 75}]
    assert sources.SpApiSource._map_sales_traffic({}, "2026-10-31") == []


def source_with(tmp_path, run_report):
    config = tmp_path / "spapi.json"
    config.write_text('{"region": "na"}')
    src = sources.SpApiSource(config, tmp_path / "raw", "2026-10-31", asins=["B1"])
    src._run_report = run_report
    return src


def test_optional_report_failure_keeps_the_other_tables(tmp_path):
    def run_report(report_type, options, start, end):
        if report_type == "GET_SALES_AND_TRAFFIC_REPORT":
            raise RuntimeError("FATAL")
        return {"dataByAsin": []}

    tables = source_with(tmp_path, run_report).load()
    assert "sales_traffic" not in tables
    assert tables["repeat_asin"] == []


def test_required_report_failure_still_stops_the_build(tmp_path):
    def run_report(report_type, options, start, end):
        raise RuntimeError("FATAL")

    with pytest.raises(RuntimeError):
        source_with(tmp_path, run_report).load()


def _walmart_snapshot(days=70):
    import datetime as dt
    start = dt.date(2026, 8, 1)  # a Saturday, so the first full week starts Aug 2
    return {"captured": "2026-11-03", "daily": [
        {"date": (start + dt.timedelta(days=i)).isoformat(), "gmv": 80.0, "net": 70.0, "units": 4,
         "orders": 4, "auth": 100.0, "cancelled": 0.0, "refunds": 20.0} for i in range(days)],
        "top_items": [{"sku": "A", "gmv": 80.0}], "stock": [{"sku": "A", "status": "out"}]}


def test_walmart_source_is_optional(tmp_path):
    assert sources.WalmartSource(tmp_path).load() == {}
    (tmp_path / "walmart_snapshot.json").write_text('{"captured": "2026-11-03"}')
    assert sources.WalmartSource(tmp_path).load()["walmart"]["captured"] == "2026-11-03"


def test_walmart_kpis_compare_orders_placed_with_amazon():
    spec = importlib.util.spec_from_file_location("dashboard_kpis", Path(__file__).parent.parent / "dashboard" / "kpis.py")
    kpis = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kpis)
    w = kpis.walmart_kpis(_walmart_snapshot(), {"series": [{"week_end": "2026-08-08", "sales": 2100.0}]})
    assert w["last30"]["days"] == 30 and w["prior30"]["days"] == 30
    assert w["last30"]["refund_share"] == pytest.approx(20.0)
    first = w["weekly"][0]  # the partial Aug 1 is skipped; Aug 2-8 is the first full week
    assert first["week_end"] == "2026-08-08" and first["auth"] == 700.0
    assert first["share"] == pytest.approx(25.0)
    assert w["top"][0]["stock"]["status"] == "out"


def _walmart_api():
    spec = importlib.util.spec_from_file_location("walmart_api", Path(__file__).parent.parent / "dashboard" / "walmart_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _perf_row(day, sku="A", gmv="80.00", units="4"):
    return {"Data Start Time": f"{day}T00:00:00Z", "SKU ID": sku, "GMV": gmv, "GMV-Commission": "70", "Total Units": units,
            "Authorized Sales": "$100.00", "Authorized Orders": "4", "Authorized Units": "4", "Cancelled Sales": "0",
            "Refunded Sales": "20", "Product Level Pageviews": "40"}


class _FakeWalmart:
    """Stands in for the HTTP layer: two reports, then one page of inventory."""
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def call(self, method, path, query=None, payload=None):
        self.calls.append((method, path))
        if method == "POST":
            return {"requestId": "r1"}
        if path.startswith("/v3/reports/reportRequests/"):
            return {"requestStatus": "READY"}
        if path == "/v3/reports/downloadReport":
            return {"downloadURL": "https://example.test/file"}
        return {"headers": {"totalCount": 1}, "payload": {"inventory": [
            {"itemInformation": {"sku": "A"}, "inventoryData": {"availableUnits": 10, "inboundUnits": 5}}]}}

    def download(self, url):
        import csv, io
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=list(self.rows[0]))
        w.writeheader()
        w.writerows(self.rows)
        return out.getvalue().encode()

    def sleep(self, s):
        pass


def test_walmart_client_refuses_writes():
    w = _walmart_api()
    api = w.Api("id", "secret", opener=lambda *a, **k: pytest.fail("no request should be sent"))
    for path in ("/v3/price", "/v3/orders/1/cancel", "/v3/items"):
        with pytest.raises(w.WalmartError):
            api._send("POST", path)
    with pytest.raises(w.WalmartError):
        api._send("PUT", "/v3/price")


def test_walmart_windows_respect_the_15_day_limit():
    import datetime as dt
    w = _walmart_api()
    spans = w.windows(dt.date(2026, 10, 1), dt.date(2026, 10, 31))
    assert spans[0][0] == dt.date(2026, 10, 1) and spans[-1][1] == dt.date(2026, 10, 31)
    assert all((b - a).days < 15 for a, b in spans) and len(spans) == 3


def test_walmart_summarize_adds_skus_by_day_and_rejects_changed_columns():
    w = _walmart_api()
    daily = w.summarize([_perf_row("2026-10-01"), _perf_row("2026-10-01", "B"), _perf_row("2026-10-02")])
    assert [(d["date"], d["gmv"], d["auth"]) for d in daily] == [("2026-10-01", 160.0, 200.0), ("2026-10-02", 80.0, 100.0)]
    bad = _perf_row("2026-10-01")
    del bad["GMV"]
    with pytest.raises(w.WalmartError):
        w.summarize([bad])
    with pytest.raises(w.WalmartError):
        w.summarize([])


def test_walmart_first_pull_must_match_the_hand_read(tmp_path):
    import datetime as dt
    import json
    w = _walmart_api()
    today = dt.date(2026, 11, 3)
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(1, 31)]
    hand = [{"date": d, "gmv": 80.0, "net": 70.0, "units": 4, "orders": 4, "auth": 100.0, "cancelled": 0, "refunds": 20} for d in days]
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"captured": "2026-11-01", "daily": hand, "wfs": {"age": []}, "notes": ["kept"]}))
    before = path.read_text()

    with pytest.raises(w.WalmartError):  # API sales double the hand read: nothing is saved
        w.pull(_FakeWalmart([_perf_row(d, gmv="160") for d in days]), str(path), today=today)
    assert path.read_text() == before

    w.pull(_FakeWalmart([_perf_row(d) for d in days]), str(path), today=today)
    snap = json.loads(path.read_text())
    assert snap["auto"] is True and snap["hand_read"] == "2026-11-01" and snap["notes"] == ["kept"]
    assert snap["wfs"]["items_total"] == 1 and snap["wfs"]["age"] == []
    assert snap["stock"][0]["status"] == "at_risk" and snap["stock"][0]["days_supply"] == 4  # 15 units at 4 a day


def test_walmart_stock_status_rule():
    w = _walmart_api()
    inv = [{"itemInformation": {"sku": s}, "inventoryData": {"availableUnits": a, "inboundUnits": i}}
           for s, a, i in (("OUT", 0, 22), ("LOW", 10, 0), ("OK", 10, 200), ("IDLE", 5, 0))]
    rows, total, risky = w.stock_rows(inv, {"OUT": 60, "LOW": 60, "OK": 60}, {"OUT": 600, "LOW": 500, "OK": 400}, 30)
    assert {r["sku"]: r["status"] for r in rows} == {"OUT": "out", "LOW": "at_risk", "OK": "in_stock", "IDLE": "in_stock"}
    assert total == 4 and risky == 2


def _spapi(monkeypatch, responses):
    import io, json, urllib.error
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake(req, timeout=None):
        calls.append(req.full_url)
        code = responses.pop(0)
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b"{}"))
        return Resp(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(sources.urllib.request, "urlopen", fake)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    api = sources.SpApiSource.__new__(sources.SpApiSource)
    api.endpoint, api._token = "https://sp.test", "token"
    api._token_at = sources.time.time()
    return api, calls


def test_spapi_waits_out_throttling(monkeypatch):
    api, calls = _spapi(monkeypatch, [429, 503, 200])
    assert api._call("POST", "/reports/2021-06-30/reports", {}) == {"ok": True}
    assert len(calls) == 3


def test_spapi_does_not_retry_real_errors(monkeypatch):
    import urllib.error
    api, calls = _spapi(monkeypatch, [403])
    with pytest.raises(urllib.error.HTTPError):
        api._call("GET", "/x")
    assert len(calls) == 1


def test_repeat_mapping_handles_nulls_and_fractions():
    doc = {"dataByAsin": [
        {"asin": "A", "orders": 30, "uniqueCustomers": 20, "repeatCustomersPctTotal": 0.1,
         "repeatPurchaseRevenue": {"amount": 10.5}},
        {"asin": "B", "orders": None, "uniqueCustomers": None, "repeatCustomersPctTotal": None,
         "repeatPurchaseRevenue": None}]}
    a, b = sources.SpApiSource._map_repeat_asin(doc, "2026-10-10")
    assert (a["orders"], a["customers"], a["repeat_customers"], a["repeat_sales"]) == (30, 20, 2, 10.5)
    assert (b["orders"], b["customers"], b["repeat_customers"], b["repeat_sales"]) == (0, 0, 0, 0.0)


def test_basket_gets_percentages_orders_and_titles():
    doc = {"dataByAsin": [{"asin": "A", "purchasedWithAsin": "X", "purchasedWithRank": 1, "combinationPct": 0.1667}]}
    api = sources.SpApiSource.__new__(sources.SpApiSource)
    api.titles = lambda asins: {"A": "Dagacci set", "X": "Other brand scrubs"}
    t = api._complete({"basket": sources.SpApiSource._map_basket(doc, "w"),
                       "repeat_asin": [{"asin": "A", "orders": 12, "title": "A"}]})
    row = t["basket"][0]
    assert row["orders"] == 12 and row["title"] == "Dagacci set"
    assert row["combos"][0]["pct"] == 16.67 and row["combos"][0]["title"] == "Other brand scrubs"


def _kpis():
    spec = importlib.util.spec_from_file_location("dashboard_kpis", Path(__file__).parent.parent / "dashboard" / "kpis.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _days(start, n, refunded=5):
    import datetime as dt
    d = dt.date.fromisoformat(start)
    return [{"date": (d + dt.timedelta(days=i)).isoformat(), "sales": 100.0, "units": 10, "order_items": 8,
             "units_refunded": refunded, "sessions": 50} for i in range(n)]


def test_weekly_sales_uses_full_sunday_to_saturday_weeks():
    w = _kpis().weekly_kpis(_days("2026-09-30", 31))  # starts Wednesday, ends Friday: both ends partial
    assert [r["week_end"] for r in w["series"]] == ["2026-10-10", "2026-10-17", "2026-10-24"]
    assert w["latest"]["sales"] == 700.0 and w["source"] == "account"


def test_refund_rate_needs_four_weeks_of_refunds():
    k = _kpis()
    w = k.weekly_kpis(_days("2026-09-06", 28))
    assert [r["refund_rate_4w"] for r in w["series"]] == [None, None, None, pytest.approx(50.0)]
    w = k.weekly_kpis(_days("2026-09-06", 28, refunded=None))  # exports have no refunds column
    assert w["latest"]["refund_rate_4w"] is None


def test_weekly_falls_back_to_customer_trends():
    w = _kpis().weekly_kpis([], [{"week_end": "2026-10-24", "sales": 5.0, "orders": 2}])
    assert w["source"] == "customer_trends" and w["latest"]["sales"] == 5.0


def test_api_days_replace_export_days():
    merged = sources.merge_daily([{"date": "d1", "src": "csv"}, {"date": "d2", "src": "csv"}], [{"date": "d2", "src": "api"}])
    assert [r["src"] for r in merged] == ["csv", "api"]


def test_business_report_export_reads_dollar_amounts(tmp_path):
    (tmp_path / "amazon_daily_x.csv").write_text(
        'Date,Ordered Product Sales,Units Ordered,Total Order Items,Sessions - Total\n10/4/26,"$1,234.50",50,40,"2,000"\n',
        encoding="utf-8-sig")
    r = sources.business_report_daily(tmp_path)[0]
    assert (r["date"], r["sales"], r["units"], r["order_items"], r["sessions"]) == ("2026-10-04", 1234.5, 50, 40, 2000)
