"""Keep the dashboard's Walmart data current from the Walmart Marketplace API.

    python3 -I walmart_api.py check            # sign in and count WFS items; changes nothing
    python3 -I walmart_api.py pull             # refresh the last 30 days of walmart_snapshot.json
    python3 -I walmart_api.py pull --days 90   # go further back (Walmart keeps 2 years)

Keys come from .env (WALMART_CLIENT_ID, WALMART_CLIENT_SECRET; see walmart_setup.sh).

This only reads. The sole POSTs allowed are the sign-in and asking Walmart to prepare a
report, so a bug here can't change a price, an order or an item. It uses the Item
Performance report and WFS inventory, neither of which holds customer details. It
deliberately doesn't use the Orders API, which returns names and addresses.

The first pull must match the figures read by hand from Seller Center, or the snapshot is
left alone. A failed pull also leaves it alone, so the tab keeps its last good data.
"""
import argparse
import base64
import collections
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sources import _read_env

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(HERE)
ROOT = os.path.expanduser(os.environ.get("DAGACCI_DATA", "~/Documents/dagacci"))
BASE = "https://marketplace.walmartapis.com"
POST_ALLOWED = ("/v3/token", "/v3/reports/reportRequests")
PACIFIC = ZoneInfo("America/Los_Angeles")  # Seller Center reports in Pacific time
DAILY_WINDOW_DAYS = 15                     # Walmart's limit for one DAILY report
RECONCILE_TOLERANCE = 0.02                 # the first pull must be within 2% of the hand read
RECONCILE_SETTLE_DAYS = 3                  # recent days still move as refunds post
AT_RISK_DAYS = 30


class WalmartError(Exception):
    pass


class Api:
    def __init__(self, client_id, client_secret, opener=urllib.request.urlopen, sleep=time.sleep):
        self.client_id, self.client_secret = client_id, client_secret
        self.opener, self.sleep = opener, sleep
        self.token, self.token_at = None, 0.0

    def _send(self, method, path, query=None, body=None, headers=None, tries=6):
        if method != "GET" and path not in POST_ALLOWED:
            raise WalmartError(f"refusing {method} {path}: this client only reads")
        url = BASE + path + ("?" + urllib.parse.urlencode(query) if query else "")
        hdrs = {"WM_SVC.NAME": "Walmart Marketplace", "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
                "Accept": "application/json", **(headers or {})}
        for attempt in range(tries):
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            try:
                with self.opener(req, timeout=60) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                    # Status and path only: bodies aren't needed here and tokens never appear in them.
                    raise WalmartError(f"{method} {path} returned HTTP {e.code}") from None
                self.sleep(min(300, 15 * 2 ** attempt))
            except urllib.error.URLError as e:
                if attempt == tries - 1:
                    raise WalmartError(f"{method} {path} failed: {e.reason}") from None
                self.sleep(min(300, 15 * 2 ** attempt))

    def _auth(self):
        if self.token and time.time() - self.token_at < 600:  # tokens last 15 minutes
            return self.token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        raw = self._send("POST", "/v3/token", body=b"grant_type=client_credentials", headers={
            "Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"})
        self.token, self.token_at = json.loads(raw)["access_token"], time.time()
        return self.token

    def call(self, method, path, query=None, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"WM_SEC.ACCESS_TOKEN": self._auth()}
        if body:
            headers["Content-Type"] = "application/json"
        return json.loads(self._send(method, path, query, body, headers))

    def download(self, url):
        """Report files come from a time-limited link that takes no Walmart headers."""
        with self.opener(urllib.request.Request(url), timeout=300) as resp:
            return resp.read()


def windows(start, end, size=DAILY_WINDOW_DAYS):
    """Split start..end (inclusive dates) into spans Walmart accepts in one DAILY report."""
    out, d = [], start
    while d <= end:
        last = min(d + dt.timedelta(days=size - 1), end)
        out.append((d, last))
        d = last + dt.timedelta(days=1)
    return out


def fetch_item_performance(api, start, end, poll_seconds=60, timeout_seconds=5400):
    """Ask for every window at once, then wait: Walmart takes 15-45 minutes per report."""
    pending = {}
    for a, b in windows(start, end):
        doc = api.call("POST", "/v3/reports/reportRequests",
                       {"reportType": "ITEM_PERFORMANCE", "reportVersion": "v3"},
                       {"dataStartTime": f"{a}T00:00:00Z", "dataEndTime": f"{b}T23:59:59Z",
                        "dateGranularity": "DAILY", "format": "CSV"})
        pending[doc["requestId"]] = (a, b)
    rows, waited = [], 0
    while pending:
        for rid in list(pending):
            status = api.call("GET", f"/v3/reports/reportRequests/{rid}").get("requestStatus")
            if status == "READY":
                link = api.call("GET", "/v3/reports/downloadReport", {"requestId": rid})["downloadURL"]
                rows += parse_report(api.download(link))
                del pending[rid]
            elif status == "ERROR":
                a, b = pending[rid]
                raise WalmartError(f"Walmart couldn't build the report for {a} to {b}")
        if pending:
            if waited >= timeout_seconds:
                raise WalmartError(f"{len(pending)} report(s) weren't ready after {timeout_seconds // 60} minutes")
            api.sleep(poll_seconds)
            waited += poll_seconds
    return rows


def parse_report(data):
    if data[:2] == b"PK":  # some reports arrive zipped
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            data = z.read(z.namelist()[0])
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def _num(v):
    try:
        return float(str(v).replace("$", "").replace(",", "").replace("%", "").strip())
    except ValueError:
        return 0.0


def _day(v):
    s = str(v).strip()
    for fmt, n in (("%Y-%m-%d", 10), ("%m/%d/%Y", 10)):
        try:
            return dt.datetime.strptime(s[:n], fmt).date().isoformat()
        except ValueError:
            pass
    raise WalmartError("the report has a date format this script doesn't know")


REQUIRED = ("Data Start Time", "SKU ID", "GMV", "GMV-Commission", "Total Units", "Authorized Sales",
            "Authorized Orders", "Cancelled Sales", "Refunded Sales")


def summarize(rows):
    """Item Performance rows (one per SKU per day) -> daily totals and per-SKU totals."""
    if not rows:
        raise WalmartError("the report came back empty")
    missing = [c for c in REQUIRED if c not in rows[0]]
    if missing:
        raise WalmartError("the report is missing columns: " + ", ".join(missing))
    days = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        days[_day(r["Data Start Time"])].update({
            "gmv": _num(r["GMV"]), "net": _num(r["GMV-Commission"]), "units": _num(r["Total Units"]),
            "orders": _num(r["Authorized Orders"]), "auth": _num(r["Authorized Sales"]),
            "cancelled": _num(r["Cancelled Sales"]), "refunds": _num(r["Refunded Sales"])})
    daily = [{"date": d, **{k: round(c[k], 2) for k in ("gmv", "net", "units", "orders", "auth", "cancelled", "refunds")}}
             for d, c in sorted(days.items())]
    return daily


def top_items(rows, n=25):
    sku = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        s = r["SKU ID"]
        sku[s].update({"gmv": _num(r["GMV"]), "units": _num(r["Total Units"]), "orders": _num(r["Authorized Orders"]),
                       "page_views": _num(r.get("Product Level Pageviews", 0)), "refunds": _num(r["Refunded Sales"]),
                       "auth_units": _num(r.get("Authorized Units", 0)) or _num(r["Authorized Orders"])})
    out = []
    for s, c in sku.items():
        views = c["page_views"]
        out.append({"sku": s, "gmv": round(c["gmv"], 2), "units": int(c["units"]), "orders": int(c["orders"]),
                    "page_views": int(views), "conversion": round(c["auth_units"] / views * 100, 2) if views else 0.0,
                    "refunds": round(c["refunds"], 2)})
    out.sort(key=lambda t: t["gmv"], reverse=True)
    return out[:n], len(out), {s: c["units"] for s, c in sku.items()}, {s: c["gmv"] for s, c in sku.items()}


def fetch_wfs_inventory(api, page=300):
    out, offset = [], 0
    while True:
        doc = api.call("GET", "/v3/wfs/inventory", {"limit": page, "offset": offset})
        got = (doc.get("payload") or {}).get("inventory") or []
        out += got
        offset += len(got)
        if not got or offset >= (doc.get("headers") or {}).get("totalCount", 0):
            return out


def stock_rows(inventory, units_by_sku, gmv_by_sku, days):
    """Stock status by our own rule: out with nothing available; at risk under 30 days of
    supply, counting inbound. It's close to Walmart's labels but won't always match them."""
    rows, at_risk_or_out = [], 0
    for it in inventory:
        sku = (it.get("itemInformation") or {}).get("sku")
        d = it.get("inventoryData") or {}
        if not sku:
            continue
        avail, inbound = int(d.get("availableUnits") or 0), int(d.get("inboundUnits") or 0)
        per_day = max(0.0, units_by_sku.get(sku, 0) / days)
        supply = round((avail + inbound) / per_day) if per_day else None
        status = "out" if avail <= 0 else "at_risk" if supply is not None and supply < AT_RISK_DAYS else "in_stock"
        if status != "in_stock" and per_day:
            at_risk_or_out += 1
        rows.append({"sku": sku, "status": status, "daily_sales": round(max(0.0, gmv_by_sku.get(sku, 0)) / days, 2),
                     "daily_units": round(per_day, 1), "available": avail, "inbound": inbound,
                     "days_supply": supply, "suggested": None})
    rows.sort(key=lambda r: r["daily_sales"], reverse=True)
    return rows[:25], len(rows), at_risk_or_out


def reconcile(old_daily, new_daily, today):
    """Compare the API with the figures already held, leaving out days still settling.
    Returns (ok, message). Never includes anything but totals."""
    cutoff = (today - dt.timedelta(days=RECONCILE_SETTLE_DAYS)).isoformat()
    old = {r["date"]: r for r in old_daily}
    both = [r for r in new_daily if r["date"] in old and r["date"] <= cutoff]
    if not both:
        return False, "no days overlap with the figures read by hand, so nothing could be checked"
    lines, ok = [f"Compared {len(both)} days, {both[0]['date']} to {both[-1]['date']}:"], True
    for key, label in (("gmv", "GMV"), ("units", "units"), ("auth", "orders placed $")):
        a, b = sum(old[r["date"]][key] for r in both), sum(r[key] for r in both)
        off = abs(b - a) / a if a else (0 if not b else 1)
        ok &= off <= RECONCILE_TOLERANCE
        lines.append(f"  {label:16} hand read {a:12,.2f}   API {b:12,.2f}   off by {off * 100:.1f}%")
    return ok, "\n".join(lines)


def merge(snap, daily, top, top_from, top_to, activity, stock, items_total, at_risk_or_out, now):
    """Replace the pulled days and keep everything older. Figures the API doesn't supply
    (stock age, rating, Walmart's estimates, the written notes) stay, marked with their read date."""
    by_day = {r["date"]: r for r in snap.get("daily", [])}
    by_day.update({r["date"]: r for r in daily})
    out = dict(snap)
    out.update({"daily": [by_day[d] for d in sorted(by_day)], "top_items": top, "top_items_from": top_from,
                "top_items_to": top_to, "items_with_activity": activity, "stock": stock,
                "captured": now, "auto": True, "source": "Walmart Marketplace API: Item Performance report, WFS inventory"})
    out.setdefault("hand_read", snap.get("captured"))
    out["wfs"] = {**snap.get("wfs", {}), "items_total": items_total, "items_at_risk_or_out": at_risk_or_out}
    return out


def save_atomic(path, snap):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(snap, fh, indent=1)
    os.replace(tmp, path)


def pull(api, path, days=30, accept=False, today=None):
    today = today or dt.datetime.now(PACIFIC).date()
    end = today - dt.timedelta(days=1)  # today is still in progress
    start = end - dt.timedelta(days=max(days, 30) - 1)
    rows = fetch_item_performance(api, start, end)
    daily = summarize(rows)
    recent_from = (end - dt.timedelta(days=29)).isoformat()
    recent = [r for r in rows if _day(r["Data Start Time"]) >= recent_from]
    top, activity, units, gmv = top_items(recent)
    stock, items_total, at_risk = stock_rows(fetch_wfs_inventory(api), units, gmv, 30)
    snap = {}
    if os.path.exists(path):
        with open(path) as fh:
            snap = json.load(fh)
    if not snap.get("auto") and snap.get("daily"):
        ok, msg = reconcile(snap["daily"], daily, today)
        print(msg)
        if not ok and not accept:
            raise WalmartError("the API doesn't match the figures read by hand, so the snapshot wasn't changed. "
                               "Review the difference; pull --accept overrides this.")
    save_atomic(path, merge(snap, daily, top, recent_from, end.isoformat(), activity, stock,
                            items_total, at_risk, today.isoformat()))
    print(f"Walmart: updated {len(daily)} days through {daily[-1]['date']}, {items_total} WFS items")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "pull"])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--accept", action="store_true", help="save even if the first pull doesn't match the hand read")
    ap.add_argument("--env", default=os.path.join(REPO, ".env"))
    ap.add_argument("--snapshot", default=os.path.join(ROOT, "walmart", "walmart_snapshot.json"))
    a = ap.parse_args()
    env = _read_env(a.env)
    cid, secret = env.get("WALMART_CLIENT_ID"), env.get("WALMART_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("No Walmart keys in .env. Run: zsh dashboard/walmart_setup.sh keys")
    api = Api(cid, secret)
    try:
        if a.command == "check":
            doc = api.call("GET", "/v3/wfs/inventory", {"limit": 1, "offset": 0})
            print(f"Signed in to Walmart. WFS items: {(doc.get('headers') or {}).get('totalCount', 'unknown')}")
        else:
            pull(api, a.snapshot, a.days, a.accept)
    except WalmartError as e:
        sys.exit(f"Walmart: {e}")


if __name__ == "__main__":
    main()
