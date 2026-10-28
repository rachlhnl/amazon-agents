"""Assumed unit economics for products with no real cost, fee or lead time yet.

Without them every agent skips its maths: no reorders for out-of-stock best sellers, no
profit, no impact estimate to rank work by. So, before the agents run, blanks are filled
from the brand's own averages and the field names are recorded in products.assumed:

  fees            ASSUMED_FEES_PCT of price. Default 48.7%: FBA fulfilment (34.3%) plus
                  selling fees (14.4%) as a share of Amazon sales, Sep 16 - Oct 15 2026.
  landed_cost     ASSUMED_LANDED_COST_PCT of price (default 20%) until COSTS_CSV is loaded.
  lead_time_days  ASSUMED_LEAD_TIME_DAYS (default 45): factory to sellable at Amazon.

Real data replaces an assumed value (COSTS_CSV, SP-API fees) and removes its name from
`assumed`. Agents that change prices skip products whose costs are assumed.
"""

from __future__ import annotations

import os

from .data.warehouse import Warehouse

FIELDS = ("fees", "landed_cost", "lead_time_days")


def defaults() -> dict[str, float]:
    return {"fees": float(os.getenv("ASSUMED_FEES_PCT", "0.487")),
            "landed_cost": float(os.getenv("ASSUMED_LANDED_COST_PCT", "0.20")),
            "lead_time_days": float(os.getenv("ASSUMED_LEAD_TIME_DAYS", "45"))}


def apply(wh: Warehouse) -> int:
    """Fill blanks; returns how many products use at least one assumed value."""
    d = defaults()
    for f in FIELDS:
        value = "ROUND(price * ?, 2)" if f != "lead_time_days" else "CAST(? AS INTEGER)"
        wh.conn.execute(
            f"UPDATE products SET {f} = {value}, assumed = TRIM(COALESCE(assumed, '') || ' {f}') "
            f"WHERE {f} IS NULL AND (price > 0 OR '{f}' = 'lead_time_days')", (d[f],))
    wh.conn.commit()
    return wh.query("SELECT COUNT(*) AS n FROM products WHERE COALESCE(assumed, '') != ''")[0]["n"]


def clear(wh: Warehouse, sku: str, *fields: str) -> None:
    """A real value arrived for these fields of this SKU."""
    row = wh.query("SELECT assumed FROM products WHERE sku=?", (sku,))
    if row and row[0]["assumed"]:
        left = " ".join(f for f in row[0]["assumed"].split() if f not in fields)
        wh.conn.execute("UPDATE products SET assumed=? WHERE sku=?", (left or None, sku))


def is_assumed(p: dict, *fields: str) -> bool:
    have = (p.get("assumed") or "").split()
    return any(f in have for f in (fields or FIELDS))
