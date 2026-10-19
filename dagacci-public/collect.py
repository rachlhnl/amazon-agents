"""Collect public data on Dagacci from dagacci.com into snapshot.json.

Sources, all public and unauthenticated:
- the store's catalog feed (/products.json): titles, prices, variants, stock flags
- each product page's review widget (Judge.me): average rating and review count

Run:  python dagacci-public/collect.py  (then build.py to render the dashboard)
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

STORE = "https://www.dagacci.com"
HERE = Path(__file__).parent
UA = "Mozilla/5.0 (compatible; amz-agents public-data collector)"

RATING = re.compile(r"data-average-rating='([0-9.]+)'")
COUNT = re.compile(r"data-number-of-reviews='([0-9]+)'")

# Title keywords -> product line, checked in order.
LINES = [
    ("Other brands", ("barco", "cherokee")),
    ("Lab coats & jackets", ("lab coat", "jacket")),
    ("Accessories", ("shield", "socks", "cap", "shoes", "embroidery")),
    ("Scrub sets", ("set",)),
    ("Tops", ("top", "tee")),
    ("Pants", ("pant",)),
]


def product_line(title: str) -> str:
    t = title.lower()
    for line, words in LINES:
        if any(w in t for w in words):
            return line
    return "Other"


def reviews(client: httpx.Client, handle: str) -> tuple[float | None, int]:
    html = client.get(f"{STORE}/products/{handle}").text
    # The first widget on the page belongs to the product; later ones are carousels.
    ratings = [float(r) for r in RATING.findall(html) if float(r) > 0]
    counts = [int(c) for c in COUNT.findall(html)]
    return (ratings[0] if ratings else None), (counts[0] if counts else 0)


def collect() -> dict:
    products = []
    with httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as client:
        feed = client.get(f"{STORE}/products.json", params={"limit": 250}).json()["products"]
        for p in feed:
            variants = p["variants"]
            prices = [float(v["price"]) for v in variants]
            compare = [float(v["compare_at_price"] or 0) for v in variants]
            colors = next((len(o["values"]) for o in p["options"] if o["name"] == "Color"), 1)
            rating, count = reviews(client, p["handle"])
            time.sleep(0.5)  # be polite to the store
            products.append({
                "title": " ".join(p["title"].split()),
                "url": f"{STORE}/products/{p['handle']}",
                "line": product_line(p["title"]),
                "price": min(prices),
                "price_max": max(prices),
                "compare_at": max(compare) or None,
                "variants": len(variants),
                "in_stock": sum(1 for v in variants if v.get("available")),
                "colors": colors,
                "images": len(p["images"]),
                "created": p["created_at"][:10],
                "rating": rating,
                "reviews": count,
            })
    return {
        "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": STORE,
        "products": products,
    }


if __name__ == "__main__":
    snap = collect()
    out = HERE / "snapshot.json"
    out.write_text(json.dumps(snap, indent=2) + "\n")
    print(f"{len(snap['products'])} products written to {out}")
