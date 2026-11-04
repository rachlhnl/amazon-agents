"""Build the Dagacci KPI dashboard.

    python3 -I build_dashboard.py                      # from CSV exports (default)
    python3 -I build_dashboard.py --source spapi --week-end 2026-10-10 --dry-run
    python3 -I build_dashboard.py --source spapi --week-end 2026-10-10

SP-API mode reads keys from ~/Documents/dagacci/config/spapi.json (copy config.example.json).
Tables the API can't provide (customer trends, listing health) fall back to CSV.
Sales, traffic, stock and ads come from the amazon-agents warehouse when it exists
(--warehouse), the same data the agent tracker shows.
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kpis
import sources

# Code lives in the repo; business data (exports, built page, KPI history) stays outside it,
# in DAGACCI_DATA (default ~/Documents/dagacci). realpath: the old pipeline path is a symlink.
HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(HERE)
ROOT = os.path.expanduser(os.environ.get("DAGACCI_DATA", "~/Documents/dagacci"))


def latest_week_end(today=None):
    """Brand Analytics weeks end on Saturday and post a few days later."""
    d = (today or dt.date.today()) - dt.timedelta(days=3)
    return d - dt.timedelta(days=(d.weekday() - 5) % 7)


def update_history(path, data):
    """Keep one snapshot per data week (scorecard values + the week's recommendations)
    so weekly and monthly reports can compare periods after kpis.json is overwritten.
    Weeks before the first run are backfilled from the trend series where they exist."""
    hist = {}
    if os.path.exists(path):
        with open(path) as fh:
            hist = json.load(fh)
    C, S, W = data.get("customers"), data.get("search"), data.get("weekly")
    for r in (C["series"] if C else []):
        hist.setdefault(r["week_end"], {}).setdefault("kpis", {}).update(
            {"weekly_sales": r["sales"], "repeat_rate": r["repeat_rate"]})
    # Sales and refunds come from one measure across all weeks, replacing Customer Trends' sales.
    for r in (W["series"] if W else []):
        if W["source"] == "account":
            k = hist.setdefault(r["week_end"], {}).setdefault("kpis", {})
            k["weekly_sales"] = r["sales"]
            if r["refund_rate_4w"] is not None:
                k["return_rate"] = r["refund_rate_4w"]
    for f in (S["funnel"] if S else []):
        hist.setdefault(f["week_end"], {}).setdefault("kpis", {})["search_purchase_share"] = f["Purchases"]
    if C or W:
        snap = hist.setdefault((W or C)["latest"]["week_end"], {})
        # A figure only goes into a week its own data covers; downloads can lag the API.
        week = (W or C)["latest"]["week_end"]
        own = {"repeat_rate": C["latest"]["week_end"] if C else None,
               "search_purchase_share": S["funnel"][-1]["week_end"] if S else None}
        snap["kpis"] = {**snap.get("kpis", {}), **{r["key"]: r["now"] for r in data.get("scorecard", [])
                                                   if own.get(r["key"], week) == week}}
        snap["insights"] = [{k: i.get(k) for k in ("id", "title", "priority", "team", "kpi", "current", "target", "impact")}
                            for i in data.get("insights", [])]
        snap["built"] = data["built"]
    hist = dict(sorted(hist.items()))
    with open(path, "w") as fh:
        json.dump(hist, fh, indent=1, default=float)
    return hist


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["csv", "spapi"], default="csv")
    ap.add_argument("--csv-dir", default=os.path.join(ROOT, "seller_central"))
    ap.add_argument("--config", default=os.path.join(ROOT, "config", "spapi.json"))
    ap.add_argument("--env", default=os.path.join(REPO, ".env"),
                    help="SP_API_* credentials shared with amazon-agents")
    ap.add_argument("--week-end", help="Saturday ending the week to pull (SP-API mode); "
                    "default: the latest Saturday at least 3 days ago, once Brand Analytics has posted it")
    ap.add_argument("--daily-days", type=int, default=60, help="days of sales and refunds to refresh (SP-API mode)")
    ap.add_argument("--dry-run", action="store_true", help="print SP-API requests without sending")
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "dashboard"))
    ap.add_argument("--warehouse", default=os.path.join(REPO, "data", "warehouse.db"))
    a = ap.parse_args()

    csv_tables = sources.CsvSource(a.csv_dir).load()
    api_dir = os.path.join(a.csv_dir, "api_tables")
    if a.source == "spapi":
        cfg = a.config if os.path.exists(a.config) else os.path.join(HERE, "config.example.json")
        week_end = a.week_end or latest_week_end().isoformat()
        # Every new-condition brand ASIN, best sellers first, so the API roll-up
        # covers the same catalogue as the Brand View CSV.
        orders = {}
        for r in csv_tables.get("listing_health", []):
            if r["condition"] == "new":
                orders[r["asin"]] = orders.get(r["asin"], 0) + r["orders"]
        asins = sorted(orders, key=orders.get, reverse=True) or None
        api = sources.SpApiSource(cfg, os.path.join(a.csv_dir, "api_raw"), week_end, asins=asins,
                                  dry_run=a.dry_run, env_path=a.env,
                                  known_titles=sources.titles_from(csv_tables))
        sources.save_api_tables(api_dir, api.load())
        if not a.dry_run:
            sources.save_sales_daily(os.path.join(a.csv_dir, "api_daily"), api.load_daily(a.daily_days))
        if a.dry_run:
            print("dry run: no requests sent; dashboard built from CSV data")

    # Weeks saved from earlier API pulls count in CSV builds too, so a run without keys keeps them.
    tables = sources.merge_weekly(csv_tables, sources.load_api_tables(api_dir))
    tables.update(sources.WarehouseSource(a.warehouse).load())
    tables.update(sources.WalmartSource(os.path.join(ROOT, "walmart")).load())
    tables["account_daily"] = sources.merge_daily(sources.business_report_daily(os.path.join(ROOT, "data")),
                                                  sources.load_sales_daily(os.path.join(a.csv_dir, "api_daily")))
    data = kpis.build(tables, a.source)
    data["built"] = dt.datetime.now().strftime("%b %d, %Y %H:%M")
    os.makedirs(a.out_dir, exist_ok=True)
    data["history"] = update_history(os.path.join(a.out_dir, "history.json"), data)
    with open(os.path.join(a.out_dir, "kpis.json"), "w") as fh:
        json.dump(data, fh, indent=1, default=float)
    with open(os.path.join(HERE, "dashboard_template.html")) as fh:
        html = fh.read().replace("/*__DATA__*/{}", json.dumps(data, default=float).replace("</", "<\\/"))
    out = os.path.join(a.out_dir, "Dagacci_KPI_Dashboard.html")
    with open(out, "w") as fh:
        fh.write(html)
    print("wrote", out)


if __name__ == "__main__":
    main()
