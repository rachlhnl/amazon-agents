"""Mystery shopper: visits each brand's store like a customer would and says how
shopping there feels, what's useful, what's missing, and where they'd buy.

The shopper is a working nurse buying scrubs for 12-hour shifts, on their phone.
On each brand store it opens the product a shopper is likeliest to land on (a
scrub set if the brand sells them, else a scrub top), that product's details,
and the shipping and returns policies, then checks what a buyer looks for:
a size guide, fabric, care, pockets, lengths, reviews, returns, shipping, ways to
pay, help, and how heavy the page is on a phone. Amazon listings are judged from
what the market tracker already read (price, rating, reviews), not visited again.
Walmart isn't visited: it blocks automated visits and its terms forbid them.

Every judgment comes from the checks, so it's repeatable week to week. With an
Anthropic key the write-up is in the shopper's own words (Claude, given only the
checks and page excerpts); without one it's built from the checks directly.
"""

from __future__ import annotations

import html as htmllib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import median
from typing import Any

import httpx

from . import ROOT, history, load_config
from .analyze import amazon_listings, amazon_metrics, store_metrics

UA = "Mozilla/5.0 (compatible; amz-agents mystery shopper)"
DELAY = 2.0  # seconds between requests to the same store
EVERY_DAYS = 7  # how often the daily workflow re-shops
HEAVY_KB = 3000  # page weight a shopper on a phone notices
LIGHT_KB = 1500
CATEGORIES = ["Ease of shopping", "Product information", "Trust & policies", "Price & value"]
PERSONA = ("a working nurse buying two scrub sets for 12-hour shifts, shopping on their "
           "phone between shifts; cares most about fit, comfort, pockets and price, and "
           "hates guessing sizes or paying return shipping")


@dataclass(frozen=True)
class Check:
    key: str
    category: str
    weight: int  # how much a shopper cares (1-3)
    yes: str  # what a shopper likes when it's there
    no: str  # what a shopper misses when it isn't
    fix: str  # what to change
    pattern: str  # regex; matched against page text unless `markup`
    markup: bool = False  # match the raw HTML (apps and widgets)


CHECKS = [
    Check("size_guide", "Product information", 3, "Size guide right on the product page",
          "No size guide on the product page", "Put the size chart on every product page",
          r"size (chart|guide)"),
    Check("fit_tool", "Ease of shopping", 2, "A fit finder suggests my size",
          "Nothing to help me pick a size", "Add a fit finder (height, weight, usual size)",
          r"true ?fit|fit (finder|quiz|predictor)|kiwi ?sizing|find (my|your) size", markup=True),
    Check("fabric", "Product information", 3, "Fabric blend is spelled out",
          "Doesn't say what the fabric is made of",
          "List the fabric blend (e.g. 72% polyester, 21% rayon, 7% spandex)",
          r"\d{1,3}\s?%\s?(polyester|cotton|spandex|rayon|elastane|nylon|lyocell|tencel)"),
    Check("care", "Product information", 1, "Care instructions included",
          "No washing instructions", "Add care instructions",
          r"machine wash|tumble dry|care instructions|wash cold"),
    Check("model", "Product information", 2, "Says what size the model wears",
          "No model height or size to compare against",
          "Say the model's height and the size they wear",
          r"model (is|wears|is wearing)|model'?s? height|wearing (a )?size"),
    Check("pockets", "Product information", 2, "Pocket count is clear",
          "Doesn't say how many pockets", "State the pocket count in the title or bullets",
          r"\d+[- ]pockets?"),
    Check("lengths", "Ease of shopping", 2, "Petite and tall lengths",
          "No petite or tall lengths", "Offer or call out petite and tall lengths",
          r"\b(petite|tall)\b"),
    Check("reviews", "Trust & policies", 3, "Customer reviews on the page",
          "No customer reviews on the page", "Show reviews on the product page",
          r"judge\.?me|jdgm|yotpo|okendo|stamped\.io|reviews\.io|junip|loox|powerreviews|bazaarvoice",
          markup=True),
    Check("free_returns", "Trust & policies", 3, "Free returns or exchanges",
          "Returns aren't free", "Offer free exchanges at least, so sizing isn't a gamble",
          r"free (returns?|exchanges?)|returns? (are|is) free|free (return|exchange) shipping"),
    Check("return_window", "Trust & policies", 2, "A clear return window",
          "Couldn't find how long I have to return", "State the return window on the page",
          r"\b\d{2,3}[- ]?days?\b[^.]{0,40}\b(returns?|exchanges?|refund)|\b(returns?|exchanges?)"
          r"\b[^.]{0,40}\b\d{2,3}[- ]?days?\b"),
    Check("free_shipping", "Price & value", 2, "Free shipping is offered",
          "No free shipping", "Offer free shipping over a set amount",
          r"free (standard )?shipping"),
    Check("pay_later", "Ease of shopping", 1, "Can split the payment",
          "No pay-later option", "Add Shop Pay Installments, Afterpay or Klarna",
          r"afterpay|klarna|shop ?pay installments|sezzle|affirm|pay over time|shopify-payment-terms|"
          r"pay in \d+ (interest-free )?installments", markup=True),
    Check("help", "Ease of shopping", 1, "Live chat if I have a question",
          "No live chat", "Add live chat or a help widget",
          r"gorgias|zendesk|tidio|intercom|livechat|re:?amaze|richpanel|gladly|kustomer",
          markup=True),
]
_RE = {c.key: re.compile(c.pattern, re.IGNORECASE) for c in CHECKS}


# --------------------------------------------------------------------- visiting

def page_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", html))).strip()


_NOT_SCRUBS = re.compile(r"scrunch|\bcaps?\b|socks?|badge|\bbags?\b|mask|protection|gift",
                        re.IGNORECASE)


def pick_product(products: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The product a shopper is likeliest to land on: the brand's own scrub set (else a
    scrub top) that's in stock, with the widest choice of colors and sizes."""
    vendors = [p.get("vendor") for p in products if p.get("vendor")]
    own = max(set(vendors), key=vendors.count) if vendors else None
    for line in ("Scrub sets", "Tops"):
        pool = [p for p in products if p["line"] == line and p.get("in_stock")
                and (own is None or p.get("vendor") == own) and (p.get("price") or 0) >= 15
                and re.search(r"scrub|\btop\b", p["title"], re.IGNORECASE)
                and not _NOT_SCRUBS.search(p["title"])]
        if pool:
            return max(pool, key=lambda p: ((p.get("reviews") or 0), p["variants"], p["colors"]))
    return None


def _get(http: httpx.Client, url: str) -> tuple[httpx.Response | None, float, str | None]:
    t = time.monotonic()
    try:
        r = http.get(url)
        return r, time.monotonic() - t, None if r.status_code == 200 else f"HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return None, time.monotonic() - t, f"{type(e).__name__}"


def _policy(http: httpx.Client, store: str, name: str) -> str:
    r, _, err = _get(http, f"https://{store}/policies/{name}")
    if err or r is None:
        return ""
    m = re.search(r'(?is)<div class="shopify-policy__body">(.*?)</div>\s*</div>', r.text)
    return page_text(m.group(1) if m else r.text)[:6000]


def visit_store(http: httpx.Client, name: str, store: str,
                products: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"brand": name, "site": "store", "where": store}
    p = pick_product(products)
    if not p:
        return {**out, "error": "no scrub set or top in stock to look at"}
    r, secs, err = _get(http, p["url"])
    if err or r is None:
        return {**out, "error": f"product page: {err}"}
    html = r.text
    time.sleep(DELAY)
    detail: dict[str, Any] = {}
    jr, _, jerr = _get(http, p["url"].split("?")[0] + ".json")
    if not jerr and jr is not None:
        try:
            detail = jr.json().get("product", {})
        except ValueError:
            detail = {}
    time.sleep(DELAY)
    returns = _policy(http, store, "refund-policy")
    time.sleep(DELAY)
    shipping = _policy(http, store, "shipping-policy")

    text = " ".join([page_text(html), page_text(detail.get("body_html") or "")])
    policy = f"{returns} {shipping}"
    found: dict[str, Any] = {}
    for c in CHECKS:
        hay = html if c.markup else text + (" " + policy if c.category == "Trust & policies"
                                            or c.key == "free_shipping" else "")
        m = _RE[c.key].search(hay)
        found[c.key] = (m.group(0)[:80] if not c.markup else m.group(0)) if m else None
    sizes = [x for o in detail.get("options", []) if "size" in o.get("name", "").lower()
             for x in o.get("values", [])]
    days = re.search(r"\b(\d{2,3})[- ]?days?\b", returns)
    ship_min = re.search(r"free (?:standard )?shipping[^.$]{0,40}\$\s?(\d+)", policy + " " + text,
                         re.IGNORECASE)
    return {
        **out,
        "product": {"title": p["title"], "url": p["url"], "price": p["price"],
                    "line": p["line"], "colors": p["colors"], "sizes": sizes,
                    "images": len(detail.get("images", [])) or None,
                    "rating": p.get("rating"), "reviews": p.get("reviews") or None},
        "page_kb": round(len(r.content) / 1024), "load_s": round(secs, 1),
        "found": found,
        "return_days": int(days.group(1)) if days else None,
        "free_ship_over": int(ship_min.group(1)) if ship_min else None,
        "excerpt": text[:2500],
        "policy_excerpt": returns[:1200],
    }


def view_amazon(name: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """An Amazon listing family as a shopper sees it, from the market tracker's reads."""
    m = amazon_metrics(amazon_listings(items, {}))
    best = max((x for x in items if "error" not in x and x.get("reviews")),
               key=lambda x: x["reviews"], default=None)
    return {"brand": name, "site": "amazon", "where": "Amazon",
            "product": {"title": best["title"], "price": best.get("price"),
                        "rating": best.get("rating"), "reviews": best.get("reviews"),
                        "url": f"https://www.amazon.com/dp/{best['asin']}"} if best else None,
            "median_price": m["median_price"], "rating": m["rating"], "reviews": m["reviews"],
            "badge": any(x.get("best_seller_badge") for x in items),
            "error": None if best else "no Amazon listing could be read"}


# --------------------------------------------------------------------- judging

def score(v: dict[str, Any], peer_price: float | None) -> dict[str, float]:
    """0-10 per category, from the checks (and price and page weight)."""
    if v.get("error"):
        return {}
    if v["site"] == "amazon":
        out = {"Ease of shopping": 8.0}  # Prime checkout and returns are the same for all
        r, n = v.get("rating") or 0, v.get("reviews") or 0
        out["Trust & policies"] = round(min(10, max(0, (r - 3.5) * 6 + min(n, 20000) / 5000)), 1)
        out["Price & value"] = _value(v.get("median_price"), peer_price)
        return out
    have = dict.fromkeys(CATEGORIES, 0)
    need = dict.fromkeys(CATEGORIES, 0)
    for c in CHECKS:
        need[c.category] += c.weight
        have[c.category] += c.weight if v["found"].get(c.key) else 0
    out = {cat: 10 * have[cat] / need[cat] if need[cat] else 5.0 for cat in CATEGORIES}
    kb = v.get("page_kb") or 0
    ease = out["Ease of shopping"] * 0.7 + (10 if kb <= LIGHT_KB else 5 if kb <= HEAVY_KB else 0) * 0.3
    sizes = len(v["product"]["sizes"])  # 0 when sizes sit on separate top/pant pages
    out["Ease of shopping"] = ease * 0.85 + (min(10, sizes * 1.25) if sizes else 5) * 0.15
    if v["product"].get("reviews"):
        out["Trust & policies"] = min(10, out["Trust & policies"] + 1)
    out["Price & value"] = round(out["Price & value"] * 0.4 + _value(v["outfit"], peer_price) * 0.6, 1)
    return {k: round(x, 1) for k, x in out.items()}


def _value(price: float | None, peer: float | None) -> float:
    """10 at half the peer price or less, 5 at the peer price, 0 at twice it."""
    if not price or not peer:
        return 5.0
    return round(max(0, min(10, 5 - 5 * (price / peer - 1) if price >= peer
                            else 5 + 10 * (1 - price / peer))), 1)


def overall(s: dict[str, float]) -> float | None:
    return round(sum(s.values()) / len(s), 1) if s else None


def narrate(v: dict[str, Any]) -> dict[str, Any]:
    """Likes, dislikes and changes from the checks, most important first."""
    if v.get("error"):
        return {"summary": f"Couldn't shop here: {v['error']}.", "likes": [], "dislikes": [],
                "change": []}
    if v["site"] == "amazon":
        p = v["product"] or {}
        likes = ["Prime shipping and Amazon's returns"]
        if (v.get("rating") or 0) >= 4.3:
            likes.append(f"Well rated: {v['rating']:.1f}★ across {v['reviews']:,} reviews")
        if v.get("badge"):
            likes.append("Best Seller badge")
        dislikes = []
        if (v.get("rating") or 5) < 4.2:
            dislikes.append(f"Rating is only {v['rating']:.1f}★")
        dislikes.append("Hard to tell fit and fabric apart from other brands' listings")
        return {"summary": f"{p.get('title', 'The listing')[:70]} at ${p.get('price') or 0:.2f}.",
                "likes": likes, "dislikes": dislikes,
                "change": ["Put the size chart and fabric blend in the main images"]}
    f = v["found"]
    ranked = sorted(CHECKS, key=lambda c: -c.weight)
    likes = [c.yes + (f": {f[c.key]}" if c.key == "fabric" else "") for c in ranked if f.get(c.key)]
    dislikes = [c.no for c in ranked if not f.get(c.key)]
    change = [c.fix for c in ranked if not f.get(c.key)]
    kb = v.get("page_kb") or 0
    if kb > HEAVY_KB:
        dislikes.insert(0, f"The product page is heavy ({kb / 1024:.1f} MB): slow on a phone")
        change.insert(0, "Cut the product page's weight (images, apps) to under 1.5 MB")
    elif kb <= LIGHT_KB:
        likes.append("The page loads light and fast")
    if v.get("return_days"):
        likes.append(f"{v['return_days']}-day returns")
    if v.get("free_ship_over"):
        likes.append(f"Free shipping over ${v['free_ship_over']}")
    p = v["product"]
    if p.get("reviews"):
        likes.insert(0, f"{p['reviews']} reviews at {p['rating']:.1f}★ on this product"
                     if p.get("rating") else f"{p['reviews']} reviews on this product")
    s = v["scores"]
    best, worst = max(s, key=s.get), min(s, key=s.get)
    return {"summary": (f"Shopped {p['title'][:60]} (${p['price']:.2f}). Strongest on "
                        f"{best.lower()}, weakest on {worst.lower()}."),
            "likes": likes[:5], "dislikes": dislikes[:5], "change": change[:3]}


def verdict(visits: list[dict[str, Any]], self_name: str) -> dict[str, Any]:
    ranked = sorted((v for v in visits if v.get("overall") is not None),
                    key=lambda v: -v["overall"])
    if not ranked:
        return {"choice": None, "why": "No store could be shopped."}
    top = ranked[0]
    ours = [v for v in ranked if v["brand"] == self_name]
    where = "on Amazon" if top["site"] == "amazon" else "on its own site"
    def items(xs: list[str]) -> str:
        return "; ".join(x[0].lower() + x[1:] for x in xs if x)

    def price(v: dict[str, Any]) -> float | None:
        return v.get("outfit") if v["site"] == "store" else v.get("median_price")

    if top["brand"] == self_name:
        why = (f"{self_name} {where} scores best overall ({top['overall']}/10): "
               + items(top["review"]["likes"][:3]) + ".")
    else:
        mine = max(ours, key=lambda v: v["overall"]) if ours else None
        gap = ""
        if mine:
            gap = (f" {self_name}'s best is {mine['overall']}/10 "
                   f"({'Amazon' if mine['site'] == 'amazon' else 'its site'}): "
                   + items(mine["review"]["dislikes"][:2]) + ".")
            a, b = price(top), price(mine)
            if a and b and a > b * 1.15:
                gap += (f" {self_name} is the cheaper buy (${b:.2f} vs ${a:.2f} a set), so a "
                        "shopper on a tight budget would still pick it.")
        why = (f"{top['brand']} {where} ({top['overall']}/10): "
               + items(top["review"]["likes"][:3]) + "." + gap)
    return {"choice": top["brand"], "site": top["site"], "score": top["overall"], "why": why,
            "ranking": [{"brand": v["brand"], "site": v["site"], "overall": v["overall"]}
                        for v in ranked]}


def llm_review(visits: list[dict[str, Any]], model: str) -> dict[str, Any] | None:
    """The shopper's own words, from the checks and excerpts only. None without a key."""
    from .. import llm

    if not llm.available():
        return None
    facts = [{k: v.get(k) for k in ("brand", "site", "product", "found", "page_kb", "return_days",
                                     "free_ship_over", "scores", "overall", "excerpt",
                                     "policy_excerpt", "median_price", "rating", "reviews",
                                     "error")} for v in visits]
    out = llm.complete(
        f"You are {PERSONA}. You just shopped these scrub brands' sites and Amazon listings. "
        "Be objective and specific; judge only from the facts given (they are what you saw). "
        'Return only JSON: {"sites": [{"brand": str, "site": "store"|"amazon", '
        '"summary": str, "likes": [str], "dislikes": [str], "change": [str]}], '
        '"verdict": {"choice": str, "site": str, "why": str}}. 2-4 items per list, one '
        "short sentence each, in your voice as a shopper.",
        json.dumps(facts)[:60000], model, max_tokens=3000)
    if not out:
        return None
    try:
        return json.loads(out.strip().removeprefix("```json").strip("`\n "))
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------- running

def shop(root: Path = ROOT, log=print, model: str | None = None) -> dict[str, Any]:
    snaps = history(root)
    if not snaps:
        raise RuntimeError("no market snapshot yet; run amz-agents market first")
    snap, config = snaps[-1], load_config(root)
    self_key = next((k for k, c in config.items() if c.get("role") == "self"), None)
    self_name = config[self_key]["name"] if self_key else ""
    visits: list[dict[str, Any]] = []
    with httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as http:
        for key, cfg in config.items():
            b = snap["brands"].get(key, {})
            if cfg.get("store") and b.get("store"):
                v = visit_store(http, cfg["name"], cfg["store"], b["store"]["products"])
                v["outfit"] = store_metrics(b["store"], snap["date"])["outfit_price"]
                visits.append(v)
                log(f"{cfg['name']}: {'shopped' if not v.get('error') else v['error']}")
            if b.get("amazon"):
                visits.append(view_amazon(cfg["name"], b["amazon"]))
    store_prices = [v["outfit"] for v in visits if v["site"] == "store" and v.get("outfit")]
    amz_prices = [v["median_price"] for v in visits if v["site"] == "amazon" and v.get("median_price")]
    for v in visits:
        peer = median(amz_prices if v["site"] == "amazon" else store_prices) \
            if (amz_prices if v["site"] == "amazon" else store_prices) else None
        v["scores"] = score(v, peer)
        v["overall"] = overall(v["scores"])
        v["review"] = narrate(v)
    result: dict[str, Any] = {
        "date": date.today().isoformat(),
        "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "persona": PERSONA, "self": self_name, "visits": visits,
        "verdict": verdict(visits, self_name), "voice": "checks",
        "not_visited": [{"where": "Walmart", "why": "Walmart blocks automated visits and its "
                         "terms forbid them; check it by hand or connect Walmart Marketplace."}],
    }
    if model and (words := llm_review(visits, model)):
        by = {(s.get("brand"), s.get("site")): s for s in words.get("sites", [])}
        for v in visits:
            if (s := by.get((v["brand"], v["site"]))) and not v.get("error"):
                v["review"] = {k: s.get(k) or v["review"][k]
                               for k in ("summary", "likes", "dislikes", "change")}
        if words.get("verdict", {}).get("choice"):
            result["verdict"] = {**result["verdict"], **words["verdict"]}
        result["voice"] = "claude"
    return result


def save(result: dict[str, Any], root: Path = ROOT) -> Path:
    d = root / "shopper"
    d.mkdir(parents=True, exist_ok=True)
    slim = {**result, "visits": [{k: x for k, x in v.items() if k not in ("excerpt", "policy_excerpt")}
                                 for v in result["visits"]]}
    path = d / f"{result['date']}.json"
    path.write_text(json.dumps(slim, indent=1))
    (d / "latest.json").write_text(json.dumps(slim, indent=1))
    return path


def latest(root: Path = ROOT) -> dict[str, Any] | None:
    p = root / "shopper" / "latest.json"
    return json.loads(p.read_text()) if p.exists() else None


LIVE_DAYS = 60  # how long a hand-run live walkthrough's findings stay in the queue


def live(root: Path = ROOT) -> dict[str, Any] | None:
    """The latest live walkthrough (market/shopper/live-YYYY-MM-DD.json): a person, or
    Claude in a real browser, shopping the sites by hand, Walmart included."""
    files = sorted((root / "shopper").glob("live-*.json"))
    return json.loads(files[-1].read_text()) if files else None


def live_findings(root: Path = ROOT, today: date | None = None) -> list[dict[str, Any]]:
    w = live(root)
    if not w or ((today or date.today()) - date.fromisoformat(w["date"])).days > LIVE_DAYS:
        return []
    return w.get("findings", [])


def due(root: Path = ROOT, every: int = EVERY_DAYS) -> bool:
    last = latest(root)
    return not last or (date.today() - date.fromisoformat(last["date"])).days >= every


def findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Our own store's gaps as market-tracker findings (area 'Website'), for the UX agent."""
    out = []
    for v in result["visits"]:
        if v["brand"] != result["self"] or v["site"] != "store" or v.get("error"):
            continue
        peers = [x for x in result["visits"] if x["site"] == "store" and not x.get("error")
                 and x["brand"] != result["self"]]
        f = v["found"]
        for c in sorted(CHECKS, key=lambda c: -c.weight):
            if f.get(c.key):
                continue
            have = [x["brand"] for x in peers if x["found"].get(c.key)]
            if c.weight < 2 and len(have) < len(peers) / 2:
                continue  # a nice-to-have most rivals skip too
            out.append({
                "when": 2 if c.weight == 3 else 3, "area": "Website", "effort": "Low",
                "title": f"dagacci.com: {c.no[0].lower() + c.no[1:]}",
                "see": (f"The mystery shopper didn't find it on {v['product']['title'][:60]}; "
                        f"{len(have)} of {len(peers)} other brands have it"
                        + (f" ({', '.join(have[:4])})" if have else "") + "."),
                "why": "Shoppers who can't find this on the page buy elsewhere or guess and return.",
                "do": [c.fix + "."], "kpi": None, "now": None, "target": None,
                "url": v["product"]["url"]})
        if (v.get("page_kb") or 0) > HEAVY_KB:
            out.insert(0, {
                "when": 2, "area": "Website", "effort": "Medium",
                "title": f"dagacci.com product pages are heavy ({v['page_kb'] / 1024:.1f} MB)",
                "see": f"{v['product']['title'][:60]} is {v['page_kb']:,} KB; other brands' median "
                       f"is {median([x['page_kb'] for x in peers]):,.0f} KB." if peers else "",
                "why": "Most scrub shoppers are on phones; a slow page loses them before they "
                       "see the price.",
                "do": ["Compress and lazy-load images; remove apps the page doesn't need."],
                "kpi": "Product page weight", "now": f"{v['page_kb']:,} KB", "target": "< 1,500 KB",
                "url": v["product"]["url"]})
    return out
