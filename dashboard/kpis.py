"""KPI layer: normalized tables in, one JSON-able dict out."""
import collections
import datetime as dt
import json
import os
import re

SIZE_ALIASES = {
    "XXXXXL": "5XL", "XXXXX-Large": "5XL", "5X": "5XL", "5XL": "5XL", "5X-Large": "5XL",
    "XXXXL": "4XL", "XXXX-Large": "4XL", "4X": "4XL", "4XL": "4XL", "4X-Large": "4XL",
    "XXXL": "3XL", "XXX-Large": "3XL", "3X": "3XL", "3XL": "3XL", "3X-Large": "3XL",
    "XXL": "2XL", "XX-Large": "2XL", "2X": "2XL", "2XL": "2XL", "2X-Large": "2XL",
    "XL": "XL", "X-Large": "XL", "Large": "L", "L": "L", "Medium": "M", "M": "M",
    "Small": "S", "S": "S", "XS": "XS", "X-Small": "XS",
    "XXS": "XXS", "XX-Small": "XXS", "2XS": "XXS",
}
SIZE_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL"]
_SIZE_RE = [(re.compile(r"(?<![A-Za-z0-9-])" + re.escape(k) + r"(?![A-Za-z0-9-])"), v)
            for k, v in sorted(SIZE_ALIASES.items(), key=lambda kv: -len(kv[0]))]

# Order matters: first match wins.
PRODUCT_LINES = [
    ("Jogger", ("jogger",)),
    ("Men's cargo", ("men's v-neck cargo", "mens v-neck cargo", "scrubs for men set")),
    ("Y-neck stretch", ("y-neck", "y neck")),
    ("Stretch V-neck", ("4-way stretch contrast", "natural stretch")),
    ("Caps", ("scrub cap",)),
    ("Jackets", ("jacket",)),
    ("Pants only", ("scrubs pants", "scrub pants")),
    ("Tops only", ("shirts top ", "scrub top,", "scrubs top")),
    ("Classic unisex set", ("unisex", "womens and mens scrub", "women and man scrub", "women and men scrub")),
    ("Classic women's set", ("womens medical scrub set", "women's medical scrub set", "women's scrubs set")),
]


def size_of(title):
    """Size is the last size token in the title (titles end '..., Navy, Large')."""
    best = None
    for rx, v in _SIZE_RE:
        for m in rx.finditer(title):
            if best is None or m.start() > best[0]:
                best = (m.start(), v)
    return best[1] if best else None


def line_of(title):
    t = title.lower()
    for name, keys in PRODUCT_LINES:
        if any(k in t for k in keys):
            return name
    return "Other"


def _share(a, b):
    return 100.0 * a / b if b else None


def _segment(query):
    q = query.lower()
    if "dagacci" in q:
        return "Branded (dagacci)"
    if re.search(r"uniforme|enfermer|medicos|médicos|mujer|hombre", q):
        return "Spanish-language"
    if re.search(r"\b(women|womens|woman|ladies|womans)\b|women's", q):
        return "Women's"
    if re.search(r"\b(men|mens|man|male)\b|men's", q):
        return "Men's"
    return "Unspecified gender"


def customer_kpis(rows):
    rows = sorted(rows, key=lambda r: r["week_end"])
    cur, prev = rows[-1], rows[-2] if len(rows) > 1 else None

    def wk(r):
        return {
            "week_end": r["week_end"], "sales": r["sales"], "orders": r["orders"],
            "customers": r["customers"],
            "aov": r["sales"] / r["orders"] if r["orders"] else None,
            "ntb_share": _share(r["ntb_customers"], r["customers"]),
            "ntb_sales_share": _share(r["ntb_sales"], r["sales"]),
            "repeat_rate": 100 * r["repeat_rate"] if r["repeat_rate"] is not None else None,
        }

    series = [wk(r) for r in rows]
    last4 = sum(r["sales"] for r in rows[-4:])
    prior4 = sum(r["sales"] for r in rows[-8:-4]) if len(rows) >= 8 else None
    peak = max(rows, key=lambda r: r["sales"])
    return {
        "latest": series[-1],
        "prior": series[-2] if prev else None,
        "series": series,
        "last4_sales": last4,
        "prior4_sales": prior4,
        "peak_week": {"week_end": peak["week_end"], "sales": peak["sales"]},
    }


def search_kpis(rows):
    weeks = sorted({r["week_end"] for r in rows})
    stages = [("Impressions", "impr"), ("Clicks", "clicks"), ("Cart adds", "cart"), ("Purchases", "purch")]
    funnel = []
    for w in weeks:
        wr = [r for r in rows if r["week_end"] == w]
        funnel.append({"week_end": w, **{
            label: _share(sum(r[f"{k}_brand"] for r in wr), sum(r[f"{k}_total"] for r in wr))
            for label, k in stages}})

    # Pool all weeks per query for stability.
    pooled = {}
    for r in rows:
        p = pooled.setdefault(r["query"], collections.defaultdict(float, query=r["query"]))
        for k in ("volume", "impr_total", "impr_brand", "clicks_total", "clicks_brand",
                  "cart_total", "cart_brand", "purch_total", "purch_brand"):
            p[k] += r[k]
        p["weeks"] += 1
        if r["purch_price_mkt"]:
            p["_pm"] += r["purch_price_mkt"]; p["_pmn"] += 1
        if r["purch_price_brand"]:
            p["_pb"] += r["purch_price_brand"]; p["_pbn"] += 1

    segs = collections.defaultdict(lambda: collections.defaultdict(float))
    for p in pooled.values():
        s = segs[_segment(p["query"])]
        for k in ("volume", "impr_total", "impr_brand", "purch_total", "purch_brand"):
            s[k] += p[k]
    n = len(weeks)
    segments = sorted(({
        "segment": name,
        "weekly_searches": s["volume"] / n,
        "impr_share": _share(s["impr_brand"], s["impr_total"]),
        "purch_share": _share(s["purch_brand"], s["purch_total"]),
        "weekly_brand_purchases": s["purch_brand"] / n,
        "weekly_market_purchases": s["purch_total"] / n,
    } for name, s in segs.items()), key=lambda x: -x["weekly_searches"])

    queries = []
    for p in pooled.values():
        if "dagacci" in p["query"].lower():
            continue
        imp, pur = _share(p["impr_brand"], p["impr_total"]), _share(p["purch_brand"], p["purch_total"])
        flag = None
        if imp is not None and pur is not None and p["purch_total"] >= 30:
            if pur >= 1.5 * imp and imp < 10:
                flag = "Under-exposed"   # shoppers buy when they see it; get seen more
            elif imp >= 4 and pur < 0.5 * imp:
                flag = "Leaking"         # seen but not bought; fix offer/listing
        queries.append({
            "query": p["query"], "segment": _segment(p["query"]),
            "weekly_searches": p["volume"] / p["weeks"],
            "impr_share": imp,
            "click_share": _share(p["clicks_brand"], p["clicks_total"]),
            "purch_share": pur,
            "weekly_brand_purchases": p["purch_brand"] / p["weeks"],
            "price_mkt": p["_pm"] / p["_pmn"] if p["_pmn"] else None,
            "price_brand": p["_pb"] / p["_pbn"] if p["_pbn"] else None,
            "flag": flag,
        })
    queries.sort(key=lambda q: -q["weekly_searches"])
    branded = sum(p["purch_brand"] for p in pooled.values() if "dagacci" in p["query"].lower())
    total_brand = sum(p["purch_brand"] for p in pooled.values())
    return {
        "weeks": weeks,
        "funnel": funnel,
        "segments": segments,
        "queries": queries[:60],
        "flag_counts": collections.Counter(q["flag"] for q in queries if q["flag"]),
        "branded_purchase_share": _share(branded, total_brand),
        "query_count": len(pooled),
    }


def listing_kpis(rows):
    new = [r for r in rows if r["condition"] == "new"]
    orders = sum(r["orders"] for r in new)
    rr = [r for r in new if r["return_rate"] is not None]
    w_return = sum(r["return_rate"] * r["orders"] for r in rr) / max(1, sum(r["orders"] for r in rr))
    ncx = sum(r["ncx_orders"] for r in new)

    reasons = collections.Counter()
    fit = collections.defaultdict(lambda: {"Too Large": 0.0, "Too Small": 0.0, "orders": 0})
    by_line = collections.defaultdict(lambda: collections.defaultdict(float))
    for r in new:
        size, line = size_of(r["title"]), line_of(r["title"])
        if r["top_ncx_reason"]:
            # Only the top reason is reported; attribute that share of NCX orders to it.
            attributed = r["ncx_orders"] * (r["top_ncx_share"] or 100) / 100
            reasons[r["top_ncx_reason"]] += attributed
            if size and r["top_ncx_reason"] in ("Too Large", "Too Small"):
                fit[size][r["top_ncx_reason"]] += attributed
        if size:
            fit[size]["orders"] += r["orders"]
        b = by_line[line]
        b["orders"] += r["orders"]; b["ncx"] += r["ncx_orders"]; b["skus"] += 1
        if r["return_rate"] is not None:
            b["ret_w"] += r["return_rate"] * r["orders"]; b["ret_o"] += r["orders"]
        if r["stars"]:
            b["stars_w"] += r["stars"] * r["orders"]; b["stars_o"] += r["orders"]
        if r["return_badge"] in ("Yes", "At risk"):
            b["badge_orders"] += r["orders"]

    badge = {k: sum(r["orders"] for r in new if r["return_badge"] == k) for k in ("Yes", "At risk")}
    watch = sorted(new, key=lambda r: -r["orders"])[:15]
    return {
        "skus": len(new),
        "orders": orders,
        "return_rate": w_return,
        "ncx_rate": _share(ncx, orders),
        "reasons": [{"reason": k, "orders": v} for k, v in reasons.most_common()],
        "fit_by_size": [{"size": s, **fit[s]} for s in SIZE_ORDER if s in fit],
        "badge_orders": badge,
        "badge_share": _share(badge["Yes"] + badge["At risk"], orders),
        "lines": sorted(({
            "line": k, "orders": v["orders"], "skus": int(v["skus"]),
            "return_rate": v["ret_w"] / v["ret_o"] if v["ret_o"] else None,
            "ncx_rate": _share(v["ncx"], v["orders"]),
            "stars": v["stars_w"] / v["stars_o"] if v["stars_o"] else None,
            "badge_share": _share(v["badge_orders"], v["orders"]),
        } for k, v in by_line.items()), key=lambda x: -x["orders"]),
        "watchlist": [{
            "asin": r["asin"], "title": r["title"], "line": line_of(r["title"]), "size": size_of(r["title"]),
            "orders": r["orders"], "return_rate": r["return_rate"], "ncx_rate": r["ncx_rate"],
            "stars": r["stars"], "top_reason": r["top_ncx_reason"], "top_share": r["top_ncx_share"],
            "badge": r["return_badge"] if r["return_badge"] != "--" else None,
        } for r in watch],
    }


def repeat_kpis(rows):
    by_line = collections.defaultdict(lambda: collections.defaultdict(float))
    for r in rows:
        b = by_line[line_of(r["title"])]
        b["orders"] += r["orders"]; b["customers"] += r["customers"]
        b["repeat_customers"] += r["repeat_customers"]; b["repeat_sales"] += r["repeat_sales"]
    weeks = len({r["week_end"] for r in rows}) or 1
    return {"lines": sorted(({
        "line": k, "weekly_orders": v["orders"] / weeks,
        "repeat_customer_share": _share(v["repeat_customers"], v["customers"]),
    } for k, v in by_line.items()), key=lambda x: -x["weekly_orders"])}


_SCRUB_WORDS = re.compile(r"scrub|uniform|nursing|medical|jogger|lab coat", re.I)


def basket_kpis(rows):
    kinds = collections.Counter()
    competitors = collections.Counter()
    complements = collections.Counter()
    for r in rows:
        for c in r["combos"]:
            weight = r["orders"] * c["pct"] / 100
            title = c["title"] or ""
            if "dagacci" in title.lower():
                kinds["Another Dagacci item"] += weight
            elif _SCRUB_WORDS.search(title):
                kinds["Competitor scrubs"] += weight
                name = " ".join(title.split()[:2])
                if not re.match(r"(scrubs?|medical|womens?|women's|nurse|uniform)\b", name, re.I):
                    competitors[name] += weight
            else:
                kinds["Non-scrub item"] += weight
                complements[" ".join(title.split()[:6])] += weight
    total = sum(kinds.values())
    return {
        "mix": [{"kind": k, "share": _share(v, total)} for k, v in kinds.most_common()],
        "competitors": [{"name": k, "weight": v} for k, v in competitors.most_common(8)],
        "complements": [{"name": k, "weight": v} for k, v in complements.most_common(8)],
    }


# KPIs from the proposal that need data we don't have yet, and where it will come from.
AWAITING = [
    {"kpi": "Conversion rate & sessions by child ASIN", "source": "SP-API GET_SALES_AND_TRAFFIC_REPORT",
     "status": "API mapped, needs keys"},
    {"kpi": "Return reasons by size (all reasons, not just top)", "source": "SP-API GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA",
     "status": "API mapped, needs keys"},
    {"kpi": "Stockout rate on core sizes, weeks of cover", "source": "SP-API GET_FBA_INVENTORY_PLANNING_DATA",
     "status": "API mapped, needs keys"},
    {"kpi": "ACoS, TACoS, new-to-brand from ads", "source": "Amazon Ads API (separate approval)",
     "status": "Not started"},
    {"kpi": "Contribution margin per unit", "source": "SP-API Finances / Date Range report",
     "status": "Not started"},
]


# ------------------------------------------------- sales, traffic, stock & ads

CORE_SKUS = 60  # best sellers whose stock and pages are watched one by one


def _short(title, n=70):
    t = re.sub(r"^Dagacci( Medical Uniform| Scrubs Medical Uniform)?\u00ae?\s*", "", title or "", flags=re.I)
    return t if len(t) <= n else t[:n - 1].rstrip() + "\u2026"


def ops_kpis(t):
    """From the warehouse tables: daily sales, traffic & Buy Box, stock cover, ads."""
    out = {}
    days = t.get("sales_daily") or []
    if days:
        last7, prior7 = days[-7:], days[-14:-7]
        out["sales"] = {
            "series": days, "through": days[-1]["date"], "from": days[0]["date"],
            "rev_7": sum(d["revenue"] for d in last7), "units_7": sum(d["units"] for d in last7),
            "rev_prior": sum(d["revenue"] for d in prior7), "units_prior": sum(d["units"] for d in prior7),
            "rev_total": sum(d["revenue"] for d in days), "units_total": sum(d["units"] for d in days),
        }
    rows = [r for r in t.get("traffic") or [] if r["sessions"]]
    if rows:
        sess = sum(r["sessions"] for r in rows)
        units = sum(r["units"] for r in rows)
        bb_rows = [r for r in rows if r["buy_box"] is not None]
        bb = sum(r["buy_box"] * r["sessions"] for r in bb_rows) / max(sum(r["sessions"] for r in bb_rows), 1)
        lines = collections.defaultdict(lambda: {"sessions": 0, "units": 0, "bb_w": 0.0})
        for r in rows:
            g = lines[line_of(r["title"] or "")]
            g["sessions"] += r["sessions"]
            g["units"] += r["units"]
            g["bb_w"] += (r["buy_box"] or 0) * r["sessions"]
        by_line = sorted(({"line": k, "sessions": v["sessions"], "units": v["units"],
                           "cr": _share(v["units"], v["sessions"]), "bb": _share(v["bb_w"], v["sessions"])}
                          for k, v in lines.items()), key=lambda r: -r["sessions"])
        page = lambda r: {"asin": r["asin"], "sku": r["sku"], "title": _short(r["title"]),  # noqa: E731
                          "line": line_of(r["title"] or ""), "size": size_of(r["title"] or ""),
                          "sessions": r["sessions"], "units": r["units"], "cr": _share(r["units"], r["sessions"]),
                          "bb": None if r["buy_box"] is None else 100 * r["buy_box"]}
        top = sorted(rows, key=lambda r: -r["sessions"])
        out["traffic"] = {
            "sessions": sess, "units": units, "cr": _share(units, sess), "bb": 100 * bb,
            "by_line": by_line, "top": [page(r) for r in top[:15]],
            "low_bb": [page(r) for r in top if r["sessions"] >= 1000 and r["buy_box"] is not None and r["buy_box"] < 0.9][:15],
        }
    stock = [r for r in t.get("stock") or [] if (r["velocity"] or 0) > 0]
    if stock:
        core = sorted(stock, key=lambda r: -r["velocity"])[:CORE_SKUS]
        cover = lambda r: r["on_hand"] / (r["velocity"] * 7)  # noqa: E731
        item = lambda r: {"sku": r["sku"], "asin": r["asin"], "title": _short(r["title"]),  # noqa: E731
                          "size": size_of(r["title"] or ""), "per_day": r["velocity"], "on_hand": r["on_hand"],
                          "inbound": r["inbound"] or 0, "weeks": cover(r)}
        all_rows = t.get("stock") or []
        out["stock"] = {
            "on_hand": sum(r["on_hand"] or 0 for r in all_rows),
            "inbound": sum(r["inbound"] or 0 for r in all_rows),
            "weeks": sum(r["on_hand"] or 0 for r in stock) / (sum(r["velocity"] for r in stock) * 7),
            "core": len(core), "core_out": sum(1 for r in core if (r["on_hand"] or 0) <= 0),
            "low": [item(r) for r in sorted(core, key=cover) if cover(r) < 3][:20],
        }
    ads = (t.get("ads") or [None])[0]
    if ads and ads["spend"]:
        days_n = (dt.date.fromisoformat(ads["period_end"]) - dt.date.fromisoformat(ads["period_start"])).days + 1
        out["ads"] = {**ads, "days": days_n, "acos": _share(ads["spend"], ads["ad_sales"]),
                      "tacos": _share(ads["spend"], ads["sales"]), "ad_share": _share(ads["ad_sales"], ads["sales"]),
                      "spend_month": ads["spend"] * 30 / days_n}
    store = (t.get("store") or [None])[0]
    if store and store["variants"]:
        out["store"] = store
    return out


def ops_insights(o):
    out = []
    st, tr, sa = o.get("stock"), o.get("traffic"), o.get("sales")
    if st and st["core_out"]:
        outs = [r for r in st["low"] if r["on_hand"] <= 0]
        names = ", ".join(r["sku"] for r in outs[:4])
        out.append({
            "section": "ops", "priority": 1, "effort": "Low",
            "title": f"{st['core_out']} of the {st['core']} best-selling variants are out of stock",
            "finding": f"Out of stock at Amazon: {names}{' and more' if len(outs) > 4 else ''}. "
                       f"{sum(1 for r in st['low'] if r['on_hand'] > 0)} more have under 3 weeks of cover.",
            "why": "A stocked-out size loses its sales and, after a few days, its search rank, which takes weeks to rebuild.",
            "actions": ["Send an inbound shipment for the out-of-stock sizes first (see Stock below).",
                        "Set a 6-week cover floor on the top 60 variants, sized to lead time.",
                        "Pause ads on sizes that are out, so spend moves to the sizes you can ship."],
            "kpi": "Best-selling variants in stock", "current": f"{100 * (1 - st['core_out'] / st['core']):.0f}%", "target": "100%",
        })
    elif st and st["low"]:
        out.append({
            "section": "ops", "priority": 2, "effort": "Low",
            "title": f"{len(st['low'])} best-selling variants have under 3 weeks of stock",
            "finding": "Lowest: " + ", ".join(f"{r['sku']} ({r['weeks']:.1f} wk)" for r in st["low"][:4]) + ".",
            "why": "Amazon inbound takes 1-3 weeks; below that, a stockout is already likely.",
            "actions": ["Ship inbound for these sizes this week."],
            "kpi": "Best sellers under 3 weeks of cover", "current": str(len(st["low"])), "target": "0",
        })
    if tr and tr["low_bb"]:
        lost = sum(r["sessions"] * (100 - r["bb"]) / 100 for r in tr["low_bb"])
        out.append({
            "section": "ops", "priority": 1, "effort": "Low",
            "title": f"Dagacci doesn't win the Buy Box on {len(tr['low_bb'])} high-traffic pages",
            "finding": "Lowest: " + ", ".join(f"{r['sku']} ({r['bb']:.0f}%)" for r in sorted(tr["low_bb"], key=lambda r: r["bb"])[:4])
                       + f". About {fmt_int(lost)} of last month's sessions landed when another seller had the Buy Box.",
            "why": "Without the Buy Box, the sale goes to another seller (or nobody) and Dagacci's ads pay for their traffic.",
            "actions": ["Check the other offers on these listings (Seller Central > the listing > Pricing).",
                        "Report unauthorized resellers through Brand Registry; match price where it's an authorized one.",
                        "Check these SKUs aren't stranded or low on stock (that also loses the Buy Box)."],
            "kpi": "Buy Box share, high-traffic pages", "current": f"{min(r['bb'] for r in tr['low_bb']):.0f}% lowest", "target": "\u2265 95%",
        })
    if tr:
        weak = [g for g in tr["by_line"] if g["sessions"] >= 20000 and g["cr"] and tr["cr"] and g["cr"] < 0.75 * tr["cr"]]
        if weak:
            g = weak[0]
            out.append({
                "section": "ops", "priority": 2, "effort": "Medium",
                "title": f"{g['line']} pages convert well below the brand average",
                "finding": f"{g['cr']:.1f}% of {fmt_int(g['sessions'])} sessions bought, against {tr['cr']:.1f}% across all pages.",
                "why": "Traffic is already there; a page that converts like the rest of the catalog adds sales without more ad spend.",
                "actions": ["Compare the main image, price and reviews with the best-converting line.",
                            "Lead with an on-model photo and the size chart (ties in with the fit fix)."],
                "kpi": f"{g['line']} unit session %", "current": f"{g['cr']:.1f}%", "target": f"{tr['cr']:.1f}%",
            })
    if sa and sa["rev_prior"] and sa["rev_7"] < 0.95 * sa["rev_prior"]:
        d = 100 * (sa["rev_7"] / sa["rev_prior"] - 1)
        out.append({
            "section": "ops", "priority": 2, "effort": "Low",
            "title": f"Amazon sales fell {abs(d):.0f}% last week",
            "finding": f"${fmt_int(sa['rev_7'])} in the 7 days to {sa['through']} vs ${fmt_int(sa['rev_prior'])} the week before "
                       f"({fmt_int(sa['units_7'])} vs {fmt_int(sa['units_prior'])} units).",
            "why": "Check the controllable causes before reading it as demand: stockouts, Buy Box, and ad budgets.",
            "actions": ["Work through the stock and Buy Box items above first."],
            "kpi": "Weekly Amazon sales", "current": f"${fmt_int(sa['rev_7'])}", "target": f"\u2265 ${fmt_int(sa['rev_prior'])}",
        })
    return out


def fmt_int(v):
    return f"{round(v):,}"


def build(tables, source_name):
    out = {"source": source_name, "have": sorted(tables), "awaiting": AWAITING}
    if "customer_trends" in tables:
        out["customers"] = customer_kpis(tables["customer_trends"])
    if "sqp" in tables:
        out["search"] = search_kpis(tables["sqp"])
    if "listing_health" in tables:
        out["listings"] = listing_kpis(tables["listing_health"])
    if "repeat_asin" in tables:
        out["repeat"] = repeat_kpis(tables["repeat_asin"])
    if "basket" in tables:
        out["basket"] = basket_kpis(tables["basket"])
    out.update(ops_kpis(tables))
    connected = {"Conversion rate & sessions by child ASIN": "traffic",
                 "Stockout rate on core sizes, weeks of cover": "stock",
                 "ACoS, TACoS, new-to-brand from ads": "ads"}
    out["awaiting"] = [a for a in AWAITING if connected.get(a["kpi"]) not in out]
    out["insights"] = annotate(sorted(insights(out) + ops_insights(out), key=lambda i: i["priority"]), out)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets.json")) as fh:
        out["scorecard"] = scorecard(out, json.load(fh))
    out["changes"] = changes(out)
    return out


# ---------------------------------------------------------------------- insights

_GAP_PRODUCT = re.compile(r"\b(caps?|pants?|jackets?|tops?)\b", re.I)
_COMPETITOR = re.compile(r"\b(figs|cherokee|grey'?s|greys|healing hands|fabletics|mandala|koi|barco|wonderwink|jaanuu)\b", re.I)


def pct_or_dash(seg):
    return f'{seg["purch_share"]:.1f}%' if seg and seg["purch_share"] is not None else "–"


def _names(rows, n=4):
    return ", ".join(f'"{r["query"]}"' for r in rows[:n])


def insights(k):
    """Rule-based findings computed from the KPIs, so they refresh with the data.

    Each insight: section, priority (1 = act first), effort, title, finding, why,
    actions[], kpi, current, target.
    """
    out = []
    L, S, C, B = k.get("listings"), k.get("search"), k.get("customers"), k.get("basket")

    if L:
        reasons = {r["reason"]: r["orders"] for r in L["reasons"]}
        big, small = reasons.get("Too Large", 0), reasons.get("Too Small", 0)
        fit_share = _share(big + small, sum(reasons.values()))
        sized = [r for r in L["fit_by_size"] if r["orders"] >= 300]
        worst = max(sized, key=lambda r: r["Too Large"] / r["orders"])
        fit_rate = 100 * sum(r["Too Large"] + r["Too Small"] for r in sized) / sum(r["orders"] for r in sized)
        if big > 1.5 * small:
            out.append({
                "section": "fit", "priority": 1, "effort": "Low",
                "title": "The products run large, and that drives most bad experiences",
                "finding": f'"Too large" outnumbers "too small" {big / small:.1f} to 1 and makes up {fit_share:.0f}% of '
                           f'bad-experience orders. It is worst at size {worst["size"]} '
                           f'({100 * worst["Too Large"] / worst["orders"]:.1f} per 100 orders).',
                "why": f'Fit returns are what put {L["badge_share"]:.0f}% of orders on listings with, or close to, '
                       'Amazon\'s "frequently returned" badge, which lowers conversion.',
                "actions": [
                    "Re-measure and update the size chart on every listing, starting with the watchlist SKUs.",
                    'Add "Runs large: size down if between sizes" to the first bullet and the main image.',
                    "Ask the factory to grade the core unisex pattern down half a size on the next production run.",
                ],
                "kpi": "Fit complaints per 100 orders", "current": f"{fit_rate:.1f}", "target": f"{fit_rate / 2:.1f}",
            })
        risky = [w for w in L["watchlist"] if w["badge"] and (w["top_reason"] or "").startswith("Too")]
        if risky:
            out.append({
                "section": "fit", "priority": 1, "effort": "Low",
                "title": f"{len(risky)} top-15 SKUs are badged or at risk because of fit",
                "finding": "Top sellers at risk: " + ", ".join(f'{w["asin"]} ({w["size"] or "?"}, {w["top_reason"].lower()})' for w in risky[:4]) + ".",
                "why": "Once Amazon shows the badge on a top seller, conversion on your highest-volume pages drops.",
                "actions": ["Fix these listings' size charts and bullets first, this week.",
                            "Check their return comments in Seller Central (Voice of the Customer → item → comments) for specific fit complaints."],
                "kpi": "Top-15 SKUs badged or at risk", "current": str(sum(1 for w in L["watchlist"] if w["badge"])), "target": "0",
            })
        healthy = [l for l in L["lines"] if l["stars"] and l["stars"] >= 4.25 and (l["badge_share"] or 0) < 10
                   and l["orders"] < 0.1 * L["orders"] and "stretch" in l["line"].lower()]
        if healthy:
            out.append({
                "section": "fit", "priority": 3, "effort": "Medium",
                "title": "The best-rated lines are under-promoted: a ready-made step-up line",
                "finding": "; ".join(f'{l["line"]}: {l["stars"]:.2f}★, {l["orders"]:,.0f} orders across {l["skus"]} SKUs' for l in healthy) + ".",
                "why": "Customers rate these higher than the classic set, but sales are spread thin across hundreds of SKUs. "
                       "Students who trade up to FIGS could trade up within Dagacci instead.",
                "actions": ["Concentrate ads and A+ content on the 10 best-selling colors and sizes of these lines.",
                            "Retire or merge SKUs with almost no sales so reviews pool on fewer listings.",
                            'Test positioning as the "working nurse" upgrade at $35–45.'],
                "kpi": "Stretch-line share of orders", "current": f'{_share(sum(l["orders"] for l in healthy), L["orders"]):.0f}%',
                "target": "15%",
            })
        caps = next((l for l in L["lines"] if l["line"] == "Caps"), None)
    else:
        caps = None

    if S:
        f = S["funnel"][-1]
        under = [q for q in S["queries"] if q["flag"] == "Under-exposed"]
        out.append({
            "section": "search", "priority": 2, "effort": "Medium",
            "title": "Shoppers buy Dagacci when they see it, but they rarely see it",
            "finding": f'Dagacci gets {f["Impressions"]:.1f}% of search impressions but {f["Purchases"]:.1f}% of purchases '
                       f'({f["Purchases"] / f["Impressions"]:.1f}× more). {len(under)} searches are flagged as under-exposed, '
                       f"led by {_names(under)}.",
            "why": "When conversion already beats visibility, every extra impression should pay back. Visibility is the constraint, not the listing.",
            "actions": ["Raise Sponsored Products bids on the under-exposed searches (exact match), starting with the highest volume.",
                        "Put these exact phrases in titles and backend search terms (for example, colors such as navy blue).",
                        "Re-check impression share weekly in this dashboard; stop raising bids where purchase share falls back to impression share."],
            "kpi": "Impression share, top 1,500 searches", "current": f'{f["Impressions"]:.1f}%', "target": f'{f["Impressions"] + 1:.1f}%',
        })
        seg = {s["segment"]: s for s in S["segments"]}
        strong = [s for s in (seg.get("Men's"), seg.get("Spanish-language")) if s and s["purch_share"] and s["purch_share"] > 2 * s["impr_share"]]
        if strong:
            out.append({
                "section": "search", "priority": 2, "effort": "Low",
                "title": "Men's and Spanish-language shoppers are Dagacci's strongest segments",
                "finding": " · ".join(f'{s["segment"]}: seen {s["impr_share"]:.1f}%, bought {s["purch_share"]:.1f}% '
                                      f'({s["weekly_searches"] / 1000:.0f}K searches/wk)' for s in strong) + ".",
                "why": "Fewer competitors target these shoppers, so Dagacci wins a big share of the purchases it's shown for.",
                "actions": ["Create a men's-only ad campaign featuring the men's cargo line.",
                            "Add Spanish keywords (uniformes médicos, uniformes de enfermera) to backend search terms and run a small Spanish keyword campaign.",
                            "Add a men's model to the main image of the unisex set."],
                "kpi": "Men's search purchase share", "current": pct_or_dash(seg.get("Men's")), "target": "15%",
            })
        leak = [q for q in S["queries"] if q["flag"] == "Leaking"]
        if leak:
            out.append({
                "section": "search", "priority": 3, "effort": "Low",
                "title": "Some women's-set searches see Dagacci but don't buy",
                "finding": "; ".join(f'"{q["query"]}": seen {q["impr_share"]:.1f}%, bought {q["purch_share"]:.1f}%' for q in leak[:3]) + ".",
                "why": "Price matches the market on these searches, so the likely cause is the listing shown: its images, rating, or size chart.",
                "actions": ["Search these phrases on Amazon and note which Dagacci listing appears and how it compares to the top 3 results.",
                            "Point ads for these phrases to the best-rated women's listing instead.",
                            "Confirm with page conversion once Business Reports are loaded."],
                "kpi": "Purchase share ÷ impression share on these searches", "current": f'{sum(q["purch_share"] for q in leak) / sum(q["impr_share"] for q in leak):.1f}×', "target": "≥1.0×",
            })
        gaps = [q for q in S["queries"] if q["weekly_searches"] >= 15000 and (q["impr_share"] or 0) < 1.5 and _GAP_PRODUCT.search(q["query"]) and not _COMPETITOR.search(q["query"])]
        if gaps:
            cap_note = f' The caps Dagacci already sells are its healthiest line ({caps["stars"]:.1f}★, {caps["return_rate"]:.0f}% returns).' if caps else ""
            out.append({
                "section": "search", "priority": 3, "effort": "Medium",
                "title": "Big searches for caps, pants and jackets where Dagacci barely shows up",
                "finding": "; ".join(f'"{q["query"]}" {q["weekly_searches"] / 1000:.0f}K/wk at {q["impr_share"]:.1f}% share' for q in gaps[:4]) + "." + cap_note,
                "why": "These are add-on items nurses buy every semester. Winning them raises order value and brings in new customers.",
                "actions": ["Launch ads for caps and pants on these exact searches.",
                            "Add more cap colors and prints. Caps have only a few SKUs today but sell over 1,000 orders.",
                            "Offer a set + cap bundle."],
                "kpi": "Impression share on cap/pant/jacket searches", "current": f'{sum(q["impr_share"] for q in gaps) / len(gaps):.1f}%', "target": "3%",
            })
        comp = [q for q in S["queries"] if _COMPETITOR.search(q["query"]) and q["weekly_searches"] >= 5000]
        if comp:
            c0 = comp[0]
            out.append({
                "section": "search", "priority": 4, "effort": "Low",
                "title": "Test a lower-price alternative to FIGS and other premium brands",
                "finding": "; ".join(f'"{q["query"]}" {q["weekly_searches"] / 1000:.0f}K/wk at {q["impr_share"]:.1f}% share' for q in comp[:3]) +
                           (f'. Market median ${c0["price_mkt"]:.0f} vs Dagacci ${c0["price_brand"]:.0f}.' if c0["price_mkt"] and c0["price_brand"] else "."),
                "why": "Price-conscious shoppers search premium brands first. A clear value message can win some of them at a fraction of the price.",
                "actions": ["Run a small, capped Sponsored Brands test on competitor-name searches with a value headline.",
                            "Judge it on new-to-brand orders, not total ad cost."],
                "kpi": "Purchase share on competitor-brand searches", "current": f'{sum(q["purch_share"] or 0 for q in comp) / len(comp):.1f}%', "target": "1%",
            })

    if C:
        s = C["series"]
        early = sum(r["repeat_rate"] for r in s[:4]) / 4
        late = sum(r["repeat_rate"] for r in s[-4:]) / 4
        if late < early:
            out.append({
                "section": "customers", "priority": 2, "effort": "Medium",
                "title": "Fewer customers are coming back",
                "finding": f"The repeat purchase rate averaged {early:.1f}% over the first 4 weeks and {late:.1f}% over the last 4. "
                           f"About {s[-1]['ntb_share']:.0f}% of each week's customers are new to the brand.",
                "why": "Dagacci is a first-scrub brand. Most buyers are students who later trade up elsewhere, so each lost repeat buyer costs future orders.",
                "actions": ['Offer 2- and 3-set multi-packs ("one for every shift").',
                            "Use Brand Tailored Promotions in Seller Central to target past buyers with a second-set discount.",
                            "Add a package insert pointing to the step-up stretch lines."],
                "kpi": "Repeat purchase rate", "current": f"{s[-1]['repeat_rate']:.1f}%", "target": f"{early + 1:.1f}%",
            })
        peak = max(s, key=lambda r: r["sales"])
        calm = sorted(s, key=lambda r: r["sales"])[: len(s) // 2]
        calm_aov = sum(r["aov"] for r in calm) / len(calm)
        out.append({
            "section": "customers", "priority": 3, "effort": "Low",
            "title": "Peaks are predictable, and the buyers in them spend less per order",
            "finding": f"Sales peaked at ${peak['sales'] / 1000:.0f}K the week ending {peak['week_end']}. "
                       f"Order value at the peak was ${peak['aov']:.2f} vs ${calm_aov:.2f} in quieter weeks, "
                       f"and {peak['ntb_share']:.0f}% of peak customers were new to the brand.",
            "why": "Peaks bring a wave of first-time, one-set buyers (back-to-school and nursing-program starts). That's the cheapest moment to win them and sell them more.",
            "actions": ["Stock core sizes and raise ad budgets 2–3 weeks before late July, late August and January.",
                        "Feature bundles (set + cap, 3-pack) during peak weeks to lift order value.",
                        "Check stock on the core sizes before each peak (section 5, Stock on the best sellers)."],
            "kpi": "Peak-week order value", "current": f"${peak['aov']:.2f}", "target": f"${calm_aov:.2f}",
        })

    if B:
        comp = next((m for m in B["mix"] if m["kind"] == "Competitor scrubs"), None)
        if comp and comp["share"] >= 5:
            names = ", ".join(c["name"] for c in B["competitors"][:3])
            out.append({
                "section": "basket", "priority": 3, "effort": "Low",
                "title": "Some shoppers buy a competitor's scrubs in the same order",
                "finding": f'{comp["share"]:.0f}% of co-purchased items are competitor scrubs, most often {names}.',
                "why": "Buying two brands at once usually means hedging on fit or style and planning to return one. This ties back to the sizing problem.",
                "actions": ["Fix the size chart (see Fit) so shoppers commit to one brand.",
                            "Target Sponsored Display ads at these competitors' product pages.",
                            "Compare Dagacci's size chart to Cherokee's and call out the difference."],
                "kpi": "Competitor share of co-purchases", "current": f'{comp["share"]:.0f}%', "target": f'{comp["share"] / 2:.0f}%',
            })

    out.sort(key=lambda i: i["priority"])
    return out


# ------------------------------------------------------- tracking, impact, scorecard

# Stable id and owning team per recommendation, keyed on the KPI it tracks (titles
# carry this week's numbers, KPIs don't). The id keeps the dashboard's action
# tracker rows (owner, status, due date) attached to the same item week to week.
KPI_IDS = [
    ("Fit complaints per 100 orders", "fit-runs-large", "Product"),
    ("Top-15 SKUs badged or at risk", "fit-top-skus", "Amazon ops"),
    ("Stretch-line share of orders", "fit-step-up-line", "Marketing"),
    ("Impression share, top 1,500 searches", "search-visibility", "Amazon ops"),
    ("Men's search purchase share", "search-segments", "Marketing"),
    ("Purchase share ÷ impression share on these searches", "search-leaking", "Creative"),
    ("Impression share on cap/pant/jacket searches", "search-gaps", "Product"),
    ("Purchase share on competitor-brand searches", "search-competitors", "Marketing"),
    ("Repeat purchase rate", "customers-repeat", "Marketing"),
    ("Peak-week order value", "customers-peaks", "Amazon ops"),
    ("Competitor share of co-purchases", "basket-competitors", "Creative"),
    ("Best-selling variants in stock", "ops-stockouts", "Amazon ops"),
    ("Best sellers under 3 weeks of cover", "ops-low-cover", "Amazon ops"),
    ("Buy Box share, high-traffic pages", "ops-buybox", "Amazon ops"),
    ("unit session %", "ops-line-conversion", "Creative"),
    ("Weekly Amazon sales", "ops-sales-drop", "Amazon ops"),
]
WEEKS_PER_MONTH = 4.33


def _range(mid, assumption, lo=0.5, hi=1.0):
    """Monthly sales impact as a range: `lo`..`hi` of the full-effect estimate."""
    return {"low": round(mid * lo, -2), "high": round(mid * hi, -2), "assumption": assumption}


def impact(i, k):
    """Estimated monthly sales at stake if the recommendation hits its target.
    Sales dollars only (no margin data yet); None where there's no defensible basis."""
    C, S, L, B = k.get("customers"), k.get("search"), k.get("listings"), k.get("basket")
    sa, st, tr = k.get("sales"), k.get("stock"), k.get("traffic")
    month = C["last4_sales"] / 4 * WEEKS_PER_MONTH if C else None
    aov = C["latest"]["aov"] if C else None
    unit_price = sa["rev_total"] / sa["units_total"] if sa and sa["units_total"] else aov
    mkt = sum(s["weekly_market_purchases"] for s in S["segments"]) if S else 0
    conv = mkt / sum(s["weekly_searches"] for s in S["segments"]) if S else 0  # purchases per search
    iid = i["id"]

    if iid == "fit-runs-large" and L and month:
        reasons = {r["reason"]: r["orders"] for r in L["reasons"]}
        fit = _share(reasons.get("Too Large", 0) + reasons.get("Too Small", 0), sum(reasons.values())) / 100
        return _range(month * L["return_rate"] / 100 * fit * 0.5,
                      f"Halving fit complaints keeps half of fit-related returns: monthly sales × {L['return_rate']:.1f}% "
                      f"returns × {fit:.0%} fit share × 50%. Low end assumes half of those would have been exchanged anyway.")
    if iid == "fit-top-skus" and L and month:
        risky = [w for w in L["watchlist"] if w["badge"]]
        share = sum(w["orders"] for w in risky) / L["orders"]
        return _range(month * share * 0.10,
                      f"The badged or at-risk top sellers carry {share:.0%} of orders; keeping the badge off "
                      "protects 5–10% of their sales (typical conversion hit from the badge).")
    if iid == "search-visibility" and S and aov:
        f = S["funnel"][-1]
        ratio = f["Purchases"] / f["Impressions"]
        return _range(mkt * 0.01 * ratio * aov * WEEKS_PER_MONTH,
                      f"+1 pt impression share on the top searches ({fmtN(mkt)} purchases/wk), converting at today's "
                      f"{ratio:.1f}× ratio (high) or half of it (low), × ${aov:.0f} order value. Before extra ad spend.")
    if iid == "search-segments" and S and aov:
        men = next((s for s in S["segments"] if s["segment"] == "Men's"), None)
        if men and men["purch_share"] < 15:
            return _range(men["weekly_market_purchases"] * (15 - men["purch_share"]) / 100 * aov * WEEKS_PER_MONTH,
                          f"Men's purchase share from {men['purch_share']:.1f}% to 15% of "
                          f"{fmtN(men['weekly_market_purchases'])} men's purchases/wk × ${aov:.0f}. Spanish-language not counted.")
    if iid == "search-leaking" and S:
        leak = [q for q in S["queries"] if q["flag"] == "Leaking" and q["purch_share"]]
        orders = sum(q["weekly_brand_purchases"] * (q["impr_share"] / q["purch_share"] - 1) for q in leak)
        price = aov or 0
        return _range(orders * price * WEEKS_PER_MONTH,
                      "On the leaking searches, purchase share rises to match impression share at today's "
                      f"traffic, × ${price:.0f} order value.")
    if iid == "search-gaps" and S:
        gaps = [q for q in S["queries"] if q["weekly_searches"] >= 15000 and (q["impr_share"] or 0) < 1.5
                and _GAP_PRODUCT.search(q["query"]) and not _COMPETITOR.search(q["query"])]
        val = sum(q["weekly_searches"] * conv * max(0, 3 - (q["impr_share"] or 0)) / 100 * (q["price_mkt"] or 0) for q in gaps)
        return _range(val * WEEKS_PER_MONTH,
                      f"3% share of purchases on these searches ({conv:.1%} of searches end in a purchase across "
                      "the top searches) at each search's market median price.")
    if iid == "search-competitors" and S:
        comp = [q for q in S["queries"] if _COMPETITOR.search(q["query"]) and q["weekly_searches"] >= 5000]
        val = sum(q["weekly_searches"] * conv * max(0, 1 - (q["purch_share"] or 0)) / 100 * (q["price_brand"] or aov or 0) for q in comp)
        return _range(val * WEEKS_PER_MONTH,
                      f"1% purchase share on competitor-name searches ({conv:.1%} of searches buy) at Dagacci's price. "
                      "A test: low end assumes half the target.")
    if iid == "customers-repeat" and C and aov:
        s = C["series"]
        target = sum(r["repeat_rate"] for r in s[:4]) / 4 + 1
        gain = max(0, target - s[-1]["repeat_rate"]) / 100
        return _range(C["latest"]["customers"] * gain * aov * WEEKS_PER_MONTH,
                      f"Repeat rate from {s[-1]['repeat_rate']:.1f}% to {target:.1f}% of "
                      f"{fmtN(C['latest']['customers'])} weekly customers, each placing one more ${aov:.0f} order.")
    if iid == "customers-peaks" and C:
        s = C["series"]
        peak4 = sorted(s, key=lambda r: r["sales"])[-4:]
        calm = sorted(s, key=lambda r: r["sales"])[: len(s) // 2]
        gap = sum(r["aov"] for r in calm) / len(calm) - sum(r["aov"] for r in peak4) / 4
        per_season = sum(r["orders"] for r in peak4) * max(0, gap)
        return _range(per_season * 2 / 12,
                      f"Peak-week order value up ${gap:.2f} to the quiet-week level on {fmtN(sum(r['orders'] for r in peak4))} "
                      "orders in the 4 peak weeks; 2 peak seasons a year (back-to-school, January), spread per month.")
    if iid == "ops-stockouts" and st and unit_price:
        outs = [r for r in st["low"] if r["on_hand"] <= 0]
        return _range(sum(r["per_day"] for r in outs) * 30 * unit_price,
                      f"Units/day of the out-of-stock best sellers × 30 days × ${unit_price:.0f} per unit. "
                      "Low end assumes half the shoppers buy another size or color.")
    if iid == "ops-line-conversion" and tr and unit_price:
        weak = [g for g in tr["by_line"] if g["sessions"] >= 20000 and g["cr"] and tr["cr"] and g["cr"] < 0.75 * tr["cr"]]
        if weak:
            g = weak[0]
            return _range(g["sessions"] * (tr["cr"] - g["cr"]) / 100 * unit_price,
                          f"{g['line']} converting at the brand average ({tr['cr']:.1f}% vs {g['cr']:.1f}%) on its "
                          f"{fmtN(g['sessions'])} monthly sessions × ${unit_price:.0f} per unit.")
    return None


def fmtN(v):
    return f"{v / 1000:.1f}K" if v >= 1000 else f"{round(v)}"


def annotate(insights_, k):
    for i in insights_:
        for kpi, iid, team in KPI_IDS:
            if i["kpi"] == kpi or (kpi.startswith("unit") and i["kpi"].endswith(kpi)):
                i["id"], i["team"] = iid, team
                break
        else:
            i["id"], i["team"] = re.sub(r"[^a-z0-9]+", "-", i["kpi"].lower()).strip("-"), "Amazon ops"
        i["impact"] = impact(i, k)
    return insights_


def scorecard(k, targets):
    """The 4 headline KPIs vs target, with last week's value where the data has one."""
    C, S, L = k.get("customers"), k.get("search"), k.get("listings")
    vals = {}
    if C:
        vals["weekly_sales"] = (C["latest"]["sales"], C["prior"]["sales"], f"Week ending {C['latest']['week_end']}, Brand Analytics")
        vals["repeat_rate"] = (C["latest"]["repeat_rate"], C["prior"]["repeat_rate"], "Share of the week's customers who bought before")
    if L:
        vals["return_rate"] = (L["return_rate"], None, "Voice of the Customer, new-condition listings (rolling window)")
    if S:
        f = S["funnel"]
        vals["search_purchase_share"] = (f[-1]["Purchases"], f[-2]["Purchases"] if len(f) > 1 else None,
                                         "Dagacci's share of purchases on the top 1,500 searches")
    out = []
    for key, t in targets.items():
        if key.startswith("_") or key not in vals:
            continue
        now, prior, note = vals[key]
        up = t["better"] == "up"
        gap = (now - t["target"]) if up else (t["target"] - now)  # >= 0 means on target
        status = "on" if gap >= 0 else "close" if abs(gap) <= 0.1 * t["target"] else "off"
        out.append({"key": key, "label": t["label"], "unit": t["unit"], "better": t["better"], "now": now,
                    "prior": prior, "target": t["target"], "status": status, "note": note})
    return out


def changes(k):
    """What moved since last week, biggest relative change first."""
    C, S = k.get("customers"), k.get("search")
    rows = []
    if C:
        a, b = C["latest"], C["prior"]
        rows += [("Weekly sales", a["sales"], b["sales"], "$", True), ("Orders", a["orders"], b["orders"], "n", True),
                 ("Average order value", a["aov"], b["aov"], "$", True),
                 ("New-to-brand share", a["ntb_share"], b["ntb_share"], "%", True),
                 ("Repeat customer rate", a["repeat_rate"], b["repeat_rate"], "%", True)]
    if S and len(S["funnel"]) > 1:
        a, b = S["funnel"][-1], S["funnel"][-2]
        rows += [("Impression share, top searches", a["Impressions"], b["Impressions"], "%", True),
                 ("Purchase share, top searches", a["Purchases"], b["Purchases"], "%", True)]
    out = [{"label": l, "now": n, "prior": p, "unit": u, "good": (n >= p) == up,
            "rel": (n / p - 1) if p else 0} for l, n, p, u, up in rows]
    return sorted(out, key=lambda r: -abs(r["rel"]))
