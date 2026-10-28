"""Which style (parent listing) each SKU belongs to, so agents can report one piece of
work per style instead of one card per size and color.

Parent ASIN from an agency export (ad_products) when known; otherwise the SKU stem
(KV_10005000_NVY_M -> KV_10005000); otherwise the product name up to its first comma
or bracket.
"""

from __future__ import annotations

import re
from typing import Any

from .data.warehouse import Warehouse

_BRAND = re.compile(r"^Dagacci( Medical Uniform| Scrubs Medical Uniform)?®?\s*", re.IGNORECASE)


def name_of(title: str | None, n: int = 60) -> str:
    t = re.split(r"[,(]", _BRAND.sub("", title or ""))[0].strip()
    return t[:n] + ("…" if len(t) > n else "")


def key_of(p: dict[str, Any], parents: dict[str, str]) -> str:
    if p.get("asin") and parents.get(p["asin"]):
        return parents[p["asin"]]
    parts = p["sku"].split("_")
    if len(parts) >= 3:
        return "_".join(parts[:-2])
    return name_of(p.get("title")) or p["sku"]


def styles(wh: Warehouse) -> dict[str, tuple[str, str]]:
    """sku -> (style key, style name)."""
    parents = {r["asin"]: r["parent_asin"] for r in wh.query(
        "SELECT DISTINCT asin, parent_asin FROM ad_products WHERE COALESCE(parent_asin, '') != ''")}
    products = wh.query("SELECT sku, asin, title, daily_velocity FROM products "
                        "ORDER BY daily_velocity DESC")
    names: dict[str, str] = {}
    out = {}
    for p in products:
        k = key_of(p, parents)
        names.setdefault(k, name_of(p["title"]) or k)  # named after its best seller
        out[p["sku"]] = (k, names[k])
    return out
