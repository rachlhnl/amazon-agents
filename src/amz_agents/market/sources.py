"""Public market data: brand Shopify stores and Amazon product pages.

Two kinds of source, neither needing the brand's own credentials:

- **Shopify storefronts** publish their catalog at `/products.json` (titles, prices,
  compare-at prices, variants and whether each can be bought). Judge.me review
  widgets on product pages carry an average rating and review count.
- **Amazon products** come from the Selling Partner API when SP_API_* keys are set
  (price and sales rank only; it has no reviews), and otherwise from the public
  product page. Page reads are against Amazon's Conditions of Use and get blocked,
  so they are throttled, capped to the configured ASINs, and allowed to fail: a
  failed read returns `{"asin": ..., "error": ...}` and the analysis keeps the last
  good value.
"""

from __future__ import annotations

import html
import os
import re
import time
from typing import Any

import httpx

UA = "Mozilla/5.0 (compatible; amz-agents market tracker)"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
MAX_PAGES = 40  # 10,000 products; catalogs bigger than that are truncated

_RATING = re.compile(r"data-average-rating='([0-9.]+)'")
_COUNT = re.compile(r"data-number-of-reviews='([0-9]+)'")

# Title keywords -> product line, checked in order.
LINES = [
    ("Lab coats & jackets", ("lab coat", "jacket", "vest", "fleece", "sweatshirt")),
    ("Accessories", ("shield", "sock", "cap", "shoe", "embroidery", "mask", "bag",
                     "badge", "gift", "hat", "headband", "clog")),
    ("Underscrubs", ("underscrub", "tee", "long sleeve")),
    ("Scrub sets", ("set", "bundle")),
    ("Tops", ("top", "shirt", "tunic")),
    ("Pants", ("pant", "jogger", "legging", "skirt", "short", "flare", "wide leg", "cargo",
               "sweatpant")),
]


_LINE_RES = [(line, re.compile(r"\b(" + "|".join(words) + r")s?\b")) for line, words in LINES]


def product_line(title: str) -> str:
    t = title.lower()
    for line, rx in _LINE_RES:
        if rx.search(t):
            return line
    return "Other"


def _client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True)


def judgeme(http: httpx.Client, url: str) -> tuple[float | None, int]:
    """Average rating and review count from a product page's Judge.me widget."""
    page = http.get(url).text
    # The first widget on the page belongs to the product; later ones are carousels.
    ratings = [float(r) for r in _RATING.findall(page) if float(r) > 0]
    counts = [int(c) for c in _COUNT.findall(page)]
    return (ratings[0] if ratings else None), (counts[0] if counts else 0)


def shopify_product(p: dict[str, Any], store: str) -> dict[str, Any]:
    variants = p["variants"] or [{"price": "0", "compare_at_price": None}]
    prices = [float(v["price"]) for v in variants]
    compare = [float(v.get("compare_at_price") or 0) for v in variants]
    color_vals = next((o["values"] for o in p.get("options", [])
                       if o["name"].lower() in ("color", "colour")), [])
    colors = len(color_vals) or 1
    return {
        "id": p["id"],
        "title": " ".join(p["title"].split()),
        "url": f"{store}/products/{p['handle']}",
        "line": product_line(p["title"] + " " + (p.get("product_type") or "")),
        "vendor": p.get("vendor", "").strip(),
        "price": min(prices),
        "price_max": max(prices),
        "compare_at": max(compare) or None,
        "variants": len(p["variants"]),
        "in_stock": sum(1 for v in p["variants"] if v.get("available")),
        "colors": colors,
        "color_names": color_vals,
        "created": p["created_at"][:10],
    }


def shopify_catalog(domain: str, reviews: bool = False, delay: float = 0.5) -> dict[str, Any]:
    store = f"https://{domain}"
    products: list[dict[str, Any]] = []
    with _client() as http:
        for page in range(1, MAX_PAGES + 1):
            batch = http.get(f"{store}/products.json",
                             params={"limit": 250, "page": page}).json()["products"]
            products += [shopify_product(p, store) for p in batch]
            if len(batch) < 250:
                break
        if reviews:
            for p in products:
                p["rating"], p["reviews"] = judgeme(http, p["url"])
                time.sleep(delay)
    return {"source": store, "products": products}


# --- Amazon -----------------------------------------------------------------

def _text(s: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def _num(s: str) -> int:
    return int(s.replace(",", ""))


def parse_amazon_page(asin: str, page: str) -> dict[str, Any]:
    """Pull the public facts off an Amazon product page."""
    if "validateCaptcha" in page or "/errors/validateCaptcha" in page:
        return {"asin": asin, "error": "captcha"}
    title = re.search(r'id="productTitle"[^>]*>([^<]+)', page)
    if not title:
        return {"asin": asin, "error": "no product on page"}
    out: dict[str, Any] = {"asin": asin, "title": _text(title.group(1))}
    if m := re.search(r'"parentAsin":"([A-Z0-9]{10})"', page):
        out["parent"] = m.group(1)
    if m := re.search(r'"priceAmount":([0-9.]+)', page):
        out["price"] = float(m.group(1))
    if m := re.search(r'id="acrPopover"[^>]*title="([0-9.]+) out of 5', page):
        out["rating"] = float(m.group(1))
    if m := re.search(r'acrCustomerReviewText"[^>]*aria-label="([0-9,]+) [Rr]eview', page):
        out["reviews"] = _num(m.group(1))
    if m := re.search(r"([0-9,]+K?)\+? bought in past month", page):
        v = m.group(1)
        out["bought_past_month"] = _num(v[:-1]) * 1000 if v.endswith("K") else _num(v)
    if m := re.search(r"Best Sellers Rank(.{0,1500}?)(?:Customer Reviews|</table>|</ul>)", page, re.DOTALL):
        ranks = re.findall(r"#([0-9,]+) in ([^(<#]+)", _text(m.group(1)) + " ")
        out["ranks"] = {cat.strip(): _num(r) for r, cat in ranks}
    out["best_seller_badge"] = "Best Seller<" in page or ">#1 Best Seller" in page
    if m := re.search(r'id="bylineInfo"[^>]*>([^<]+)', page):
        out["byline"] = _text(m.group(1))
    return out


def amazon_page(http: httpx.Client, asin: str) -> dict[str, Any]:
    try:
        r = http.get(f"https://www.amazon.com/dp/{asin}", headers={
            "User-Agent": BROWSER_UA, "Accept": "text/html",
            "Accept-Language": "en-US,en;q=0.9"})
    except httpx.HTTPError as e:
        return {"asin": asin, "error": type(e).__name__}
    if r.status_code != 200:
        return {"asin": asin, "error": f"HTTP {r.status_code}"}
    return parse_amazon_page(asin, r.text)


def spapi_item(sp: Any, asin: str) -> dict[str, Any]:
    """Price and sales rank from SP-API (Catalog Items 2022-04-01, Product Pricing v0).

    Untested against live credentials; verify field paths when keys are first added.
    """
    item = sp.get(f"/catalog/2022-04-01/items/{asin}", marketplaceIds=sp.marketplace_id,
                  includedData="summaries,salesRanks,relationships")
    out: dict[str, Any] = {"asin": asin, "via": "sp-api"}
    if s := (item.get("summaries") or [{}])[0]:
        out["title"] = s.get("itemName")
        out["byline"] = s.get("brand")
    ranks = {}
    for group in item.get("salesRanks") or []:
        for r in group.get("classificationRanks", []) + group.get("displayGroupRanks", []):
            ranks[r["title"]] = r["rank"]
    if ranks:
        out["ranks"] = ranks
    for rel in item.get("relationships") or []:
        for r in rel.get("relationships", []):
            if r.get("parentAsins"):
                out["parent"] = r["parentAsins"][0]
    offers = sp.get(f"/products/pricing/v0/items/{asin}/offers",
                    MarketplaceId=sp.marketplace_id, ItemCondition="New")
    summary = (offers.get("payload") or {}).get("Summary") or {}
    for key in ("BuyBoxPrices", "LowestPrices"):
        if prices := summary.get(key):
            out["price"] = float(prices[0]["LandedPrice"]["Amount"])
            break
    return out


def amazon_items(asins: list[str], delay: float = 4.0) -> list[dict[str, Any]]:
    """One record per ASIN. SP-API supplies price/rank when configured; the page
    read supplies rating, reviews and 'bought in past month' (SP-API has none)."""
    sp = None
    if os.getenv("SP_API_REFRESH_TOKEN"):
        from ..data.clients import SPAPIClient

        sp = SPAPIClient.from_env()
    out = []
    with _client() as http:
        for i, asin in enumerate(asins):
            if i:
                time.sleep(delay)
            rec = amazon_page(http, asin)
            if sp is not None:
                try:
                    rec = {**rec, **spapi_item(sp, asin)}
                    rec.pop("error", None)
                except Exception as e:  # noqa: BLE001 - keep the page data if SP-API fails
                    rec.setdefault("sp_error", str(e)[:200])
            out.append(rec)
            if rec.get("error") == "captcha":
                # Blocked: don't keep knocking. Mark the rest and stop.
                out += [{"asin": a, "error": "skipped after captcha"} for a in asins[i + 1:]]
                break
    return out
