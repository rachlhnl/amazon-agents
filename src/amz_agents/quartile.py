"""Amazon ad results by product from a Quartile export, and what they say.

Quartile (the agency running Dagacci's Amazon ads) has no open API. Its portal's
Reports > Product Performance > Amazon Products > ASIN Analysis table exports, per
ASIN for the chosen dates: ParentASIN, ASIN, Total Sales, TACoS, Ad Sales, Ad Spend,
ACoS, Name. `load` reads that export (.xlsx or .csv) into `ad_products`, and `view`
turns the latest period into the tracker's Amazon ads tab: totals, each style's
reliance on ads, sizes and colors, and findings to take to Quartile.

The export is a whole period, not daily, so it doesn't feed metrics_daily; Seller
Central (SP-API) or the Ads API do that.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .data.warehouse import Warehouse

COLUMNS = {"ParentASIN": "parent_asin", "ASIN": "asin", "Total Sales": "total_sales",
           "Ad Sales": "ad_sales", "Ad Spend": "ad_spend", "Name": "name"}
BRAND = re.compile(r"^((Dagacci( Medical Uniform| Scrubs Medical Uniform)?®?|Medical Uniform)\s*)+",
                   re.IGNORECASE)
SIZES = ["XXS", "XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL", "6XL"]
_SIZE = {"xx-small": "XXS", "xxs": "XXS", "2xs": "XXS", "x-small": "XS", "xs": "XS",
         "small": "S", "s": "S",
         "medium": "M", "m": "M", "large": "L", "l": "L", "x-large": "XL", "xl": "XL",
         "xx-large": "2XL", "xxl": "2XL", "2xl": "2XL", "2x": "2XL", "2x-large": "2XL",
         "xxx-large": "3XL", "xxxl": "3XL", "3xl": "3XL", "3x": "3XL", "3x-large": "3XL",
         "xxxxl": "4XL", "4xl": "4XL", "4x": "4XL", "4x-large": "4XL",
         "xxxxxl": "5XL", "5xl": "5XL", "5x": "5XL", "5x-large": "5XL", "6xl": "6XL"}
_COLOR_WORDS = {"grey": "gray", "seaform": "seafoam", "hotpink": "hot pink", "ceilblue": "ceil blue"}
_COLOR_SAME = {"royal": "royal blue", "navy blue": "navy", "olive green": "olive"}
# A finding needs this much behind it to be worth a conversation with the agency.
MIN_SPEND = 200.0
MIN_AD_SALES = 1000.0


# ---------------------------------------------------------------------- reading

def _num(v: Any) -> float:
    if v in (None, ""):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return float(str(v).replace("$", "").replace(",", "").replace("%", "") or 0)


def _period(text: str) -> tuple[str, str] | None:
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})", text or "")
    if not m:
        return None
    a, b = (datetime.strptime(x, "%m/%d/%Y").date().isoformat() for x in m.groups())
    return a, b


def read_export(path: str | Path) -> dict[str, Any]:
    """{"start", "end", "ad_types", "rows"} from the ASIN Analysis export. The dates and
    ad types come from the "Applied filters" note Power BI puts above the table."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl  # optional: pip install -e ".[xlsx]"

        sheet = openpyxl.load_workbook(path, data_only=True).active
        table = [list(r) for r in sheet.iter_rows(values_only=True)]
    else:
        with path.open(newline="", encoding="utf-8-sig") as f:
            table = list(csv.reader(f))
    head = next((i for i, r in enumerate(table) if "ASIN" in r and "Ad Spend" in r), None)
    if head is None:
        raise ValueError(f"{path.name}: no ASIN / Ad Spend header; export the ASIN Analysis table")
    note = " ".join(str(c) for r in table[:head] for c in r if c)
    cols = {COLUMNS[c]: i for i, c in enumerate(table[head]) if c in COLUMNS}
    rows = []
    for r in table[head + 1:]:
        asin = r[cols["asin"]] if len(r) > cols["asin"] else None
        if not asin or str(asin).strip().lower() == "total":
            continue
        rows.append({k: (_num(r[i]) if k in ("total_sales", "ad_sales", "ad_spend")
                         else str(r[i] or "").strip()) for k, i in cols.items()})
    period = _period(note)
    excluded = [t for t, code in (("Sponsored Brands", "SB"), ("Sponsored Display", "SD"))
                if re.search(rf"not[^\n]*\b{code}\b", note)]
    types = "Sponsored Products" if excluded else "all ad types"
    return {"start": period[0] if period else None, "end": period[1] if period else None,
            "ad_types": types, "rows": rows}


def load(wh: Warehouse, path: str | Path, start: str | None = None,
         end: str | None = None) -> dict[str, Any]:
    """Read an export into ad_products. `start`/`end` (YYYY-MM-DD) are only needed if
    the file has no "Date m/d/yyyy - m/d/yyyy" filter note."""
    ex = read_export(path)
    start, end = ex["start"] or start, ex["end"] or end
    if not (start and end):
        raise ValueError("the export doesn't say its dates: pass --start and --end")
    wh.execute("DELETE FROM ad_products WHERE period_start=? AND period_end=?", (start, end))
    wh.insert_many("ad_products", [{**r, "period_start": start, "period_end": end,
                                    "ad_types": ex["ad_types"], "source": "quartile"}
                                   for r in ex["rows"]])
    return {"start": start, "end": end, "rows": len(ex["rows"]), "ad_types": ex["ad_types"]}


# ------------------------------------------------------------------- analysis

def _norm_color(c: str) -> str:
    c = re.sub(r"[_()]+", " ", c).strip().lower()
    c = re.sub(r"\s+", " ", c)
    for a, b in _COLOR_WORDS.items():
        c = re.sub(rf"\b{a}\b", b, c)
    return _COLOR_SAME.get(c, c)


def _split(name: str) -> tuple[str | None, str | None, str]:
    """(size, color, what's left) from a variant name, when its end says them plainly:
    "..., Navy, Medium" or "... (Medium, Navy Blue)"."""
    paren = re.search(r"\(([^()]*)\)\s*$", name)
    if paren and "," in paren.group(1):
        parts, rest = [x.strip() for x in paren.group(1).split(",")], name[:paren.start()]
    else:
        parts = [x.strip() for x in name.split(",") if x.strip()]
        if len(parts) < 3:
            return None, None, name
        rest, parts = ", ".join(parts[:-2]), parts[-2:]
    size = next((_SIZE[x.lower()] for x in parts if x.lower() in _SIZE), None)
    color = next((x for x in parts if x.lower() not in _SIZE), None)
    return size, (_norm_color(color) if color else None), rest


def colors_known(names: list[str]) -> set[str]:
    """Color names learned from the variant names that spell them out."""
    return {c for c in (_split(n)[1] for n in names) if c and len(c) <= 20}


def variant(name: str, known: set[str]) -> tuple[str | None, str | None]:
    """(size, color) of a variant, e.g. ("M", "navy")."""
    size, color, _ = _split(name)
    if size or color:
        return size, color
    words = [w.strip("(),_") for w in BRAND.sub("", name).split()]
    words = [w for w in words if w]
    tail = words[-5:]
    for i in range(len(tail) - 1, -1, -1):
        if tail[i].lower() in _SIZE:
            size = _SIZE[tail[i].lower()]
            del tail[i]
            break
    text = _norm_color(" ".join(tail))
    color = max((c for c in known if re.search(rf"\b{re.escape(c)}\b", text)), key=len, default=None)
    return size, color


def _style(names: list[str], known: set[str] = frozenset()) -> str:
    """A style's name: the words its best sellers' names share, without the brand,
    color or size."""
    words = [_split(BRAND.sub("", n))[2].replace(",", " ,").split() for n in names if n]
    common: list[str] = []
    for col in zip(*words):
        if len({w.lower() for w in col}) > 1:
            break
        common.append(col[0])
    if len(common) < 5 and names:  # names differ early: the best seller's, up to a "(" or ","
        common = re.split(r"[(,]", _split(BRAND.sub("", names[0]))[2])[0].split()
    # Variants that all share one color or size (or a single variant) leave it on the end.
    text = " ".join(common)
    for _ in range(2):
        words = text.split()
        while words and words[-1].lower().strip("(),") in _SIZE:
            words.pop()
        text = " ".join(words).rstrip(" ,–-")
        for c in sorted(known, key=len, reverse=True):
            m = re.search(rf"\s+{re.escape(c)}$", _norm_color(text))
            if m:
                text = text[:len(text) - len(m.group(0))].rstrip(" ,–-")
                break
    return text.strip(" ,–-")


def _n(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def view(wh: Warehouse, plan_amazon_monthly: float = 100_000) -> dict[str, Any] | None:
    """The tracker's Amazon ads tab, from the latest period loaded."""
    latest = wh.query("SELECT period_start, period_end, ad_types FROM ad_products "
                      "ORDER BY period_end DESC, period_start ASC LIMIT 1")
    if not latest:
        return None
    start, end, types = latest[0]["period_start"], latest[0]["period_end"], latest[0]["ad_types"]
    rows = wh.query("SELECT * FROM ad_products WHERE period_start=? AND period_end=?",
                    (start, end))
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    sales = sum(r["total_sales"] for r in rows)
    ad_sales = sum(r["ad_sales"] for r in rows)
    spend = sum(r["ad_spend"] for r in rows)
    acos = _ratio(spend, ad_sales) or 0.0

    by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_parent[r["parent_asin"] or r["asin"]].append(r)
    styles, sizes, colors = [], defaultdict(float), defaultdict(float)
    known = colors_known([r["name"] or "" for r in rows])
    for parent, kids in by_parent.items():
        kids.sort(key=lambda k: -k["total_sales"])
        name = _style([k["name"] for k in kids[:10] if k["total_sales"] > 0] or [kids[0]["name"]], known)
        s = sum(k["total_sales"] for k in kids)
        a = sum(k["ad_sales"] for k in kids)
        sp = sum(k["ad_spend"] for k in kids)
        styles.append({"parent": parent, "name": name or parent, "sales": s, "ad_sales": a,
                       "spend": sp, "share": _ratio(s, sales), "ad_share": _ratio(min(a, s), s),
                       "acos": _ratio(sp, a), "tacos": _ratio(sp, s), "asins": len(kids),
                       "selling": sum(1 for k in kids if k["total_sales"] > 0)})
        for k in kids:
            size, color = variant(k["name"] or "", known)
            if size:
                sizes[size] += k["total_sales"]
            if color:
                colors[color] += k["total_sales"]
    styles.sort(key=lambda s: -s["sales"])
    ranked = sorted(rows, key=lambda r: -r["total_sales"])
    top10 = sum(r["total_sales"] for r in ranked[:10])

    findings: list[dict[str, Any]] = []
    lead = styles[0] if styles else None
    if lead and lead["share"] and lead["share"] >= 0.4:
        findings.append({
            "kind": "risk", "title": f"{lead['share']:.0%} of Amazon sales are one style",
            "see": f"{lead['name']} sold ${lead['sales']:,.0f} of ${sales:,.0f}; the next style "
                   f"sold ${styles[1]['sales']:,.0f}." if len(styles) > 1 else "",
            "do": "Protect it first (stock, Buy Box, reviews, the sizing fix) and grow a second style.",
            "who": "Amazon operations"})
    reliant = [s for s in styles if s["sales"] >= 5000 and (s["ad_share"] or 0) >= 0.5]
    if reliant:
        findings.append({
            "kind": "listing", "title": f"{_n(len(reliant), 'style')} sell mostly through ads",
            "see": "; ".join(f"{s['name']}: {s['ad_share']:.0%} of sales from ads" for s in reliant[:4]),
            "do": "Organic rank is weak, so every sale costs ad money. Improve their listings "
                  "(images, title, reviews) before raising their budgets.",
            "who": "Marketing", "items": [s["parent"] for s in reliant]})
    costly = sorted((r for r in rows if r["ad_spend"] >= MIN_SPEND and r["ad_sales"]
                     and r["ad_spend"] / r["ad_sales"] >= 1.5 * acos), key=lambda r: -r["ad_spend"])
    if costly:
        save = sum(r["ad_spend"] - r["ad_sales"] * acos for r in costly)
        findings.append({
            "kind": "ask_agency",
            "title": f"{_n(len(costly), 'product')} cost much more per ad sale than the account",
            "see": f"ACoS at least {1.5 * acos:.0%} (account {acos:.1%}) on ${sum(r['ad_spend'] for r in costly):,.0f} "
                   f"of spend. Bringing them to the account average saves about ${save:,.0f} "
                   f"over {days} days.",
            "do": "Ask Quartile to lower bids or pause these, or say why they're worth it "
                  "(new launches, defending rank).",
            "who": "Marketing", "gain": save * 30 / days, "items": [r["asin"] for r in costly[:15]]})
    cheap = sorted((r for r in rows if r["ad_sales"] >= MIN_AD_SALES and r["ad_spend"]
                    and r["ad_spend"] / r["ad_sales"] <= 0.7 * acos), key=lambda r: -r["ad_sales"])
    if cheap:
        findings.append({
            "kind": "ask_agency",
            "title": f"{_n(len(cheap), 'product')} earn ad sales cheaply: room to spend more",
            "see": f"ACoS at most {0.7 * acos:.0%} on ${sum(r['ad_sales'] for r in cheap):,.0f} of ad sales.",
            "do": "Ask Quartile whether budget or bids cap them, and to raise them while ACoS stays "
                  "under the account average.",
            "who": "Marketing", "items": [r["asin"] for r in cheap[:15]]})
    wasted = [r for r in rows if r["ad_spend"] > 0 and not r["ad_sales"]]
    if wasted:
        findings.append({
            "kind": "ask_agency",
            "title": f"{_n(len(wasted), 'product')} spent on ads and sold nothing from them",
            "see": f"${sum(r['ad_spend'] for r in wasted):,.0f} in {days} days.",
            "do": "Small, but worth a negative or a pause on the variants that never sell.",
            "who": "Marketing", "items": [r["asin"] for r in sorted(wasted, key=lambda r: -r["ad_spend"])[:15]]})
    dead = [r for r in rows if r["total_sales"] <= 0]
    if dead:
        findings.append({
            "kind": "catalog",
            "title": f"{len(dead):,} of {len(rows):,} variants sold nothing in {days} days",
            "see": "Usually out of stock, suppressed, or a size and color nobody buys.",
            "do": "Check stock and listing health on the ones you mean to sell; retire the rest so "
                  "reviews and rank aren't spread thin.",
            "who": "Amazon operations"})
    monthly = sales * 30 / days
    if plan_amazon_monthly and monthly >= 2 * plan_amazon_monthly:
        findings.append({
            "kind": "plan", "title": f"Amazon sells about ${monthly / 1000:,.0f}k a month, not ${plan_amazon_monthly / 1000:,.0f}k",
            "see": "The CEO plan's gain estimates assumed the smaller figure.",
            "do": "Re-estimate the Amazon-driven plan items (sizing, on-model photos, Buy Box) "
                  "with the real sales.", "who": "CEO"})

    def asin_row(r: dict[str, Any]) -> dict[str, Any]:
        return {"asin": r["asin"], "name": BRAND.sub("", r["name"] or ""), "sales": r["total_sales"],
                "ad_sales": r["ad_sales"], "spend": r["ad_spend"],
                "acos": _ratio(r["ad_spend"], r["ad_sales"]), "tacos": _ratio(r["ad_spend"], r["total_sales"])}

    keep = {a for f in findings for a in f.get("items", [])} | {r["asin"] for r in ranked[:25]}
    return {
        "start": start, "end": end, "days": days, "ad_types": types, "source": "Quartile",
        "totals": {"sales": sales, "ad_sales": ad_sales, "spend": spend,
                   "organic": max(sales - ad_sales, 0.0), "acos": _ratio(spend, ad_sales),
                   "tacos": _ratio(spend, sales), "roas": _ratio(ad_sales, spend),
                   "monthly": monthly, "asins": len(rows),
                   "selling": sum(1 for r in rows if r["total_sales"] > 0),
                   "top10_share": _ratio(top10, sales)},
        "styles": styles,
        "sizes": [{"size": s, "sales": sizes[s]} for s in SIZES if sizes.get(s)],
        "colors": sorted(({"color": c.title(), "sales": v} for c, v in colors.items()),
                         key=lambda c: -c["sales"])[:12],
        "top": [asin_row(r) for r in ranked[:25]],
        "asins": {r["asin"]: asin_row(r) for r in rows if r["asin"] in keep},
        "findings": findings,
    }
