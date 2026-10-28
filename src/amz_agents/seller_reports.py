"""Seller Central reports downloaded by hand, for when SP-API access isn't available.

`amz-agents seller FILE [FILE ...]` reads each file, works out which report it is from
its header, and loads it into the same tables the SP-API ingest fills:

  All Orders (Reports > Fulfillment > All Orders, or Order Reports)
      -> metrics_daily units and revenue by day and SKU, and products (SKU, ASIN, name).
         Ad spend already loaded (e.g. from an agency) is kept.
  Business Report "Detail Page Sales and Traffic by Child Item" (Reports > Business
  Reports), for the last 30 days
      -> asin_traffic: sessions, page views, units and Buy Box % per SKU.
  Manage FBA Inventory (Reports > Fulfillment > Manage FBA Inventory, or Inventory
  Reports)
      -> products units on hand and inbound.
  All Listings (Inventory Reports > All Listings Report)
      -> products SKU, ASIN, name and price.
"""

from __future__ import annotations

import csv
import io
import os
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from .data.warehouse import Warehouse
from .ingest import _decode, update_velocity


def _rows(path: Path) -> list[dict[str, str]]:
    text = _decode(path.read_bytes())
    first = text.split("\n", 1)[0]
    delim = "\t" if first.count("\t") >= first.count(",") else ","
    return [{_key(k): (v or "").strip() for k, v in r.items() if k}
            for r in csv.DictReader(io.StringIO(text), delimiter=delim)]


def _key(k: str) -> str:
    """Header names as Amazon spells them, minus BOMs, quotes and dash variants."""
    return re.sub(r"\s+", " ", k.replace("﻿", "").replace("–", "-").replace("—", "-")
                  .strip().strip('"')).lower()


def _num(v: str | None) -> float:
    v = re.sub(r"[^0-9.\-]", "", v or "")
    return float(v) if v not in ("", "-", ".") else 0.0


def kind(rows: list[dict[str, str]]) -> str | None:
    cols = set(rows[0]) if rows else set()
    if {"amazon-order-id", "purchase-date", "sku"} <= cols:
        return "orders"
    if any(c.startswith("sessions") for c in cols) and ("(child) asin" in cols or "sku" in cols):
        return "traffic"
    if {"sku", "afn-fulfillable-quantity"} <= cols:
        return "inventory"
    if {"seller-sku", "asin1"} <= cols:
        return "listings"
    return None


def _upsert_products(wh: Warehouse, rows: list[dict[str, Any]]) -> None:
    """Add or update products without wiping costs, stock or other columns; a blank
    field in a report doesn't overwrite what's known."""
    wh.upsert("products", [{c: v for c, v in r.items() if v not in (None, "")} for r in rows],
              ["sku"])


def load_orders(wh: Warehouse, rows: list[dict[str, str]],
                today: date | None = None) -> dict[str, Any]:
    """Only the marketplace in SELLER_SALES_CHANNEL (default Amazon.com): other
    marketplaces are in their own currency (MXN, CAD...), and "Non-Amazon" lines are
    other channels' orders shipped from FBA stock, counted by those channels. Today is
    left out: a report pulled mid-day would read as a sales drop."""
    today_iso = (today or date.today()).isoformat()
    channel = os.getenv("SELLER_SALES_CHANNEL", "Amazon.com")
    by_day: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0, 0.0])
    names: dict[str, dict[str, str]] = {}
    skipped: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.get("order-status") == "Cancelled" or not r.get("sku"):
            continue
        if r.get("sales-channel") and r["sales-channel"] != channel:
            skipped[r["sales-channel"]] += 1
            continue
        if r["purchase-date"][:10] >= today_iso:
            skipped["today (partial)"] += 1
            continue
        d = by_day[(r["purchase-date"][:10], r["sku"])]
        d[0] += int(_num(r.get("quantity")))
        d[1] += _num(r.get("item-price"))  # blank while pending
        names.setdefault(r["sku"], {"sku": r["sku"], "asin": r.get("asin", ""),
                                    "title": r.get("product-name", "")})
    _upsert_products(wh, list(names.values()))
    wh.upsert("metrics_daily", [{"date": d, "sku": s, "units": int(u), "revenue": round(rev, 2)}
                                for (d, s), (u, rev) in sorted(by_day.items())], ["date", "sku"])
    days = sorted({d for d, _ in by_day})
    return {"rows": len(by_day), "skus": len(names), "channel": channel,
            "skipped": dict(skipped),
            "from": days[0] if days else None, "to": days[-1] if days else None,
            "units": int(sum(u for u, _ in by_day.values())),
            "revenue": round(sum(r for _, r in by_day.values()), 2)}


def load_traffic(wh: Warehouse, rows: list[dict[str, str]]) -> dict[str, Any]:
    # Several SKUs can share an ASIN (Amazon's amzn.gr.* resale SKUs, relabels): the
    # page's traffic belongs to the one that sells the most.
    asin_to_sku: dict[str, str] = {}
    for p in wh.query(
            "SELECT p.sku, p.asin, COALESCE(SUM(m.units), 0) AS units FROM products p "
            "LEFT JOIN metrics_daily m ON m.sku = p.sku WHERE p.asin IS NOT NULL AND "
            "p.asin != '' GROUP BY p.sku ORDER BY p.sku LIKE 'amzn.gr.%', units DESC"):
        asin_to_sku.setdefault(p["asin"], p["sku"])

    def col(r: dict[str, str], *names: str) -> str:
        return next((r[n] for n in names if r.get(n) not in (None, "")), "")

    out, unmatched = [], 0
    for r in rows:
        sku = r.get("sku") or asin_to_sku.get(col(r, "(child) asin", "asin"))
        if not sku:
            unmatched += 1
            continue
        bb = col(r, "featured offer (buy box) percentage", "buy box percentage",
                 "featured offer percentage")
        out.append({
            "sku": sku,
            "sessions_30d": int(_num(col(r, "sessions - total", "sessions"))),
            "page_views_30d": int(_num(col(r, "page views - total", "page views"))),
            "units_30d": int(_num(col(r, "units ordered"))),
            "buy_box_pct": _num(bb) / 100 if bb else None,
        })
    wh.insert_many("asin_traffic", out)
    return {"rows": len(out), "unmatched": unmatched,
            "note": "ASINs with no SKU yet; load an All Orders or All Listings report first"
            if unmatched else ""}


def load_inventory(wh: Warehouse, rows: list[dict[str, str]]) -> dict[str, Any]:
    _upsert_products(wh, [{"sku": r["sku"], "asin": r.get("asin", ""),
                           "title": r.get("product-name", "")} for r in rows if r.get("sku")])
    n = 0
    for r in rows:
        inbound = sum(_num(r.get(k)) for k in ("afn-inbound-working-quantity",
                                                "afn-inbound-shipped-quantity",
                                                "afn-inbound-receiving-quantity"))
        n += wh.conn.execute("UPDATE products SET units_on_hand=?, units_inbound=? WHERE sku=?",
                             (int(_num(r.get("afn-fulfillable-quantity"))), int(inbound),
                              r["sku"])).rowcount
    wh.conn.commit()
    return {"rows": n}


def load_listings(wh: Warehouse, rows: list[dict[str, str]]) -> dict[str, Any]:
    items = [{"sku": r["seller-sku"], "asin": r.get("asin1", ""), "title": r.get("item-name", ""),
              "price": _num(r.get("price")) or None} for r in rows if r.get("seller-sku")]
    _upsert_products(wh, items)
    return {"rows": len(items)}


LOADERS = {"orders": load_orders, "traffic": load_traffic, "inventory": load_inventory,
           "listings": load_listings}
ORDER = ["listings", "orders", "inventory", "traffic"]  # traffic needs ASIN -> SKU


def load(wh: Warehouse, paths: list[str | Path]) -> list[dict[str, Any]]:
    """Load each report; returns one summary per file."""
    found = []
    for p in map(Path, paths):
        rows = _rows(p)
        found.append((p, kind(rows), rows))
    found.sort(key=lambda f: ORDER.index(f[1]) if f[1] in ORDER else len(ORDER))
    out = []
    for p, k, rows in found:
        if not k:
            out.append({"file": p.name, "report": None, "error": "not a report I recognize"})
            continue
        out.append({"file": p.name, "report": k, **LOADERS[k](wh, rows)})
    update_velocity(wh)
    return out
