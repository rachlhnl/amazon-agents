"""Turn market snapshots into the dashboard: metrics, trends, changes, opportunities
and a plain-language read of every chart.

Everything here is deterministic: the same history gives the same page. The output is
a dict of tabs, each a list of tiles, charts and lists that render.py draws without
knowing what they mean.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from statistics import median
from typing import Any

APPAREL = ("Scrub sets", "Tops", "Pants", "Underscrubs", "Lab coats & jackets")
SCRUB_RANK = re.compile(r"scrub", re.IGNORECASE)
SELF, RIVAL = "s1", "s2"  # categorical slots: our brand blue, the rival orange


# --- small helpers -----------------------------------------------------------

def _med(xs: list[float]) -> float | None:
    xs = [x for x in xs if x]
    return median(xs) if xs else None


def usd(x: float | None, cents: bool = True) -> str:
    if x is None:
        return "–"
    return f"${x:,.2f}" if cents else f"${x:,.0f}"


def pct(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "–"
    return f"{x * 100:+.0f}%" if signed else f"{x * 100:.0f}%"


def num(x: float | None) -> str:
    return "–" if x is None else f"{x:,.0f}"


_SEP = re.compile(r"[\s|–-]+$")


def color_suffixes(ps: list[dict[str, Any]]) -> set[str]:
    """Trailing words shared by 3+ titles with no color option ('... Eggplant'): brands
    that put the color only in the title."""
    counts: dict[str, int] = {}
    for p in ps:
        if p.get("color_names"):
            continue
        words = p["title"].lower().split()
        for n in (2, 1):
            if len(words) > n:
                k = " ".join(words[-n:])
                counts[k] = counts.get(k, 0) + 1
    return {k for k, v in counts.items() if v >= 3}


def style_of(p: dict[str, Any], suffixes: set[str] = frozenset()) -> tuple[str, set[str]]:
    """(style, colors) for a product: 'The Mari Scrub Set - Burgundy' and 'The Mari Scrub
    Set - Black' are one style in two colors."""
    t = p["title"].lower()
    names = [c.lower() for c in p.get("color_names") or []]
    for c in sorted(names, key=len, reverse=True):
        if t.endswith(c):
            t = t[: -len(c)]
            break
    else:
        if not names:
            words = t.split()
            for n in (2, 1):
                if len(words) > n and " ".join(words[-n:]) in suffixes:
                    names = [" ".join(words[-n:])]
                    t = " ".join(words[:-n])
                    break
        if " - " in t:
            t = t.rsplit(" - ", 1)[0]
    return " ".join(_SEP.sub("", t).split()), set(names) or {f"#{p['id']}:{i}" for i in range(p["colors"])}


def styles(ps: list[dict[str, Any]]) -> dict[str, set[str]]:
    suffixes = color_suffixes(ps)
    out: dict[str, set[str]] = {}
    for p in ps:
        st, colors = style_of(p, suffixes)
        out.setdefault(st, set()).update(colors)
    return out


def short(title: str) -> str:
    t = re.sub(r"^(DAGACCI|Dagacci)\s+(MEDICAL UNIFORM®?|Medical Uniform®?|OFFICIAL|OFFCIAL)\s*",
               "", title)
    t = re.sub(r"^(COZYFIT|COZY SCRUBS)\s+", "", t)
    t = re.sub(r"\s*\[[^\]]*\]$", "", t)
    t = re.split(r",\s|:\s", t)[0]
    return t[:60]


def scrub_rank(item: dict[str, Any]) -> tuple[str, int] | None:
    """The item's best rank in a scrubs category (e.g. 'Women's Medical Scrub Sets')."""
    ranks = [(c, r) for c, r in (item.get("ranks") or {}).items() if SCRUB_RANK.search(c)]
    return min(ranks, key=lambda x: x[1]) if ranks else None


def _days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


# --- metrics for one snapshot --------------------------------------------------

def store_metrics(store: dict[str, Any], today: str) -> dict[str, Any]:
    ps = [p for p in store["products"] if p["price"] > 0]
    apparel = [p for p in ps if p["line"] in APPAREL]
    by = {line: [p["price"] for p in apparel if p["line"] == line] for line in APPAREL}
    sets, tops, pants = by["Scrub sets"], by["Tops"], by["Pants"]
    if len(sets) >= 5:
        outfit, outfit_how = _med(sets), "median scrub set"
    elif tops and pants:
        outfit, outfit_how = _med(tops) + _med(pants), "median top + median pant"
    else:
        outfit, outfit_how = _med(sets), "median scrub set"
    disc = [p for p in apparel if p["compare_at"] and p["compare_at"] > p["price"] * 1.01]
    variants = sum(p["variants"] for p in ps)
    in_stock = sum(p["in_stock"] for p in ps)
    cutoff = (date.fromisoformat(today) - timedelta(days=90)).isoformat()
    rated = [p for p in ps if p.get("reviews")]
    reviews = sum(p["reviews"] for p in rated)
    all_styles, apparel_styles = styles(ps), styles(apparel)
    new = styles([p for p in ps if p["created"] >= cutoff])
    # A store that was rebuilt shows its whole catalog as new; that isn't launches.
    new_90d = len(new) if len(new) <= 0.6 * len(all_styles) else None
    return {
        "products": len(ps),
        "styles": len(all_styles),
        "variants": variants,
        "in_stock_share": in_stock / variants if variants else None,
        "outfit_price": outfit,
        "outfit_how": outfit_how,
        "median_top": _med(tops),
        "median_pant": _med(pants),
        "discounted_share": len(disc) / len(apparel) if apparel else None,
        "avg_discount": _med([1 - p["price"] / p["compare_at"] for p in disc]),
        "new_90d": new_90d,
        "colors_per_style": _med([len(v) for v in apparel_styles.values()]),
        "lines": {line: sum(1 for p in ps if p["line"] == line) for line in
                  dict.fromkeys(p["line"] for p in ps)},
        "reviews": reviews or None,
        "rating": (sum(p["rating"] * p["reviews"] for p in rated) / reviews) if reviews else None,
    }


def amazon_listings(items: list[dict[str, Any]], last_good: dict[str, dict]) -> list[dict]:
    """Merge today's reads with the last good read of each ASIN; mark stale ones."""
    out = []
    for it in items:
        if "error" in it and it["asin"] in last_good:
            out.append({**last_good[it["asin"]], "stale": it["error"]})
        elif "error" not in it:
            out.append(it)
        else:
            out.append({"asin": it["asin"], "missing": it["error"]})
    return out


def amazon_metrics(listings: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [x for x in listings if "missing" not in x]
    # Reviews are shared across a listing family; count each parent once.
    fam: dict[str, dict] = {}
    for x in ok:
        fam.setdefault(x.get("parent") or x["asin"], x)
    rated = [x for x in fam.values() if x.get("reviews") and x.get("rating")]
    reviews = sum(x["reviews"] for x in rated)
    ranks = [r[1] for x in ok if (r := scrub_rank(x))]
    bought = [x["bought_past_month"] for x in fam.values() if x.get("bought_past_month")]
    return {
        "listings": len(ok),
        "families": len(fam),
        "median_price": _med([x.get("price") for x in ok]),
        "reviews": reviews or None,
        "rating": sum(x["rating"] * x["reviews"] for x in rated) / reviews if reviews else None,
        "best_rank": min(ranks) if ranks else None,
        "median_rank": _med(ranks),
        "bought_past_month": sum(bought) if bought else None,
        "stale": sum(1 for x in listings if "stale" in x),
        "missing": sum(1 for x in listings if "missing" in x),
    }


def snapshot_metrics(snaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Metrics for every snapshot, carrying Amazon reads forward across blocked days."""
    last_good: dict[str, dict[str, dict]] = {}
    out = []
    for s in snaps:
        day: dict[str, Any] = {"date": s["date"], "brands": {}}
        for key, b in s["brands"].items():
            m: dict[str, Any] = {}
            if b.get("store"):
                m["store"] = store_metrics(b["store"], s["date"])
            if "amazon" in b:
                lg = last_good.setdefault(key, {})
                listings = amazon_listings(b["amazon"], lg)
                for it in b["amazon"]:
                    if "error" not in it:
                        lg[it["asin"]] = it
                m["amazon_listings"] = listings
                m["amazon"] = amazon_metrics(listings)
            day["brands"][key] = m
        out.append(day)
    return out


# --- trends and changes ----------------------------------------------------------

def series(days: list[dict], key: str, path: tuple[str, str]) -> list[list]:
    pts = []
    for d in days:
        v = d["brands"].get(key, {}).get(path[0], {}).get(path[1])
        if v is not None:
            pts.append([d["date"], round(v, 4)])
    return pts


def delta(pts: list[list], days_back: int) -> tuple[float, float, int] | None:
    """(now, then, days) comparing the latest point with the one closest to days_back ago."""
    if len(pts) < 2:
        return None
    now_d, now_v = pts[-1]
    target = (date.fromisoformat(now_d) - timedelta(days=days_back)).isoformat()
    prior = [p for p in pts[:-1] if p[0] <= target] or pts[:1]
    then_d, then_v = prior[-1]
    return now_v, then_v, _days_between(then_d, now_d)


def store_changes(prev: dict | None, cur: dict) -> list[dict[str, str]]:
    if not prev:
        return []
    old = {p["id"]: p for p in prev["products"]}
    new = {p["id"]: p for p in cur["products"]}
    out = []
    for pid, p in new.items():
        o = old.get(pid)
        name = short(p["title"])
        if o is None:
            out.append({"kind": "New product", "text": f"{name} listed at {usd(p['price'])}"})
            continue
        if abs(p["price"] - o["price"]) >= 0.01:
            out.append({"kind": "Price", "text": f"{name}: {usd(o['price'])} → {usd(p['price'])}"})
        if o["in_stock"] and not p["in_stock"]:
            out.append({"kind": "Sold out", "text": f"{name}: no color/size left to buy"})
        elif not o["in_stock"] and p["in_stock"]:
            out.append({"kind": "Restocked", "text": f"{name}: {p['in_stock']} options back"})
        if (p.get("reviews") or 0) > (o.get("reviews") or 0):
            out.append({"kind": "Reviews", "text":
                        f"{name}: +{p['reviews'] - o['reviews']} reviews "
                        f"(now {p['reviews']}, {p.get('rating') or 0:.2f}★)"})
    for pid, o in old.items():
        if pid not in new:
            out.append({"kind": "Removed", "text": f"{short(o['title'])} is no longer listed"})
    return out


def amazon_changes(prev: list[dict] | None, cur: list[dict]) -> list[dict[str, str]]:
    if not prev:
        return []
    old = {x["asin"]: x for x in prev if "missing" not in x and "stale" not in x}
    out = []
    for x in cur:
        o = old.get(x["asin"])
        if not o or "missing" in x or "stale" in x:
            continue
        name = short(x.get("title", x["asin"]))
        if x.get("price") and o.get("price") and abs(x["price"] - o["price"]) >= 0.01:
            out.append({"kind": "Price", "text": f"{name}: {usd(o['price'])} → {usd(x['price'])}"})
        if (x.get("reviews") or 0) > (o.get("reviews") or 0):
            out.append({"kind": "Reviews", "text": f"{name}: +{x['reviews'] - o['reviews']} reviews"})
        r, ro = scrub_rank(x), scrub_rank(o)
        if r and ro and r[1] != ro[1]:
            word = "up" if r[1] < ro[1] else "down"
            out.append({"kind": "Rank", "text": f"{name}: {word} #{ro[1]} → #{r[1]} in {r[0]}"})
    return out


# --- tab builders ------------------------------------------------------------------

def tile(label: str, value: str, note: str = "", d: tuple | None = None,
         fmt: str = "num", up_is_good: bool = True) -> dict[str, Any]:
    t: dict[str, Any] = {"label": label, "value": value, "note": note}
    if d:
        now, then, days = d
        if then and abs(now - then) > 1e-9:
            change = now - then
            if fmt == "pct":
                txt = f"{change * 100:+.1f} pts"
            elif fmt == "usd":
                txt = f"{'+' if change > 0 else '−'}{usd(abs(change))}"
            else:
                txt = f"{change:+,.0f}" if abs(change) >= 1 else f"{change:+.2f}"
            good = (change > 0) == up_is_good
            t["delta"] = {"text": f"{txt} vs {days}d ago", "good": good}
        else:
            t["delta"] = {"text": f"no change vs {days}d ago", "good": None}
    return t


def lines_chart(title: str, sub: str, read: str, series_: list[dict], fmt: str) -> dict:
    return {"type": "lines", "title": title, "sub": sub, "read": read,
            "series": series_, "fmt": fmt}


def trend_read(name: str, pts: list[list], fmt: str, noun: str, up_is_good: bool = True) -> str:
    if len(pts) < 2:
        return (f"One day of data so far ({pts[0][0] if pts else 'none'}). The line appears "
                "after the second daily run; week-over-week reads start after seven.")
    first, last = pts[0][1], pts[-1][1]
    days = _days_between(pts[0][0], pts[-1][0])
    f = {"usd": usd, "pct": pct}.get(fmt, num)
    if abs(last - first) < 1e-9:
        return f"{name}'s {noun} held at {f(last)} over {days} days."
    direction = "rose" if last > first else "fell"
    good = (last > first) == up_is_good
    tail = " — a good sign." if good else " — worth a look."
    rate = ""
    if fmt == "num" and days:
        rate = f" ({(last - first) / days:+,.1f} a day)"
    return f"{name}'s {noun} {direction} from {f(first)} to {f(last)} over {days} days{rate}{tail}"


def self_tab(cfg: dict, days: list[dict], snaps: list[dict], peers: dict) -> dict:
    key, name = cfg["key"], cfg["name"]
    cur_snap = snaps[-1]["brands"][key]
    prev_snap = snaps[-2]["brands"][key] if len(snaps) > 1 else None
    m = days[-1]["brands"][key]
    sm, am = m.get("store"), m.get("amazon")
    products = cur_snap["store"]["products"] if cur_snap.get("store") else []
    tiles, charts, opps = [], [], []

    if sm:
        rev_pts = series(days, key, ("store", "reviews"))
        stock_pts = series(days, key, ("store", "in_stock_share"))
        price_pts = series(days, key, ("store", "outfit_price"))
        tiles += [
            tile("Products listed", num(sm["products"]), f"{sm['styles']} styles"),
            tile("In stock", pct(sm["in_stock_share"]), f"of {sm['variants']:,} color/size options",
                 delta(stock_pts, 7), "pct"),
            tile("Typical scrub set", usd(sm["outfit_price"]), sm["outfit_how"],
                 delta(price_pts, 7), "usd", up_is_good=True),
            tile("Store rating", f"{sm['rating']:.2f} ★" if sm["rating"] else "–",
                 f"{num(sm['reviews'])} reviews on {cfg['store']}",
                 delta(series(days, key, ("store", "rating")), 7)),
        ]
    if am:
        tiles.append(tile("Amazon reviews", num(am["reviews"]),
                          f"{am['rating']:.2f} ★ across {am['families']} listings" if am["rating"]
                          else "", delta(series(days, key, ("amazon", "reviews")), 7)))
        if am["best_rank"]:
            tiles.append(tile("Best scrubs rank", f"#{am['best_rank']:,}", "best Amazon listing",
                              delta(series(days, key, ("amazon", "best_rank")), 7),
                              up_is_good=False))

    # Trends
    if sm:
        charts.append({"section": "Trends"} | lines_chart(
            "Reviews on dagacci.com", "Total on-site reviews, daily",
            trend_read(name, rev_pts, "num", "on-site review count") +
            " Review growth is the best public proxy for order volume: a slowdown usually "
            "means fewer orders a few weeks earlier.",
            [{"name": name, "color": SELF, "points": rev_pts}], "num"))
        charts.append({"section": "Trends"} | lines_chart(
            "Share of options in stock", "Color/size options that can be bought, daily",
            trend_read(name, stock_pts, "pct", "in-stock share") +
            " Under ~80% means shoppers are regularly hitting sold-out sizes.",
            [{"name": name, "color": SELF, "points": stock_pts}], "pct"))
    if am:
        az_pts = series(days, key, ("amazon", "reviews"))
        charts.append({"section": "Trends"} | lines_chart(
            "Amazon reviews", "Reviews across the tracked Amazon listings, daily",
            trend_read(name, az_pts, "num", "Amazon review count"),
            [{"name": name, "color": SELF, "points": az_pts}], "num"))

    # Catalog history from listing dates: real history even on day one.
    if products:
        years: dict[str, int] = {}
        for p in products:
            years[p["created"][:4]] = years.get(p["created"][:4], 0) + 1
        ys = sorted(years)
        recent = sum(v for y, v in years.items() if y >= ys[-1])
        busiest = max(years, key=years.get)
        charts.append({
            "section": "Trends", "type": "bars", "title": "When today's products were first listed",
            "sub": "Products still on the site, by year first listed",
            "read": (f"{years[busiest]} of today's {len(products)} products were first listed in "
                     f"{busiest}, the busiest year. {recent} came in {ys[-1]}. "
                     + ("Most of the range was relaunched recently, which resets on-site reviews "
                        "for those pages." if years[busiest] > len(products) / 3 and busiest >= ys[-1]
                        else "The range is mostly older styles; newness is a lever not being used.")),
            "rows": [{"name": y, "value": years[y] / years[busiest], "label": str(years[y]),
                      "color": SELF,
                      "tip": [f"{years[y]} products", f"first listed in {y}"]} for y in ys],
        })

    # Catalog snapshot charts
    if sm:
        lmax = max(sm["lines"].values())
        charts.append({
            "section": "Catalog", "type": "bars", "title": "Catalog by product line",
            "sub": f"Products on {cfg['store']}",
            "read": _lines_read(sm, peers),
            "rows": [{"name": ln, "value": v / lmax, "label": str(v), "color": SELF,
                      "tip": [f"{v} products", ln]}
                     for ln, v in sorted(sm["lines"].items(), key=lambda x: -x[1])],
        })
        rated = sorted([p for p in products if (p.get("reviews") or 0) >= 20],
                       key=lambda p: -p["rating"])
        if rated:
            charts.append({
                "section": "Catalog", "type": "bars", "title": "Ratings by product",
                "sub": "Average stars on the store, products with 20+ reviews",
                "read": _ratings_read(rated),
                "rows": [{"name": short(p["title"]), "value": p["rating"] / 5,
                          "label": f"{p['rating']:.2f} ★", "color": SELF,
                          "tip": [f"{p['rating']:.2f} ★ from {p['reviews']} reviews", p["title"]]}
                         for p in rated],
            })
        sets = sorted([p for p in products if p["line"] == "Scrub sets"], key=lambda p: -p["price"])
        if sets:
            pmax = max(max(p["price"], p["compare_at"] or 0) for p in sets)
            charts.append({
                "section": "Catalog", "type": "bars", "title": "Scrub set prices",
                "sub": "Selling price against the struck-through “compare at” price",
                "legend": [{"label": "Price", "kind": "bar", "color": SELF},
                           {"label": "Compare-at price", "kind": "tick"}],
                "read": _prices_read(sets, sm, peers),
                "rows": [{"name": short(p["title"]), "value": p["price"] / pmax,
                          "tick": (p["compare_at"] or 0) / pmax or None, "label": usd(p["price"]),
                          "color": SELF,
                          "tip": [usd(p["price"]), p["title"],
                                  f"compare at {usd(p['compare_at'])}" if p["compare_at"]
                                  else "no compare-at price"]} for p in sets],
            })
        stock = sorted([p for p in products if p["variants"] > 1],
                       key=lambda p: p["in_stock"] / p["variants"])
        charts.append({
            "section": "Catalog", "type": "bars", "title": "Stock by product",
            "sub": "Share of color/size options that can be bought now, lowest first",
            "read": _stock_read(stock),
            "rows": [{"name": short(p["title"]), "value": p["in_stock"] / p["variants"], "track": True,
                      "label": pct(p["in_stock"] / p["variants"]), "color": SELF,
                      "tip": [pct(p["in_stock"] / p["variants"]) + " in stock", p["title"],
                              f"{p['in_stock']} of {p['variants']} options"]} for p in stock],
        })
        opps += _self_opportunities(products, sm, peers, cfg)

    if am:
        charts.append(_amazon_table(m["amazon_listings"], f"{name} on Amazon", "Amazon"))

    changes = store_changes(prev_snap.get("store") if prev_snap else None, cur_snap.get("store"))
    prev_days = days[-2]["brands"][key] if len(days) > 1 else {}
    changes += [c | {"kind": "Amazon " + c["kind"].lower()} for c in
                amazon_changes(prev_days.get("amazon_listings"), m.get("amazon_listings", []))]
    if am and (am["stale"] or am["missing"]):
        opps.append({"priority": "Data", "title": "Connect SP-API for reliable Amazon numbers",
                     "text": f"{am['stale'] + am['missing']} of {len(m['amazon_listings'])} Amazon "
                     "pages couldn't be read today (Amazon blocks automated reading), so their "
                     "figures are carried over from the last good day. Adding SP-API keys to .env "
                     "gives price and sales rank every day."})
    return {"id": "self", "label": name, "tiles": tiles, "charts": charts,
            "opportunities": opps, "changes": changes,
            "note": f"Public data from {cfg['store']} and Amazon product pages."}


def _lines_read(sm: dict, peers: dict) -> str:
    lines = sm["lines"]
    top = max(lines, key=lines.get)
    share = lines[top] / sm["products"]
    txt = (f"{pct(share)} of the catalog is {top.lower()} ({lines[top]} of {sm['products']}). "
           f"The site carries {sm['styles']} distinct styles.")
    if peers:
        pm = _med([p["styles"] for p in peers.values()])
        txt += (f" Peer brands carry a median of {pm:.0f} styles on their own stores, so "
                f"{'Dagacci’s range is narrow: the site is a showroom for the Amazon business rather than a full assortment.' if sm['styles'] < pm / 2 else 'the range is in line with peers.'}")
    return txt


def _ratings_read(rated: list[dict]) -> str:
    best, worst = rated[0], rated[-1]
    low = [p for p in rated if p["rating"] < 3.8]
    txt = (f"Best: {short(best['title'])} at {best['rating']:.2f}★ ({best['reviews']} reviews). "
           f"Lowest: {short(worst['title'])} at {worst['rating']:.2f}★. ")
    if low:
        txt += (f"{len(low)} products sit under 3.8★ — on Amazon that is roughly where conversion "
                "drops off, so read their 1–2★ reviews for a fixable fit or fabric complaint.")
    else:
        txt += "Nothing is below 3.8★, so ratings aren't holding any product back."
    return txt


def _prices_read(sets: list[dict], sm: dict, peers: dict) -> str:
    lo, hi = min(p["price"] for p in sets), max(p["price"] for p in sets)
    disc = [p for p in sets if p["compare_at"] and p["compare_at"] > p["price"] * 1.01]
    txt = f"Sets run {usd(lo)}–{usd(hi)}. {len(disc)} of {len(sets)} show a markdown from a compare-at price"
    if disc:
        txt += f" (median {pct(_med([1 - p['price'] / p['compare_at'] for p in disc]))} off)"
    txt += "."
    pm = _med([p["outfit_price"] for p in peers.values()])
    if pm and sm["outfit_price"]:
        ratio = sm["outfit_price"] / pm
        txt += (f" A typical peer outfit costs {usd(pm)} on their own sites, so Dagacci is priced at "
                f"{pct(ratio)} of the market"
                + (" — firmly the value option." if ratio < 0.7 else "."))
    return txt


def _stock_read(stock: list[dict]) -> str:
    out = [p for p in stock if p["in_stock"] == 0]
    thin = [p for p in stock if 0 < p["in_stock"] / p["variants"] < 0.5]
    txt = f"{len(out)} products are fully sold out and {len(thin)} more have under half their options available."
    reviewed_out = [p for p in out + thin if (p.get("reviews") or 0) >= 20]
    if reviewed_out:
        names = ", ".join(short(p["title"]) for p in reviewed_out[:3])
        txt += f" The ones with proven demand (20+ reviews) are the priority: {names}."
    return txt


def _self_opportunities(products: list[dict], sm: dict, peers: dict, cfg: dict) -> list[dict]:
    opps = []
    gaps = [p for p in products if (p.get("reviews") or 0) >= 20
            and p["variants"] and p["in_stock"] / p["variants"] < 0.5]
    for p in sorted(gaps, key=lambda p: -p["reviews"])[:3]:
        opps.append({"priority": "High", "title": f"Restock {short(p['title'])}",
                     "text": f"{p['in_stock']} of {p['variants']} options buyable, despite "
                     f"{p['reviews']} reviews at {p['rating']:.2f}★. Proven demand that can't "
                     "currently convert.", "url": p["url"]})
    winners = sorted([p for p in products if (p.get("reviews") or 0) >= 50 and p["rating"] >= 4.35],
                     key=lambda p: -p["rating"])
    if winners:
        names = ", ".join(short(p["title"]) for p in winners[:3])
        floor = min(p["rating"] for p in winners[:3])
        opps.append({"priority": "High", "title": "Put spend behind the best-rated sets",
                     "text": f"{names} rate {floor:.2f}★ or better on 50+ reviews. "
                     "These are the safest products to feature in ads, the homepage and Amazon "
                     "Sponsored Brands."})
    weak = [p for p in products if (p.get("reviews") or 0) >= 40 and p["rating"] < 3.8]
    for p in weak[:2]:
        opps.append({"priority": "Medium", "title": f"Fix what buyers dislike in {short(p['title'])}",
                     "text": f"{p['rating']:.2f}★ on {p['reviews']} reviews. Read the 1–2★ reviews "
                     "for a recurring fit or fabric issue; fix the product or say it in the listing.",
                     "url": p["url"]})
    pm = _med([p["outfit_price"] for p in peers.values()])
    if pm and sm["outfit_price"] and sm["outfit_price"] < 0.6 * pm and (sm["rating"] or 0) >= 4.0:
        opps.append({"priority": "Medium", "title": "Test a small price increase on top sets",
                     "text": f"A typical Dagacci set is {usd(sm['outfit_price'])} against a peer median of "
                     f"{usd(pm)}, with a {sm['rating']:.2f}★ store rating. A +5–10% test on the "
                     "best-rated sets (within the pricing agent's 10% guardrail) would show "
                     "how much headroom there is."})
    peer_new = _med([p["new_90d"] for p in peers.values()])
    if peer_new and sm["new_90d"] is not None and sm["new_90d"] < peer_new / 2:
        opps.append({"priority": "Medium", "title": "Launch more often",
                     "text": f"{sm['new_90d']} new styles in the last 90 days against a peer median of "
                     f"{peer_new:.0f}. New colors are the cheapest newness: peers carry a median of "
                     f"{_med([p['colors_per_style'] for p in peers.values()]):.0f} colors per style "
                     f"vs Dagacci's {sm['colors_per_style']:.0f}."})
    return opps


def _amazon_table(listings: list[dict], title: str, section: str) -> dict:
    rows = []
    for x in listings:
        if "missing" in x:
            rows.append([x["asin"], f"https://www.amazon.com/dp/{x['asin']}", None, None, None, None,
                         "couldn't read"])
            continue
        r = scrub_rank(x)
        rows.append([short(x.get("title", x["asin"])), f"https://www.amazon.com/dp/{x['asin']}",
                     x.get("price"), x.get("rating"), x.get("reviews"), r[1] if r else None,
                     ("as of last good read" if "stale" in x else (r[0] if r else ""))])
    return {"section": section, "type": "table", "title": title,
            "sub": "Tracked listings. Rank is the best rank in a scrubs category (lower is better).",
            "columns": [{"label": "Listing"}, {"label": "Price", "fmt": "usd"},
                        {"label": "Rating", "fmt": "stars"}, {"label": "Reviews", "fmt": "int"},
                        {"label": "Scrubs rank", "fmt": "rank"}, {"label": "Category / note"}],
            "rows": rows, "sort": 3}


def rival_tab(self_cfg: dict, rival_cfg: dict, days: list[dict], snaps: list[dict]) -> dict:
    sk, rk = self_cfg["key"], rival_cfg["key"]
    sn, rn = self_cfg["name"], rival_cfg["name"]
    cur = days[-1]["brands"]
    sa, ra = cur[sk].get("amazon"), cur[rk].get("amazon")
    sl, rl = cur[sk].get("amazon_listings", []), cur[rk].get("amazon_listings", [])
    tiles, charts, opps = [], [], []

    def pair(label, f, field, note="", up_is_good=True, fmt="num"):
        sv, rv = (sa or {}).get(field), (ra or {}).get(field)
        tiles.append({"label": label, "pair": [
            {"brand": sn, "color": SELF, "value": f(sv),
             **({"delta": d} if (d := _pair_delta(days, sk, field, fmt, up_is_good)) else {})},
            {"brand": rn, "color": RIVAL, "value": f(rv),
             **({"delta": d} if (d := _pair_delta(days, rk, field, fmt, up_is_good)) else {})}],
            "note": note})

    pair("Median price", usd, "median_price", "tracked Amazon listings", fmt="usd")
    pair("Rating", lambda v: f"{v:.2f} ★" if v else "–", "rating", "review-weighted")
    pair("Reviews", num, "reviews", "across listing families")
    pair("Best scrubs rank", lambda v: f"#{v:,}" if v else "–", "best_rank", "lower is better",
         up_is_good=False)

    # Price strip: every listing as a dot on one price axis.
    pts = []
    for nm, col, ls in ((sn, SELF, sl), (rn, RIVAL, rl)):
        pts.append({"name": nm, "color": col, "points": [
            {"x": x["price"], "tip": [usd(x["price"]), short(x.get("title", "")),
                                      f"{x.get('rating') or 0:.1f}★ · {num(x.get('reviews'))} reviews"]}
            for x in ls if x.get("price")]})
    sp, rp = (sa or {}).get("median_price"), (ra or {}).get("median_price")
    read = "Prices couldn't be read today."
    if sp and rp:
        gap = sp / rp - 1
        read = (f"{sn}'s median tracked listing is {usd(sp)} vs {rn}'s {usd(rp)} "
                f"({pct(gap, True)}). ")
        read += (f"{sn} is the cheaper option, so the comparison shoppers make is value vs "
                 f"{rn}'s stretch/pocket features, not price." if gap < -0.05 else
                 f"{sn} is not cheaper, so it has to win on reviews and brand history."
                 if gap > 0.05 else "Prices are at parity, so reviews and images decide the click.")
    charts.append({"section": "Head to head", "type": "dots", "title": "Price per listing",
                   "sub": "Current Amazon price of each tracked listing", "read": read,
                   "series": pts, "fmt": "usd"})

    # Reviews per listing family, both brands.
    fams = []
    for nm, col, ls in ((sn, SELF, sl), (rn, RIVAL, rl)):
        seen = set()
        for x in ls:
            k = x.get("parent") or x["asin"]
            if "missing" in x or k in seen or not x.get("reviews"):
                continue
            seen.add(k)
            fams.append((nm, col, x))
    fams.sort(key=lambda t: -t[2]["reviews"])
    if fams:
        rmax = fams[0][2]["reviews"]
        sr, rr = (sa or {}).get("reviews") or 0, (ra or {}).get("reviews") or 0
        lead = sn if sr >= rr else rn
        charts.append({
            "section": "Head to head", "type": "bars", "title": "Reviews per listing",
            "sub": "Review count of each listing family (colors and sizes share reviews)",
            "legend": [{"label": sn, "kind": "bar", "color": SELF},
                       {"label": rn, "kind": "bar", "color": RIVAL}],
            "read": (f"{lead} leads on social proof: {num(max(sr, rr))} reviews vs "
                     f"{num(min(sr, rr))} across the tracked listings. "
                     f"{rn}'s biggest listing has {num(max((x['reviews'] for n, c, x in fams if n == rn), default=0))}; "
                     f"{sn}'s has {num(max((x['reviews'] for n, c, x in fams if n == sn), default=0))}. "
                     "Review count is what keeps a listing on page one, so the daily gain "
                     "(Trends below) matters more than the total."),
            "rows": [{"name": f"{nm}: {short(x.get('title', ''))}", "value": x["reviews"] / rmax,
                      "label": num(x["reviews"]), "color": col,
                      "tip": [f"{num(x['reviews'])} reviews", x.get("title", ""),
                              f"{x.get('rating') or 0:.1f}★ · {usd(x.get('price'))}"]}
                     for nm, col, x in fams[:16]],
        })

    # Category ranks
    ranked = [(nm, col, x, r) for nm, col, ls in ((sn, SELF, sl), (rn, RIVAL, rl))
              for x in ls if "missing" not in x and (r := scrub_rank(x))]
    if ranked:
        ranked.sort(key=lambda t: t[3][1])
        worst = max(t[3][1] for t in ranked)
        top10 = {nm: sum(1 for n, _, _, r in ranked if n == nm and r[1] <= 10) for nm in (sn, rn)}
        charts.append({
            "section": "Head to head", "type": "bars", "title": "Best-seller rank in scrubs",
            "sub": "Rank in the listing's scrubs category; shorter bar = better rank",
            "legend": [{"label": sn, "kind": "bar", "color": SELF},
                       {"label": rn, "kind": "bar", "color": RIVAL}],
            "read": (f"Top-10 placements: {sn} {top10[sn]}, {rn} {top10[rn]}. Best overall is "
                     f"{ranked[0][0]}'s {short(ranked[0][2].get('title', ''))} at #{ranked[0][3][1]} in "
                     f"{ranked[0][3][0]}. Rank moves daily with sales, so read it as a trend "
                     "(below), not a single number."),
            "rows": [{"name": f"{nm}: {short(x.get('title', ''))}", "value": r[1] / worst,
                      "label": f"#{r[1]:,}", "color": col,
                      "tip": [f"#{r[1]:,} in {r[0]}", x.get("title", "")]}
                     for nm, col, x, r in ranked[:16]],
        })

    # Trends
    for field, title, fmt, up in (("reviews", "Amazon reviews over time", "num", True),
                                  ("median_price", "Median Amazon price over time", "usd", True),
                                  ("best_rank", "Best scrubs rank over time", "rank", False)):
        s1, s2 = series(days, sk, ("amazon", field)), series(days, rk, ("amazon", field))
        read = trend_read(sn, s1, "num" if fmt == "rank" else fmt, title.split(" over")[0].lower(), up)
        if len(s2) >= 2:
            read += " " + trend_read(rn, s2, "num" if fmt == "rank" else fmt,
                                     title.split(" over")[0].lower(), up)
        if field == "reviews" and len(s1) >= 2 and len(s2) >= 2:
            g1 = (s1[-1][1] - s1[0][1]) / max(1, _days_between(s1[0][0], s1[-1][0]))
            g2 = (s2[-1][1] - s2[0][1]) / max(1, _days_between(s2[0][0], s2[-1][0]))
            read += (f" Review velocity: {sn} {g1:+.1f}/day vs {rn} {g2:+.1f}/day — "
                     f"{'Dagacci is out-pacing' if g1 > g2 else 'the rival is out-pacing'} on new social proof.")
        charts.append({"section": "Trends"} | lines_chart(
            title, "Tracked listings, daily", read,
            [{"name": sn, "color": SELF, "points": s1}, {"name": rn, "color": RIVAL, "points": s2}],
            fmt))

    charts.append(_amazon_table(rl, f"{rn} listings", "Listings"))
    charts.append(_amazon_table(sl, f"{sn} listings", "Listings"))

    # Opportunities
    rebrand = [x for x in rl if "COZY SCRUBS" in (x.get("title", "") + x.get("byline", "")).upper()]
    if rebrand:
        opps.append({"priority": "High", "title": f"{rn} is renaming itself “COZY SCRUBS”",
                     "text": f"{len(rebrand)} tracked {rn} listings now carry the COZY SCRUBS name. "
                     "A rename splits brand search for a while: bid on both names as Sponsored "
                     "Products keywords while shoppers relearn it."})
    for note in rival_cfg.get("notes", []):
        opps.append({"priority": "Watch", "title": note["title"], "text": note["text"]})
    if sa and ra and sa.get("rating") and ra.get("rating") and ra["rating"] > sa["rating"] + 0.05:
        opps.append({"priority": "High", "title": "Close the rating gap",
                     "text": f"{rn} rates {ra['rating']:.2f}★ vs {sn}'s {sa['rating']:.2f}★. Shoppers "
                     "see stars before price; the review agent's defect themes for the "
                     "lowest-rated listing are the place to start."})
    elif sa and ra and sa.get("rating") and ra.get("rating"):
        opps.append({"priority": "Medium", "title": "Lead with the rating",
                     "text": f"{sn} rates {sa['rating']:.2f}★ vs {rn}'s {ra['rating']:.2f}★. Put the "
                     "star count and review total in the main image and A+ content."})
    if sp and rp and sp < rp * 0.95:
        opps.append({"priority": "Medium", "title": "Use the price gap in ads",
                     "text": f"{sn}'s median listing is {usd(rp - sp)} cheaper than {rn}'s. Target "
                     f"{rn}'s listings with Sponsored Display product targeting; the ad shows "
                     "the cheaper, higher-reviewed alternative on their page."})
    rival_kinds = {k for x in rl for k in ("jogger", "cargo", "men") if k in x.get("title", "").lower()}
    self_kinds = {k for x in sl for k in ("jogger", "cargo", "men") if k in x.get("title", "").lower()}
    if missing := rival_kinds - self_kinds:
        opps.append({"priority": "Medium", "title": "Styles the rival covers and we don't track",
                     "text": f"{rn} has {', '.join(sorted(missing))} listings with no tracked "
                     f"{sn} equivalent. Add the matching {sn} ASINs to market/brands.toml, or "
                     "consider those styles for launch."})
    return {"id": "rival", "label": f"vs {rn}", "tiles": tiles, "charts": charts,
            "opportunities": opps,
            "changes": [c | {"kind": f"{rn} " + c["kind"].lower()} for c in amazon_changes(
                days[-2]["brands"][rk].get("amazon_listings") if len(days) > 1 else None, rl)],
            "note": f"{rn} sells only on Amazon, so this tab compares Amazon listings. "
                    "Amazon blocks some automated reads; blocked listings show their last good values."}


def _pair_delta(days, key, field, fmt, up_is_good):
    d = delta(series(days, key, ("amazon", field)), 7)
    if not d:
        return None
    t = tile("", "", "", d, fmt, up_is_good)
    return t.get("delta")


def market_tab(self_cfg: dict, peer_cfgs: list[dict], days: list[dict]) -> dict:
    cur = days[-1]["brands"]
    brands = [(self_cfg, True)] + [(c, False) for c in peer_cfgs]
    rows = [(c, is_self, cur[c["key"]]["store"]) for c, is_self in brands
            if cur.get(c["key"], {}).get("store")]
    charts, opps = [], []
    col = lambda is_self: SELF if is_self else "muted"

    def bar_chart(title, sub, field, fmt, read, invert=False):
        vals = [(c, s, m[field]) for c, s, m in rows if m.get(field) is not None]
        if not vals:
            return
        vals.sort(key=lambda t: t[2], reverse=not invert)
        mx = max(v for _, _, v in vals) or 1
        f = {"usd": lambda v: usd(v, False) if v >= 100 else usd(v), "pct": pct, "num": num}[fmt]
        charts.append({"section": "Market", "type": "bars", "title": title, "sub": sub, "read": read,
                       "legend": [{"label": self_cfg["name"], "kind": "bar", "color": SELF},
                                  {"label": "Other brands", "kind": "bar", "color": "muted"}],
                       "rows": [{"name": c["name"], "value": v / mx, "label": f(v), "color": col(s),
                                 "tip": [f(v), c["name"], c.get("store", "")]}
                                for c, s, v in vals]})

    me = cur[self_cfg["key"]]["store"]
    peers = [m for c, s, m in rows if not s]
    pm = lambda f: _med([p[f] for p in peers if p.get(f) is not None])

    rank = sorted([m["outfit_price"] for _, _, m in rows if m["outfit_price"]])
    pos = rank.index(me["outfit_price"]) + 1 if me["outfit_price"] in rank else None
    dear = sorted([(m["outfit_price"], c["name"]) for c, s, m in rows
                   if not s and m["outfit_price"]], reverse=True)[:2]
    read = (f"Dagacci is the #{pos} cheapest of {len(rank)} brands at {usd(me['outfit_price'])}; "
            f"the peer median is {usd(pm('outfit_price'))}.")
    if dear and me["outfit_price"]:
        read += (f" The priciest, {dear[0][1]}" + (f" and {dear[1][1]}" if len(dear) > 1 else "")
                 + f", charge {dear[-1][0] / me['outfit_price']:.1f}–"
                 f"{dear[0][0] / me['outfit_price']:.1f}× Dagacci's price, so shoppers pay up for "
                 "brand and fabric story.")
    if pos == 1:
        read += " Dagacci owns the value end; the risk is being seen as only cheap."
    bar_chart("Typical outfit price", "Median scrub set, or median top + median pant where a brand "
              "sells separates", "outfit_price", "usd", read)
    bar_chart("Share of products marked down", "Apparel showing a lower price than its compare-at "
              "price", "discounted_share", "pct",
              f"Dagacci shows a markdown on {pct(me['discounted_share'])} of apparel vs a peer median "
              f"of {pct(pm('discounted_share'))}. "
              + ("Heavier markdowns than peers train shoppers to wait for a sale; check that "
                 "the compare-at prices are real prices the products sold at."
                 if (me["discounted_share"] or 0) > (pm("discounted_share") or 0) + 0.1 else
                 "Markdowns are not out of line with the market."))
    bar_chart("New styles in the last 90 days", "Distinct styles first listed in the last 90 days",
              "new_90d", "num",
              f"Dagacci added {num(me['new_90d'])} new styles; the peer median is {num(pm('new_90d'))}. "
              "Frequent drops (new colors, limited prints) give paid social something new to say "
              "and bring repeat buyers back.")
    bar_chart("Share of options in stock", "Color/size options that can be bought now",
              "in_stock_share", "pct",
              f"Dagacci {pct(me['in_stock_share'])} vs peer median {pct(pm('in_stock_share'))}. "
              "Size availability is the quiet conversion killer in scrubs: buyers often need "
              "one exact size in a hospital-mandated color.")
    more = (me["colors_per_style"] or 0) >= (pm("colors_per_style") or 0)
    bar_chart("Colors per style", "Median colors offered per apparel style", "colors_per_style",
              "num", f"Dagacci offers a median of {me['colors_per_style']:.0f} colors per style vs "
                     f"{pm('colors_per_style'):.0f} for peers. Hospitals mandate colors by role, so "
                     "breadth of color is coverage of buyers, not decoration"
                     + (" — a real Dagacci strength to say out loud in listings and ads."
                        if more else "; this is a gap."))

    # Brand comparison table
    table_rows = [[c["name"], f"https://{c['store']}", m["styles"], m["products"],
                   m["outfit_price"], m["median_top"], m["median_pant"], m["discounted_share"],
                   m["avg_discount"], m["new_90d"], m["in_stock_share"]] for c, s, m in rows]
    charts.append({"section": "Brands", "type": "table", "title": "Brand comparison",
                   "sub": "Each brand's own store, today",
                   "columns": [{"label": "Brand"}, {"label": "Styles", "fmt": "int"},
                               {"label": "Products", "fmt": "int"},
                               {"label": "Outfit", "fmt": "usd"}, {"label": "Top", "fmt": "usd"},
                               {"label": "Pant", "fmt": "usd"},
                               {"label": "Marked down", "fmt": "pct"},
                               {"label": "Typical markdown", "fmt": "pct"},
                               {"label": "New (90d)", "fmt": "int"},
                               {"label": "In stock", "fmt": "pct"}],
                   "rows": table_rows, "sort": 3})

    # Trends: price moves per brand
    trend_rows = []
    for c, s, m in rows:
        pts = series(days, c["key"], ("store", "outfit_price"))
        d7 = delta(pts, 7)
        prods = series(days, c["key"], ("store", "products"))
        dp = delta(prods, 7)
        trend_rows.append([c["name"], None, m["outfit_price"],
                           (d7[0] / d7[1] - 1) if d7 and d7[1] else None,
                           (dp[0] - dp[1]) if dp else None])
    charts.append({"section": "Trends", "type": "table", "title": "Week-over-week moves",
                   "sub": "Change in typical outfit price and product count vs ~7 days earlier",
                   "read": ("Changes appear after the second daily run." if len(days) < 2 else
                            "A brand cutting its outfit price or adding many products in a week is "
                            "usually running a promotion or a launch; check their site and socials."),
                   "columns": [{"label": "Brand"}, {"label": "Outfit", "fmt": "usd"},
                               {"label": "Price change", "fmt": "pct_signed"},
                               {"label": "Products added", "fmt": "int_signed"}],
                   "rows": trend_rows})
    if len(days) >= 2:
        charts.append({"section": "Trends"} | lines_chart(
            "Typical outfit price over time", "Dagacci vs peer median, daily",
            "The peer line is the median of the other brands each day.",
            [{"name": self_cfg["name"], "color": SELF,
              "points": series(days, self_cfg["key"], ("store", "outfit_price"))},
             {"name": "Peer median", "color": "muted", "points": [
                 [d["date"], _med([d["brands"].get(c["key"], {}).get("store", {}).get("outfit_price")
                                   for c in peer_cfgs])] for d in days]}], "usd"))

    if (pm("colors_per_style") or 0) > (me["colors_per_style"] or 0) * 1.5:
        opps.append({"priority": "Medium", "title": "Widen colors on the best sellers",
                     "text": f"Peers offer a median {pm('colors_per_style'):.0f} colors per style vs "
                     f"Dagacci's {me['colors_per_style']:.0f}. Adding hospital-standard colors to the "
                     "top-rated sets covers more buyers without new patterns."})
    premium = [c["name"] for c, s, m in rows if not s and (m["outfit_price"] or 0) > 2.5 * (me["outfit_price"] or 1e9)]
    if premium:
        opps.append({"priority": "Medium", "title": "A “premium” line has room",
                     "text": f"{', '.join(premium)} sell sets at 2.5×+ Dagacci's price. A small "
                     "premium-fabric line ($40–55) could capture shoppers trading up without "
                     "touching the value core."})
    return {"id": "market", "label": "Scrub market", "tiles": [], "charts": charts,
            "opportunities": opps, "changes": [],
            "note": "From each brand's own online store (Shopify catalog feeds). Product counts "
                    "aren't comparable across brands that list each color separately; use "
                    "styles and prices."}


def build(snaps: list[dict[str, Any]], config: dict[str, dict]) -> dict[str, Any]:
    cfgs = [{"key": k, **v} for k, v in config.items()]
    days = snapshot_metrics(snaps)
    self_cfg = next(c for c in cfgs if c["role"] == "self")
    rival = next((c for c in cfgs if c["role"] == "rival"), None)
    peer_cfgs = [c for c in cfgs if c["role"] == "peer"]
    peers = {c["key"]: days[-1]["brands"][c["key"]]["store"] for c in peer_cfgs
             if days[-1]["brands"].get(c["key"], {}).get("store")}
    tabs = [self_tab(self_cfg, days, snaps, peers)]
    if rival:
        tabs.append(rival_tab(self_cfg, rival, days, snaps))
    if peer_cfgs:
        tabs.append(market_tab(self_cfg, peer_cfgs, days))
    order = {"High": 0, "Medium": 1, "Watch": 2, "Data": 3}
    for t in tabs:
        t["opportunities"].sort(key=lambda o: order.get(o["priority"], 9))
    errors = [f"{b['name']}: {e}" for b in snaps[-1]["brands"].values() for e in b["errors"]]
    return {"date": snaps[-1]["date"], "collected_at": snaps[-1]["collected_at"],
            "days": len(snaps), "first_date": snaps[0]["date"], "tabs": tabs, "errors": errors}
