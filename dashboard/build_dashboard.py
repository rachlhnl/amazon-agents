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
    C, S = data.get("customers"), data.get("search")
    for r in (C["series"] if C else []):
        hist.setdefault(r["week_end"], {}).setdefault("kpis", {}).update(
            {"weekly_sales": r["sales"], "repeat_rate": r["repeat_rate"]})
    for f in (S["funnel"] if S else []):
        hist.setdefault(f["week_end"], {}).setdefault("kpis", {})["search_purchase_share"] = f["Purchases"]
    if C:
        snap = hist.setdefault(C["latest"]["week_end"], {})
        snap["kpis"] = {**snap.get("kpis", {}), **{r["key"]: r["now"] for r in data.get("scorecard", [])}}
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
    ap.add_argument("--dry-run", action="store_true", help="print SP-API requests without sending")
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "dashboard"))
    ap.add_argument("--warehouse", default=os.path.join(REPO, "data", "warehouse.db"))
    a = ap.parse_args()

    csv_tables = sources.CsvSource(a.csv_dir).load()
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
                                  dry_run=a.dry_run, env_path=a.env)
        tables = {**csv_tables, **api.load()}
        if a.dry_run:
            print("dry run: no requests sent; dashboard built from CSV data")
    else:
        tables = csv_tables

    tables.update(sources.WarehouseSource(a.warehouse).load())
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
