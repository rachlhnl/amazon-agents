"""Review sentiment: what buyers praise and complain about, by topic and by product.

Two sources, read the same way:

- **Store reviews** (Judge.me on the brand's site): every review's stars and text. A
  review counts toward a topic when its text mentions it; its stars decide the side
  (4–5★ happy, 3★ mixed, 1–2★ unhappy). Rules, not a model, so the numbers can be
  checked by reading the reviews.
- **Amazon "Customers say"**: Amazon's own topic summary per listing, with a verdict
  (positive / mixed / negative), how many reviews mention the topic, and how many of
  those agree with the verdict.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

TOPICS: dict[str, tuple[str, ...]] = {
    "Fit & sizing": ("size", "sizes", "sizing", "fit", "fits", "fitted", "runs small", "runs big",
                     "runs large", "too big", "too small", "tight", "loose", "baggy", "snug"),
    "Length": ("length", "too long", "too short", "petite", "tall", "inseam"),
    "Fabric & feel": ("fabric", "material", "soft", "thin", "thick", "stiff", "scratchy",
                      "polyester", "cotton", "texture", "plastic", "shiny"),
    "Stretch & comfort": ("stretch", "stretchy", "comfortable", "comfy", "comfort", "flexible"),
    "Durability": ("rip", "ripped", "tear", "torn", "hole", "holes", "seam", "seams", "fell apart",
                   "shrink", "shrank", "shrunk", "pilling", "pilled", "fade", "faded", "fading"),
    "Color": ("color", "colors", "colour", "shade", "bleed", "bleeds", "dye"),
    "Lint & wrinkles": ("lint", "wrinkle", "wrinkles", "wrinkled", "fuzz", "pet hair"),
    "Pockets": ("pocket", "pockets"),
    "See-through": ("see through", "see-through", "sheer", "transparent"),
    "Price & value": ("price", "value", "worth", "money", "deal", "affordable", "expensive"),
    "Shipping & service": ("shipping", "shipped", "arrived", "delivery", "customer service",
                           "return", "exchange", "refund", "wrong item", "missing"),
}
_RX = {t: re.compile(r"\b(" + "|".join(re.escape(w) for w in ws) + r")\b", re.IGNORECASE)
       for t, ws in TOPICS.items()}
SMALL = re.compile(r"\b(runs? small|too small|too tight|size up|order(ed)? (a )?(size )?up|"
                   r"smaller than)\b", re.IGNORECASE)
BIG = re.compile(r"\b(runs? (big|large)|too (big|large|loose|baggy)|size down|"
                 r"order(ed)? (a )?(size )?down|bigger than|huge)\b", re.IGNORECASE)


def side(score: int) -> str:
    return "pos" if score >= 4 else "neg" if score <= 2 else "mid"


def topics_of(r: dict[str, Any]) -> set[str]:
    text = f"{r.get('title', '')} {r.get('body', '')}"
    return {t for t, rx in _RX.items() if rx.search(text)}


def _quote(text: str, rx: re.Pattern, n: int = 200) -> str:
    """The sentence that mentions the topic, trimmed."""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if rx.search(sent):
            s = sent.strip()
            return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"
    return text[:n]


def topic_table(reviews: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"pos": 0, "mid": 0, "neg": 0})
    for r in reviews:
        for t in topics_of(r):
            out[t][side(r["score"])] += 1
    return dict(out)


def net(c: dict[str, int]) -> float:
    n = sum(c.values())
    return (c["pos"] - c["neg"]) / n if n else 0.0


def store_sentiment(reviews: list[dict[str, Any]], products: list[dict[str, Any]],
                    today: str) -> dict[str, Any]:
    if not reviews:
        return {}
    by_product: dict[int, list[dict]] = defaultdict(list)
    for r in reviews:
        by_product[r["product_id"]].append(r)
    # Some products share one review pool; count each review once in the totals.
    reviews = list({r["id"]: r for r in reviews}.values())
    end = date.fromisoformat(today)
    y1 = (end - timedelta(days=365)).isoformat()
    y2 = (end - timedelta(days=730)).isoformat()
    recent = [r for r in reviews if r["date"] > y1]
    prior = [r for r in reviews if y2 < r["date"] <= y1]

    # Quarterly: volume, share happy, share unhappy (quarters with 5+ reviews).
    q: dict[str, list[int]] = defaultdict(list)
    for r in reviews:
        y, m = int(r["date"][:4]), int(r["date"][5:7])
        q[f"{y}-{(m - 1) // 3 * 3 + 1:02d}-01"].append(r["score"])
    current = f"{end.year}-{(end.month - 1) // 3 * 3 + 1:02d}-01"
    quarters = sorted(k for k, v in q.items() if len(v) >= 5 or k == current)

    titles = {p["id"]: p for p in products}

    pinpoints = []
    for pid, rs in by_product.items():
        p = titles.get(pid)
        unhappy = [r for r in rs if r["score"] <= 2]
        if not p or len(rs) < 10 or len(unhappy) < 4:
            continue
        counts: dict[str, list[dict]] = defaultdict(list)
        for r in unhappy:
            for t in topics_of(r):
                counts[t].append(r)
        if not counts:
            continue
        topic, hits = max(counts.items(), key=lambda kv: len(kv[1]))
        detail = ""
        if topic == "Fit & sizing":
            text = " ".join(r["body"] for r in hits)
            s, b = len(SMALL.findall(text)), len(BIG.findall(text))
            detail = "runs small" if s >= b + 2 else "runs large" if b >= s + 2 else ""
        # The newest review that says something specific (40+ characters).
        quote_r = max(hits, key=lambda r: (len(r["body"]) >= 40, r["date"]))
        pinpoints.append({
            "product": p["title"], "url": p["url"], "topic": topic, "detail": detail,
            "unhappy": len(unhappy), "reviews": len(rs), "hits": len(hits),
            "share": len(hits) / len(unhappy),
            "quote": _quote(quote_r["body"], _RX[topic]), "quote_date": quote_r["date"],
        })
    pinpoints.sort(key=lambda x: -(x["hits"] * x["share"]))
    seen, unique = set(), []
    for x in pinpoints:  # products sharing a review pool would repeat the same finding
        if (x["topic"], x["quote"]) not in seen:
            seen.add((x["topic"], x["quote"]))
            unique.append(x)
    pinpoints = unique

    sizing = [r for r in reviews if r["score"] <= 3]
    text = " ".join(r["body"] for r in sizing)
    return {
        "total": len(reviews), "recent": len(recent), "prior": len(prior),
        "recent_unhappy": sum(1 for r in recent if r["score"] <= 2) / len(recent) if recent else None,
        "prior_unhappy": sum(1 for r in prior if r["score"] <= 2) / len(prior) if prior else None,
        "recent_avg": sum(r["score"] for r in recent) / len(recent) if recent else None,
        "current_quarter": current,
        "quarters": [[k, len(q[k]), sum(1 for s in q[k] if s >= 4) / len(q[k]),
                      sum(1 for s in q[k] if s <= 2) / len(q[k])] for k in quarters],
        "topics": topic_table(reviews),
        "topics_recent": topic_table(recent),
        "topics_prior": topic_table(prior),
        "runs_small": len(SMALL.findall(text)), "runs_large": len(BIG.findall(text)),
        "pinpoints": pinpoints,
    }


def amazon_topics(listings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Amazon topics summed over listing families: mentions split into agreeing-positive,
    agreeing-negative and the rest (mixed)."""
    fam: dict[str, dict] = {}
    for x in listings:
        if x.get("aspects"):
            fam.setdefault(x.get("parent") or x["asin"], x)
    out: dict[str, dict[str, Any]] = {}
    for x in fam.values():
        for a in x["aspects"]:
            t = out.setdefault(a["label"].lower(), {"pos": 0, "mid": 0, "neg": 0, "listings": 0,
                                                    "snippets": [], "worst": None})
            m, agree = a["mentions"], min(a["agree"], a["mentions"])
            if a["verdict"] == "positive":
                t["pos"] += agree
                t["mid"] += m - agree
            elif a["verdict"] == "negative":
                t["neg"] += agree
                t["mid"] += m - agree
            else:
                t["mid"] += m
            t["listings"] += 1
            if a["verdict"] in ("mixed", "negative"):
                if t["worst"] is None or m > t["worst"]["mentions"]:
                    t["worst"] = {"listing": x.get("title", x["asin"]), "asin": x["asin"],
                                  "verdict": a["verdict"], "mentions": m, "summary": a["summary"]}
                t["snippets"] += [s for s in a["snippets"][:2] if s["text"]]
    return out
