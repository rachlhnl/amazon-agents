"""The CEO plan (plan.toml): changes leadership decided on, run as approved work.

Every run, `sync` puts each plan item in the actions table as approved,
human-only work for its team ("acknowledged", so it waits for Mark done), ranked
above the agents' own suggestions, and merges the suggestions an item covers
into it ("superseded"), so each change is one piece of work with one owner.

The item's drafting agent writes a first version of the work: with an Anthropic
key Claude writes it from the evidence (the live walkthrough, the latest mystery
shop, what reviewers said); without one it's a starter outline from the same
evidence. `measure` reads each item's KPI today, so the tracker can show
baseline -> now -> target and whether it's met.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import llm
from .data.warehouse import Warehouse

AGENT = "ceo"
KIND = "ceo_priority"
FIT = "Fit & sizing"


def plan_path() -> Path:
    """PLAN_PATH, else plan.local.toml (git-ignored: the real plan, with real sales),
    else the shareable example plan.toml."""
    if os.getenv("PLAN_PATH"):
        return Path(os.environ["PLAN_PATH"])
    local = Path("plan.local.toml")
    return local if local.exists() else Path("plan.toml")


def load_plan(path: str | Path | None = None) -> list[dict[str, Any]]:
    path = Path(path) if path else plan_path()
    if not path.exists():
        return []
    with path.open("rb") as f:
        return sorted(tomllib.load(f).get("item", []), key=lambda i: i["rank"])


def priority(rank: int) -> str:
    return "now" if rank <= 4 else "high" if rank <= 7 else "medium"


# ------------------------------------------------------------------- measuring

def _market() -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """(latest snapshot of our brand, latest mystery-shop visit of our store, live walk)."""
    from . import market
    from .market import shopper

    try:
        snaps, config = market.history(), market.load_config()
        shop, walk = shopper.latest(), shopper.live()
    except (OSError, ValueError):
        return None, None, None
    key = next((k for k, c in config.items() if c.get("role") == "self"), None)
    ours = snaps[-1]["brands"].get(key) if snaps and key else None
    visit = next((v for v in (shop or {}).get("visits", [])
                  if v["brand"] == (shop or {}).get("self") and v["site"] == "store"), None)
    return ({**ours, "key": key} if ours else None), visit, walk


def fit_share(title_part: str, today: date | None = None) -> tuple[float | None, int]:
    """Share of a store product's 1-2 star reviews (last 12 months) that are about fit."""
    from . import market
    from .market.sentiment import topics_of

    ours, _, _ = _market()
    if not ours or not ours.get("store"):
        return None, 0
    ids = {p["id"] for p in ours["store"]["products"]
           if title_part.lower() in p["title"].lower()}
    since = ((today or date.today()) - timedelta(days=365)).isoformat()
    low = [r for r in market.reviews(ours["key"]) if r.get("product_id") in ids
           and (r.get("score") or 5) <= 2 and (r.get("date") or "") >= since]
    if not low:
        return None, 0
    return sum(1 for r in low if FIT in topics_of(r)) / len(low), len(low)


def _met(value: float, target: str) -> bool | None:
    m = re.match(r"\s*(≤|<=|<|≥|>=|>)\s*\$?([\d,.]+)", target)
    if not m:
        return None
    t = float(m.group(2).replace(",", ""))
    if "%" in target:
        t /= 100
    return {"≤": value <= t, "<=": value <= t, "<": value < t,
            "≥": value >= t, ">=": value >= t, ">": value > t}[m.group(1)]


def _ads_measure(how: str, item: dict[str, Any], wh: Warehouse) -> dict[str, Any] | None:
    """KPIs read from the latest Quartile export (amz_agents.quartile)."""
    from . import quartile

    v = quartile.view(wh)
    if not v:
        return None
    src = f"Quartile export, {v['start']} to {v['end']}"
    if how == "ads:costly":
        f = next((f for f in v["findings"] if "cost much more per ad sale" in f["title"]), None)
        n = len(f["items"]) if f else 0
        return {"now": str(n), "met": _met(n, item["target"]), "source": src}
    if how.startswith("ads:share:"):
        want = set(how.split(":", 2)[2].split(","))
        kids = [s for s in v["styles"] if s["parent"] in want]
        sales = sum(s["sales"] for s in kids)
        if not sales:
            return None
        share = sum(min(s["ad_sales"], s["sales"]) for s in kids) / sales
        return {"now": f"{share:.0%}", "met": _met(share, item["target"]), "source": src}
    return None


CORE_SKUS = 60  # best sellers by units/day, as on the KPI dashboard


def _stock_measure(item: dict[str, Any], wh: Warehouse) -> dict[str, Any] | None:
    core = wh.query("SELECT units_on_hand FROM products WHERE daily_velocity > 0 AND "
                    "units_on_hand IS NOT NULL ORDER BY daily_velocity DESC LIMIT ?", (CORE_SKUS,))
    if not core:
        return None
    n = sum(1 for r in core if r["units_on_hand"] <= 0)
    return {"now": str(n), "met": n <= int(re.sub(r"\D", "", item["target"]) or 0),
            "source": f"FBA stock, top {len(core)} variants by units/day"}


def _units_measure(sku: str, item: dict[str, Any], wh: Warehouse) -> dict[str, Any] | None:
    """Units/day of one SKU over the last 7 days of sales data."""
    last = wh.query("SELECT MAX(date) AS d FROM metrics_daily")[0]["d"]
    if not last:
        return None
    start = (date.fromisoformat(last) - timedelta(days=6)).isoformat()
    units = wh.query("SELECT COALESCE(SUM(units), 0) AS u FROM metrics_daily WHERE sku=? AND "
                     "date BETWEEN ? AND ?", (sku, start, last))[0]["u"]
    v = units / 7
    return {"now": f"{v:.0f}/day", "met": _met(v, item["target"].replace("/day", "")),
            "source": f"Seller Central orders, 7 days to {last}"}


def measure(item: dict[str, Any], today: date | None = None,
            wh: Warehouse | None = None) -> dict[str, Any]:
    """The KPI as it reads today: {"now": text or None, "met": bool or None, "source": ...}."""
    how = item.get("measure", "manual")
    _, visit, _ = _market() if how != "manual" and not how.startswith(("ads:", "stock:", "units7:")) \
        else (None, None, None)
    out: dict[str, Any] = {"now": None, "met": None, "source": "check by hand"}
    if how.startswith("ads:") and wh is not None:
        out = _ads_measure(how, item, wh) or out
    elif how == "stock:core_out" and wh is not None:
        out = _stock_measure(item, wh) or out
    elif how.startswith("units7:") and wh is not None:
        out = _units_measure(how.split(":", 1)[1], item, wh) or out
    elif how.startswith("fit_share:"):
        share, n = fit_share(how.split(":", 1)[1], today)
        if share is not None:
            out = {"now": f"{share:.0%}", "met": _met(share, item["target"]),
                   "source": f"{n} store reviews of 1–2★, last 12 months"}
    elif visit and how == "free_ship_over" and visit.get("free_ship_over"):
        v = visit["free_ship_over"]
        out = {"now": f"${v}", "met": _met(v, item["target"]), "source": "mystery shop"}
    elif visit and how == "page_kb" and visit.get("page_kb"):
        v = visit["page_kb"]
        out = {"now": f"{v:,} KB", "met": _met(v, item["target"]), "source": "mystery shop"}
    elif visit and how.startswith("check:"):
        ok = bool(visit["found"].get(how.split(":", 1)[1]))
        out = {"now": "yes" if ok else "no", "met": ok, "source": "mystery shop"}
    if visit and item.get("checks"):
        out["checks"] = {c: bool(visit["found"].get(c)) for c in item["checks"]}
        out["shopped"] = visit.get("product", {}).get("title")
    return out


# --------------------------------------------------------------------- drafting

def evidence(item: dict[str, Any]) -> dict[str, Any]:
    """What the drafting agent works from."""
    from . import market
    from .market.sentiment import topics_of

    ours, visit, walk = _market()
    ev: dict[str, Any] = {}
    if walk:
        ev["live_walkthrough"] = [s for s in walk.get("sites", []) if s["brand"] == "Dagacci"]
        ev["live_verdict"] = walk.get("verdict")
    if visit:
        ev["mystery_shop"] = {"found": visit.get("found"), "page_kb": visit.get("page_kb"),
                              "return_days": visit.get("return_days"),
                              "free_ship_over": visit.get("free_ship_over")}
    if ours and item.get("measure", "").startswith("fit_share:"):
        part = item["measure"].split(":", 1)[1].lower()
        ids = {p["id"] for p in ours["store"]["products"] if part in p["title"].lower()}
        quotes = [r["body"] for r in sorted(market.reviews(ours["key"]), key=lambda r: r["date"],
                                            reverse=True)
                  if r.get("product_id") in ids and (r.get("score") or 5) <= 2
                  and FIT in topics_of(r) and len(r.get("body") or "") > 30]
        ev["what_reviewers_said"] = list(dict.fromkeys(q.strip() for q in quotes))[:6]
    return ev


def draft(item: dict[str, Any], model: str | None = None) -> dict[str, str]:
    ev = evidence(item)
    if model and llm.available():
        text = llm.complete(
            f"You are the {item.get('drafter', 'chief of staff')} agent at Dagacci, a scrubs "
            "brand. Write the first draft of this piece of work for the team that owns it. "
            "Use only the evidence given; be specific and short; plain Markdown.",
            json.dumps({"work": {k: item.get(k) for k in ("title", "why", "steps", "kpi",
                                                           "baseline", "target", "draft")},
                        "evidence": ev})[:40000], model, max_tokens=1500)
        if text:
            return {"by": item.get("drafter", ""), "voice": "claude", "text": text.strip()}
    lines = [f"# {item['title']}", "",
             (f"_Starter draft by the {item.get('drafter', 'chief of staff')} agent, from the "
             "evidence. Add ANTHROPIC_API_KEY for a written draft._"), "",
             "## The problem", item.get("why", ""), ""]
    if ev.get("what_reviewers_said"):
        lines += ["## What reviewers said", *[f"- “{q[:220]}”"
                                               for q in ev["what_reviewers_said"]], ""]
    # The site the work is about: our own store, unless it's marketplace work.
    where = {"marketing": ("Walmart",), "amazon_ops": ("Amazon",)}.get(item["team"],
                                                                     ("dagacci.com",))
    seen = [x for s in ev.get("live_walkthrough", []) if s["where"] in where
            for x in s.get("dislikes", []) if any(w in x.lower() for w in _words(item))]
    if seen:
        lines += ["## What the mystery shopper saw", *[f"- {x}" for x in seen[:5]], ""]
    lines += ["## Steps", *[f"{i}. {s}" for i, s in enumerate(item.get("steps", []), 1)], "",
              "## Done when", f"{item['kpi']}: {item['baseline']} → {item['target']}", "",
              "## To write", item.get("draft", "")]
    return {"by": item.get("drafter", ""), "voice": "outline", "text": "\n".join(lines)}


def _words(item: dict[str, Any]) -> set[str]:
    stop = {"the", "and", "on", "a", "of", "to", "from", "every", "with", "for", "or", "at", "in"}
    return {w for w in re.findall(r"[a-z]+", item["title"].lower()) if w not in stop and len(w) > 2}


# --------------------------------------------------------------------- syncing

def sync(wh: Warehouse, model: str | None = None, path: str | Path | None = None) -> int:
    """Put the plan in the tracker. Returns how many agent suggestions were merged in."""
    merged = 0
    items = load_plan(path)
    # Items taken out of the plan leave the tracker too (kept as history).
    keep = [f"plan/{i['id']}" for i in items]
    if items:
        wh.execute(
            f"UPDATE actions SET status='superseded', notes='taken out of the CEO plan' "
            f"WHERE agent=? AND status='acknowledged' AND target NOT IN ({','.join('?' * len(keep))})",
            (AGENT, *keep))
    for item in items:
        target = f"plan/{item['id']}"
        gain = (item.get("gain_low", 0) + item.get("gain_high", 0)) / 2 or None
        after = {k: item.get(k) for k in ("title", "rank", "why", "steps", "kpi", "baseline",
                                           "target", "measure", "checks", "gain_low",
                                           "gain_high", "drafter", "draft", "loe", "due_days")}
        after["source"] = "plan"
        row = wh.query("SELECT id, status, after FROM actions WHERE agent=? AND target=?",
                       (AGENT, target))
        if row:
            old = json.loads(row[0]["after"] or "{}")
            after["draft_text"] = old.get("draft_text")
            wh.execute("UPDATE actions SET after=?, reason=?, team=?, priority=?, effort=?, "
                       "impact_usd=? WHERE id=?",
                       (json.dumps(after), item.get("why", ""), item["team"],
                        priority(item["rank"]), item.get("effort", ""), gain, row[0]["id"]))
            action_id = row[0]["id"]
        else:
            action_id = wh.execute(
                "INSERT INTO actions (created, agent, kind, target, before, after, reason, "
                "status, notes, team, priority, effort, impact_usd, confidence) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, 'acknowledged', '', ?, ?, ?, ?, 'low')",
                (item.get("added") or date.today().isoformat(), AGENT, KIND, target,
                 json.dumps(after), item.get("why", ""), item["team"], priority(item["rank"]),
                 item.get("effort", ""), gain))
        # Write the draft once; replace a starter outline once Claude can write it.
        old_draft = after.get("draft_text") or {}
        if not old_draft or (old_draft.get("voice") == "outline" and model and llm.available()):
            after["draft_text"] = draft(item, model)
            wh.execute("UPDATE actions SET after=? WHERE id=?", (json.dumps(after), action_id))
        note = f"merged into CEO plan #{item['rank']}: {item['title']}"
        for title in item.get("covers", []):
            merged += wh.execute_count(
                "UPDATE actions SET status='superseded', notes=? WHERE agent != ? AND "
                "status IN ('proposed', 'pending_approval') AND json_extract(after, '$.title')=?",
                (note, AGENT, title))
            # The plan was re-ranked: keep earlier merges pointing at the item's new rank.
            wh.execute("UPDATE actions SET notes=? WHERE agent != ? AND status='superseded' "
                       "AND json_extract(after, '$.title')=?", (note, AGENT, title))
    return merged
