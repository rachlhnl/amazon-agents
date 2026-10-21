"""Agent work tracker: what needs you, a board of every agent's work, and whether
each agent is healthy.

  amz-agents dashboard                  write reports/dashboard.html (also done by `run`)
  amz-agents dashboard --serve          serve it at http://127.0.0.1:8765, always fresh
  amz-agents dashboard --serve --allow-actions
                                        ...with Approve / Reject / Mark done buttons

The page is self-contained (no external scripts). Data goes in as JSON and is
written into the page with textContent only, so text from reviews, CRMs or ad
names can't inject markup. The server binds to localhost by default, and with
--allow-actions every change needs the per-process token embedded in the page.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tomllib
from collections import Counter
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from . import quartile
from .actions import HUMAN_ONLY, Action, ActionLog
from .agents import ads, listing, pricing, reviews, sales, ux
from .agents import chief_of_staff as cos
from .agents import creative as cr
from .agents import data as da
from .agents import inventory as inv
from .agents import marketing as mk
from .agents.base import EFFORT_HOURS, TEAMS, Context
from .agents.chief_of_staff import ChiefOfStaff, _describe, load_goals
from .config import Guardrails, Settings
from .data.warehouse import Warehouse

WINDOW_DAYS = 30
STALE_WAITING_DAYS = 3

# Where an action is in its life, for progress bars and filters.
STAGE = {
    "proposed": "waiting", "pending_approval": "waiting",
    "approved": "in_progress", "acknowledged": "in_progress",
    "executed": "done", "done": "done",
    "rejected": "rejected", "failed": "failed",
}


# What each agent leaves in ctx.shared for the ones after it, and who reads it.
# Keep in step with the agents' ctx.shared reads and writes.
HANDOFFS = [
    {"from": "inventory", "to": ["pricing", "ads", "marketing", "sales", "creative", "data",
                                 "chief_of_staff"],
     "what": "Items running low on stock",
     "why": "Nobody raises bids, budgets or ad variants for stock that's about to run out."},
    {"from": "inventory", "to": ["pricing", "marketing", "data"],
     "what": "Items with too much stock",
     "why": "Pricing can mark them down and marketing can email them to move the stock."},
    {"from": "market", "to": ["product", "data"],
     "what": "Findings from dagacci.com, its Judge.me reviews and competitors",
     "why": "Product works fit, stock and launches itself; the data agent routes the rest "
            "(marketing, pricing, search, ads) to the agent that owns it."},
    {"from": "market", "to": ["ux"],
     "what": "The weekly mystery shop of every brand's store",
     "why": "What dagacci.com is missing that other brands show becomes UX work."},
    {"from": "reviews", "to": ["listing", "data"],
     "what": "Recurring complaints by product",
     "why": "Listing copy stays honest about known issues; the data agent sizes defects."},
    {"from": "pricing", "to": ["sales"],
     "what": "Proposed Amazon prices",
     "why": "Other channels aren't priced under the new Amazon price and the Buy Box."},
    {"from": "everyone", "to": ["creative"],
     "what": "Drafts: emails, wholesale outreach, listing copy",
     "why": "Each draft is checked against brand.toml and marked ok or needs changes."},
    {"from": "everyone", "to": ["ux"],
     "what": "Budget and listing proposals",
     "why": "UX flags when the landing or detail page, not the ad, is what's losing sales."},
    {"from": "everyone", "to": ["data"],
     "what": "All proposals so far",
     "why": "The data agent links its opportunities to work already proposed and routes "
            "new ones to the agent whose job covers them."},
    {"from": "everyone", "to": ["chief_of_staff"],
     "what": "The full list of proposals",
     "why": "Sized, ranked, assigned to a team, checked against goals, written into the briefs."},
]


def creative_walkthrough() -> list[dict[str, str]]:
    """How a new piece of ad creative goes from a signal to built and live."""
    drop, freq = f"{-cr.FATIGUE_CTR_DROP:.0%}", f"{cr.FATIGUE_FREQUENCY:g}"
    win = f"{cr.WINNER_CTR_RATIO:g}×"
    return [
        {"who": "Ad platforms", "actor": "system", "status": "",
         "title": "Ad results land in the warehouse",
         "text": "Each run pulls every live ad's views, clicks, frequency, spend and "
                 "purchases from Meta (or a CSV export for Google and TikTok)."},
        {"who": "inventory", "actor": "agent", "status": "",
         "title": "Inventory says what's safe to promote",
         "text": "It runs first and shares the items running low, so no one asks for new "
                 "ads to sell stock that's about to run out."},
        {"who": "creative", "actor": "agent", "status": "",
         "title": "Creative agent spots that a new ad is needed",
         "text": f"Tired ad: click rate down {drop}+ while people have seen it {freq}+ times "
                 f"a week → \u201cRefresh fatigued ad\u201d. Winning ad: click rate {win} its "
                 f"campaign with {cr.WINNER_MIN_PURCHASES}+ purchases → \u201cMake variants\u201d. "
                 "Amazon pages short on images, video or A+ → \u201cImprove listing assets\u201d."},
        {"who": "data", "actor": "agent", "status": "",
         "title": "Data agent checks nothing is missed or doubled",
         "text": "It runs last, links its own opportunities to proposals already made, and "
                 "routes any new one to the agent whose job covers it."},
        {"who": "chief_of_staff", "actor": "agent", "status": "proposed",
         "title": "Chief of staff sizes it and hands it to Creative",
         "text": "It adds the 30-day value, effort (a couple of hours) and priority, tags "
                 "the goal it serves, and files it under the Creative team."},
        {"who": "Creative team", "actor": "person", "status": "pending_approval",
         "title": "The Creative team is told",
         "text": "It's in the Creative section of the daily brief, the Creative team brief "
                 "(emailed to BRIEF_TO_CREATIVE) and this tracker under To review."},
        {"who": "You", "actor": "person", "status": "acknowledged",
         "title": "Someone approves it; designers build it",
         "text": "Ad creative is people's work, so the system never makes or uploads an ad. "
                 "Once approved it moves to Being done as \u201cYour team to do\u201d. (In "
                 "suggest-only mode it stays a suggestion for the team to pick up; set "
                 "AGENT_MODE=approve to use the buttons.)"},
        {"who": "Creative team", "actor": "person", "status": "done",
         "title": "Launch it and mark it done",
         "text": "The new ad goes live in the campaign; click Mark done here."},
        {"who": "creative", "actor": "agent", "status": "",
         "title": "Agents judge it on the next runs",
         "text": "The new ad's results flow back in. Creative watches it for fatigue or a "
                 "win, and marketing moves budget toward campaigns that return profit."},
    ]


def _pct(x: float) -> str:
    return f"{abs(x):.0%}"


def decision_rules(g: Guardrails) -> dict[str, Any]:
    """Each agent's rules as `when -> then`, built from the constants the agents use,
    plus the hard limits, the chief of staff's ranking and where the LLM writes text."""
    fu = sales.FOLLOW_UP_DAYS
    agents = {
        "inventory": [
            (f"stock covers less than lead time + {inv.SAFETY_DAYS} days of sales",
             f"Reorder enough for lead time + {inv.SAFETY_DAYS} + {inv.TARGET_DAYS_AFTER_ARRIVAL} days"),
            ("stock runs out before a new order could arrive", "Tell everyone: low stock"),
            (f"more than {inv.OVERSTOCK_DAYS} days of stock", "Tell everyone: overstock"),
        ],
        "reviews": [
            (f"the same complaint in {reviews.MIN_MENTIONS}+ reviews of {reviews.LOW_RATING}\u2605 or less",
             "Flag a possible defect"),
        ],
        "pricing": [
            ("low stock", f"Raise {_pct(pricing.LOW_STOCK_RAISE)} to slow sales"),
            ("Buy Box price is under yours", "Match the Buy Box"),
            ("overstock", f"Cut {_pct(pricing.OVERSTOCK_CUT)} to sell through"),
            ("price is under the margin floor", "Raise to the floor"),
            ("(first match wins, in this order)", ""),
        ],
        "ads": [
            (f"keyword has {ads.MIN_CLICKS_FOR_BID_CHANGE}+ clicks and 0 orders",
             f"Cut the bid {_pct(g.max_bid_change_pct)}"),
            (f"keyword has {ads.MIN_CLICKS_FOR_BID_CHANGE}+ clicks and orders",
             f"Move the bid toward {_pct(g.target_acos)} ACoS (ad spend \u00f7 sales)"),
            (f"search term has {ads.NEGATE_AFTER_CLICKS}+ clicks and 0 orders", "Block it"),
            (f"search term has {ads.HARVEST_MIN_ORDERS}+ orders under target ACoS",
             "Add it as an exact keyword"),
            ("item is low on stock", "Never raise its bids"),
        ],
        "product": [
            ("the market tracker finds a well-reviewed style sold out on dagacci.com",
             "Restock it, best sellers first"),
            ("1\u20132\u2605 reviews of a style are mostly about fit or sizing",
             "Fix the fit or the size chart"),
            ("a style rates poorly, or a review topic is getting worse",
             "Fix what buyers dislike"),
            ("we launch fewer new styles or colors than other brands", "Plan a launch"),
            ("the market tracker says act this week", "Ranked Urgent"),
        ],
        "listing": [
            (f"a search term with {listing.MIN_ORDERS}+ orders is missing from the title",
             "Draft a new title and bullets"),
            ("reviews show a recurring complaint", "Address it honestly in the copy"),
        ],
        "marketing": [
            (f"campaign has under {mk.MIN_CLICKS} clicks", "Wait for more data"),
            (f"profit per $1 spent \u2265 {mk.SCALE_AT:g}", "Raise the budget"),
            (f"profit per $1 spent < {mk.CUT_BELOW:g} (losing money)", "Cut the budget"),
            (f"{mk.PAUSE_AFTER_CLICKS}+ clicks and no orders", "Pause the campaign"),
            ("campaign sells an item low on stock", "Don't scale it"),
            ("item is overstocked", "Draft an email campaign"),
            ("ads that send shoppers to Amazon",
             f"Also credit Amazon's {_pct(mk.BRAND_REFERRAL_BONUS)} Brand Referral Bonus"),
        ],
        "sales": [
            (f"another channel's price is more than {_pct(sales.PARITY_TOLERANCE)} under Amazon",
             "Bring it back in line"),
            (f"an item sells {sales.MIN_VELOCITY_TO_EXPAND:g}+ a day on Amazon",
             "Suggest listing it on another channel"),
            ((f"a lead is quiet for {fu['new']} days (new), {fu['negotiating']} (negotiating) "
             f"or {fu['contacted']} (contacted)"), "Draft a follow-up"),
        ],
        "creative": [
            ((f"click rate down {_pct(cr.FATIGUE_CTR_DROP)}+ and seen {cr.FATIGUE_FREQUENCY:g}+ "
             "times a week"), "Refresh the tired ad"),
            ((f"click rate {cr.WINNER_CTR_RATIO:g}\u00d7 its campaign's with "
             f"{cr.WINNER_MIN_PURCHASES}+ purchases"), "Make variants of the winner"),
            ((f"${cr.PAUSE_MIN_SPEND:.0f}+ spent, no sales this week, click rate under "
             f"{_pct(cr.PAUSE_CTR_RATIO)} of its campaign's"), "Pause the ad"),
            ((f"Amazon page has under {cr.MIN_IMAGES} images or {cr.MIN_BULLETS} bullets, "
             "or no video or A+"), "Complete the page"),
            ("any draft from another agent", "Check it against brand.toml"),
        ],
        "ux": [
            ((f"a checkout step is under {_pct(ux.WEAK_STEP)} of benchmark, or down "
             f"{_pct(ux.STEP_DROP)} vs the prior 2 weeks"), "Fix that step"),
            (f"mobile converts at under {_pct(ux.MOBILE_GAP)} of desktop", "Fix mobile"),
            ((f"mobile speed score under {ux.SLOW_PERF}, or main content takes over "
             f"{ux.SLOW_LCP_MS / 1000:g}s"), "Speed up the page"),
            (f"an ad's landing page converts under {_pct(ux.LANDING_GAP)} of the site rate",
             "Fix the landing page"),
            (f"an Amazon page converts under {_pct(ux.AMAZON_GAP)} of your typical page",
             "Improve that page"),
            (("the weekly mystery shop finds something on dagacci.com missing that other "
             "brands show (size guide, fabric, free returns, fit help...)"), "Add it"),
        ],
        "data": [
            ((f"an item's sales are down {_pct(da.TREND_DROP)}+ or up {_pct(da.TREND_RISE)}+ "
             "this week"), "Raise it with the right agent"),
            (f"Amazon fees are over {_pct(da.FEE_SHARE_HIGH)} of price", "Look for fee savings"),
            (f"over {_pct(da.AD_DEPENDENCE_HIGH)} of sales come from ads", "Build organic rank"),
            (f"rating under {da.LOW_RATING:g} with {da.MIN_REVIEWS}+ reviews", "Look into quality"),
            (f"over {_pct(da.CONCENTRATION_HIGH)} of units sell on Amazon",
             "Grow other channels"),
            (f"data more than {da.STALE_DAYS} days old", "Warn that decisions are on stale data"),
            ("the market tracker finds something in marketing, pricing, search or ads",
             "Route it to that agent"),
        ],
        "chief_of_staff": [
            (f"sales yesterday under {_pct(cos.ANOMALY_DROP)} of the 14-day average",
             "Escalate to you"),
            (f"ad spend yesterday over {cos.SPEND_SPIKE:g}\u00d7 normal", "Escalate to you"),
            ("a suggestion works against a goal in goals.toml", "Escalate to you"),
        ],
    }
    limits = [
        ("Bid change per day", f"\u00b1{_pct(g.max_bid_change_pct)}", "MAX_BID_CHANGE_PCT"),
        ("Bid range", f"${g.min_bid:.2f}\u2013${g.max_bid:.2f}", "MIN_BID / MAX_BID"),
        ("Target ACoS", _pct(g.target_acos), "TARGET_ACOS"),
        ("Campaign budget", f"up to ${g.max_daily_budget:,.0f}/day", "MAX_DAILY_BUDGET"),
        ("Budget change", f"\u00b1{_pct(g.max_budget_change_pct)} at a time", "MAX_BUDGET_CHANGE_PCT"),
        ("Price change", f"\u00b1{_pct(g.max_price_change_pct)} at a time", "MAX_PRICE_CHANGE_PCT"),
        ("Price floor", f"landed cost + fees + {_pct(g.min_margin_pct)} margin", "MIN_MARGIN_PCT"),
    ]
    ranking = [
        ("High", f"worth ${cos.HIGH_IMPACT:,.0f}+ over 30 days and ROI \u2265 {cos.HIGH_ROI:g}\u00d7"),
        ("Medium", f"ROI \u2265 {cos.MEDIUM_ROI:g}\u00d7"),
        ("Low", f"ROI under {cos.MEDIUM_ROI:g}\u00d7, or it loses money"),
        ("Your call", "the value can't be estimated"),
        ("Urgent", "escalations: sales drops, spend spikes, goal conflicts"),
    ]
    return {
        "agents": {k: [{"when": w, "then": t} for w, t in v] for k, v in agents.items()},
        "limits": [{"what": a, "value": b, "env": c} for a, b, c in limits],
        "ranking": [{"level": a, "rule": b} for a, b in ranking],
        "hourly_cost": cos.HOURLY_COST,
        "verdict_pct": cos.VERDICT_THRESHOLD,
        "outcome_days": cos.WINDOW,
    }


def _roster() -> list[dict[str, Any]]:
    from .orchestrator import DEFAULT_AGENTS

    out = []
    for i, a in enumerate(DEFAULT_AGENTS, 1):
        out.append({
            "name": a.name, "step": i, "team": a.role.team, "mission": a.role.mission,
            "proposes": [{"kind": k, "effort": e, "by": "person" if k in HUMAN_ONLY else "api"}
                         for k, e in a.role.proposes.items()],
            "needs": list(a.role.needs),
        })
    out.append({"name": ChiefOfStaff.name, "step": len(out) + 1, "team": "",
                "mission": "Doesn't propose changes. Sizes, ranks and assigns everyone's "
                           "proposals to a team, escalates goal conflicts and sudden swings, "
                           "checks last week's changes worked, and writes the briefs.",
                "proposes": [{"kind": "escalation", "effort": "", "by": "person"}], "needs": []})
    return out


DEFAULT_DUE_DAYS = {"now": 1, "high": 3, "medium": 7, "low": 14, "judgment": 7, "": 7}


def load_teams(path: str | Path | None = None) -> dict[str, Any]:
    """teams.toml: an owner per team and how long work may wait, by priority."""
    path = Path(path or os.getenv("TEAMS_PATH", "teams.toml"))
    cfg: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as f:
            cfg = tomllib.load(f)
    owners = {k: (v.get("owner") or "").strip() for k, v in cfg.get("team", {}).items()}
    return {"owners": owners, "due_days": {**DEFAULT_DUE_DAYS, **cfg.get("due_days", {})}}


def _short_name(title: str | None) -> str:
    """'Insulated Water Bottle 64oz Black - The BEST Leakproof Jug' -> the part before
    the marketing tail, capped so it fits on a card."""
    name = re.split(r"\s+[-|\u2013\u2014,]\s+", title or "")[0].strip()
    return name if len(name) <= 48 else name[:46].rstrip() + "\u2026"


def _named(title: str, names: dict[str, str]) -> str:
    """Swap SKUs in a title for product names."""
    for sku, name in names.items():
        if sku in title:
            title = re.sub(rf"(?<![\w-]){re.escape(sku)}(?![\w-])", name, title)
    return title


def _agents() -> list[tuple[str, Any]]:
    from .orchestrator import DEFAULT_AGENTS

    return [(a.name, a.role) for a in DEFAULT_AGENTS]


def scorecard(wh: Warehouse) -> dict[str, Any] | None:
    """The business this week vs last: revenue, units, contribution profit, TACoS.
    Weeks end on the last day with sales data, not today, so a late feed doesn't
    read as a crash."""
    last = wh.query("SELECT MAX(date) AS d FROM metrics_daily")[0]["d"]
    if not last:
        return None
    end = date.fromisoformat(last)

    def week(ago: int) -> dict[str, float | None]:
        hi, lo = end - timedelta(days=ago), end - timedelta(days=ago + 6)
        rows = wh.query(
            "SELECT COALESCE(SUM(m.units),0) AS units, COALESCE(SUM(m.revenue),0) AS revenue, "
            "COALESCE(SUM(m.ad_spend),0) AS ad_spend, "
            "SUM(m.units * (p.landed_cost + p.fees)) AS cogs, "
            "SUM(CASE WHEN p.landed_cost IS NULL OR p.fees IS NULL THEN m.units END) AS uncosted "
            "FROM metrics_daily m LEFT JOIN products p ON p.sku = m.sku "
            "WHERE m.date BETWEEN ? AND ?", (lo.isoformat(), hi.isoformat()))[0]
        rev, spend = rows["revenue"], rows["ad_spend"]
        profit = rev - rows["cogs"] - spend if rows["cogs"] is not None and not rows["uncosted"] \
            else None
        return {"revenue": rev, "units": rows["units"], "ad_spend": spend, "profit": profit,
                "tacos": spend / rev if rev else None}

    return {"through": last, "now": week(0), "prior": week(7)}


def goals_view(wh: Warehouse, settings: Settings | None = None) -> list[dict[str, Any]]:
    """goals.toml progress, plus whether each goal points at a product we sell."""
    skus = {r["sku"] for r in wh.query("SELECT sku FROM products")}
    ctx = Context(wh, settings or Settings.from_env())
    chief = ChiefOfStaff(ctx, goals=load_goals())
    out = []
    for gp in chief.goal_progress():
        g = gp["goal"]
        out.append({"name": g.name, "metric": g.metric, "scope": g.scope, "target": gp["target"],
                    "actual": gp["actual"], "status": gp["status"],
                    "known": g.scope == "all" or g.scope in skus})
    return out


def setup_gaps(wh: Warehouse, goals: list[dict[str, Any]],
               owners: dict[str, str] | None = None) -> list[str]:
    """Config still on the shipped examples, or data the agents can't decide without."""
    gaps = []
    unowned = [label for t, label in TEAMS.items() if not (owners or {}).get(t)]
    if unowned:
        gaps.append(f"No owner in teams.toml for {', '.join(unowned)}, so nobody is "
                    "accountable for that work.")
    if not wh.query("SELECT 1 FROM products LIMIT 1"):
        gaps.append(("No daily sales or product data yet (the Amazon ads tab has Quartile's "
                     "totals for a period): connect Seller Central (SP-API) and run amz-agents "
                     "ingest, then amz-agents run.")
                    if wh.query("SELECT 1 FROM ad_products LIMIT 1") else
                    "No product or sales data yet: connect Seller Central (SP-API) and "
                    "run amz-agents ingest, then amz-agents run.")
    else:
        uncosted = wh.query("SELECT COUNT(*) AS n FROM products "
                            "WHERE landed_cost IS NULL OR fees IS NULL")[0]["n"]
        if uncosted:
            gaps.append(f"{uncosted} products have no landed cost, so profit, price floors "
                        "and ROI can't be worked out for them: load COSTS_CSV.")
    if not goals:
        gaps.append("No company goals: add them to goals.toml so the chief of staff can "
                    "rank work by what matters.")
    stray = [g["scope"] for g in goals if not g["known"]]
    if stray:
        gaps.append("goals.toml targets products you don't sell (" + ", ".join(stray)
                    + "): it's still the example file.")
    brand = Path(os.getenv("BRAND_PATH", "brand.toml"))
    if brand.exists() and 'name = "Your Brand"' in brand.read_text():
        gaps.append("brand.toml is still the template, so drafts are checked against a "
                    "generic voice, not Dagacci's.")
    site = Path(os.getenv("SITE_PATH", "site.toml"))
    if site.exists() and "yourstore.example" in site.read_text():
        gaps.append("site.toml lists placeholder pages, so the UX agent isn't checking "
                    "dagacci.com.")
    return gaps


def market_summary(root: Path | None = None) -> dict[str, Any] | None:
    """Headline, tiles and recommendations from the public market tracker, if it has run."""
    from . import market
    from .market import analyze

    root = root or market.ROOT
    try:
        snaps = market.history(root)
        if not snaps:
            return None
        config = market.load_config(root)
        reviews = {k: market.reviews(k, root) for k, c in config.items()
                   if c.get("judgeme_shop")}
        a = analyze.build(snaps, config, reviews)
    except (OSError, ValueError, KeyError) as e:
        return {"error": repr(e)}
    keep = ("when", "when_label", "area", "effort", "title", "see", "why", "do", "kpi",
            "now", "target", "url")
    return {"date": a["date"], "title": a["title"], "hero": a["hero"], "tiles": a["tiles"],
            "recs": [{k: r.get(k) for k in keep} for r in a["recs"]]}


def plan_view(wh: Warehouse, actions: list[dict[str, Any]], owners: dict[str, str],
              today: date) -> list[dict[str, Any]]:
    """plan.toml items with their tracker work and KPI scorecard."""
    from . import plan

    by_target = {a["target"]: a for a in actions if a["agent"] == plan.AGENT}
    merged = {r["t"]: r["n"] for r in wh.query(
        "SELECT COUNT(*) AS n, "  # notes: "merged into CEO plan #<rank>: <title>"
        "substr(notes, instr(notes, '#') + 1, instr(notes, ':') - instr(notes, '#') - 1) AS t "
        "FROM actions WHERE status='superseded' GROUP BY t")}
    out = []
    for item in plan.load_plan():
        a = by_target.get(f"plan/{item['id']}")
        out.append({
            "id": item["id"], "rank": item["rank"], "title": item["title"], "team": item["team"],
            "owner": owners.get(item["team"], ""), "effort": item.get("effort", ""),
            "loe": item.get("loe", ""),
            "gain_low": item.get("gain_low", 0), "gain_high": item.get("gain_high", 0),
            "kpi": item["kpi"], "baseline": item["baseline"], "target": item["target"],
            "score": plan.measure(item, today), "action_id": a["id"] if a else None,
            "status": a["status"] if a else None, "due": a["due"] if a else None,
            "overdue": a["overdue"] if a else False, "merged": merged.get(str(item["rank"]), 0),
        })
    return out


def shopper_view() -> dict[str, Any] | None:
    """The latest mystery shop, plus the checklist it ran."""
    from .market import shopper

    try:
        shop, walk = shopper.latest(), shopper.live()
    except (OSError, ValueError):
        return None
    if not shop and not walk:
        return None
    if not shop:
        return {"live": walk, "visits": []}
    return {**shop, "live": walk, "checks": [{"key": c.key, "label": c.yes, "category": c.category,
                                "weight": c.weight} for c in shopper.CHECKS],
            "categories": shopper.CATEGORIES}


def collect(wh: Warehouse, today: date | None = None,
            guardrails: Guardrails | None = None, include_market: bool = True) -> dict[str, Any]:
    """Everything the page shows, as plain JSON-able data."""
    today = today or date.today()
    goals = goals_view(wh)
    run = (wh.query("SELECT * FROM runs ORDER BY id DESC LIMIT 1") or [None])[0]
    health = {r["agent"]: r for r in wh.query(
        "SELECT * FROM agent_runs WHERE run_id=?", (run["id"],))} if run else {}
    last_data = wh.query("SELECT MAX(date) AS d FROM metrics_daily")[0]["d"]

    rows = wh.query(
        "SELECT * FROM actions WHERE status != 'superseded' AND (created >= date(?, ?) "
        "OR status IN ('pending_approval', 'approved', 'acknowledged')) ORDER BY id DESC",
        (today.isoformat(), f"-{WINDOW_DAYS} days"),
    )
    verdicts = {o["action_id"]: o for o in wh.query("SELECT * FROM outcomes")}
    names = {p["sku"]: _short_name(p["title"]) for p in wh.query("SELECT sku, title FROM products")
             if p["title"]}
    teams_cfg = load_teams()
    actions = []
    for r in rows:
        before, after = json.loads(r["before"] or "null"), json.loads(r["after"] or "null")
        a = Action(agent=r["agent"], kind=r["kind"], target=r["target"], before=before,
                   after=after, reason=r["reason"] or "", sku=r["sku"])
        try:
            title = (f"Decision needed: {r['target']}" if r["kind"] == "escalation"
                     else _describe(a))
        except (KeyError, TypeError, AttributeError):
            title = f"{r['kind']} {r['target']}"
        created = (r["created"] or today.isoformat())[:10]
        o = verdicts.get(r["id"])
        title = _named(title, names)
        stage = STAGE.get(r["status"], "waiting")
        due = None
        if stage in ("waiting", "in_progress"):
            days = teams_cfg["due_days"].get(r["priority"] or "", 7)
            if isinstance(after, dict) and after.get("source") == "plan" and after.get("due_days"):
                days = after["due_days"]  # the plan sets its own deadlines
            due = (date.fromisoformat(created) + timedelta(days=days)).isoformat()
        actions.append({
            "id": r["id"], "agent": r["agent"], "target": r["target"], "team": r["team"] or "", "kind": r["kind"],
            "title": title, "reason": r["reason"] or "", "notes": r["notes"] or "",
            "status": r["status"], "stage": stage,
            "priority": r["priority"] or "", "impact": r["impact_usd"], "effort": r["effort"] or "",
            "roi": r["roi"], "sku": r["sku"] or "", "created": created,
            "age": (today - date.fromisoformat(created)).days,
            "verdict": o["verdict"] if o else None,
            "product": names.get(r["sku"] or "", ""),
            "owner": teams_cfg["owners"].get(r["team"] or "", ""),
            "due": due, "overdue": bool(due and due < today.isoformat()),
            "source": after.get("source", "") if isinstance(after, dict) else "",
            "steps": after.get("steps", []) if isinstance(after, dict) else [],
            "url": after.get("url") if isinstance(after, dict) else None,
            "draft": after.get("draft_text") if isinstance(after, dict) else None,
            "loe": after.get("loe") if isinstance(after, dict) else None,
        })

    agents = []
    for name, role in _agents():
        mine = [a for a in actions if a["agent"] == name]
        stages = Counter(a["stage"] for a in mine)
        waiting = [a["age"] for a in mine if a["stage"] == "waiting"]
        h = health.get(name)
        problems = json.loads(h["problems"]) if h else ["hasn't run yet"]
        agents.append({
            "name": name, "team": role.team, "team_label": TEAMS.get(role.team, role.team),
            "mission": role.mission, "proposed_last_run": h["proposed"] if h else 0,
            "problems": problems, "stages": dict(stages),
            "helped": sum(1 for a in mine if a["verdict"] == "helped"),
            "hurt": sum(1 for a in mine if a["verdict"] == "hurt"),
            "oldest_waiting": max(waiting) if waiting else None,
        })
    chief_items = [a for a in actions if a["agent"] == "chief_of_staff"]

    open_impact = sum(a["impact"] or 0 for a in actions
                      if a["stage"] == "waiting" and (a["impact"] or 0) > 0)
    stage_counts = Counter(a["stage"] for a in actions)
    return {
        "generated": datetime.now().isoformat(timespec="minutes"),
        "today": today.isoformat(),
        "run": {"finished": run["finished"], "mode": run["mode"], "submitted": run["submitted"],
                "skipped": run["skipped"]} if run else None,
        "data_through": last_data,
        "kpis": {
            "waiting": stage_counts["waiting"], "waiting_impact": round(open_impact),
            "in_progress": stage_counts["in_progress"], "done": stage_counts["done"],
            "failed": stage_counts["failed"],
            "healthy": sum(1 for a in agents if not a["problems"]), "agents": len(agents),
            "escalations": sum(1 for a in chief_items if a["stage"] == "waiting"),
        },
        "agents": agents,
        "teams": TEAMS,
        "actions": actions,
        "effort_hours": EFFORT_HOURS,
        "window_days": WINDOW_DAYS,
        "stale_days": STALE_WAITING_DAYS,
        "roster": _roster(),
        "handoffs": HANDOFFS,
        "walkthrough": creative_walkthrough(),
        "rules": decision_rules(guardrails or Guardrails.from_env()),
        "sample": getattr(wh, "path", "").endswith("demo.db"),
        "scorecard": scorecard(wh),
        "goals": goals,
        "setup_gaps": setup_gaps(wh, goals, teams_cfg["owners"]),
        "market": market_summary() if include_market else None,
        "shopper": shopper_view() if include_market else None,
        "plan": plan_view(wh, actions, teams_cfg["owners"], today) if include_market else [],
        "ads": quartile.view(wh),
    }


def render(data: dict[str, Any], token: str | None = None, market_src: str = "/market") -> str:
    """The page. `token` turns on the action buttons (server mode only); `market_src`
    is where the Market tab loads the market tracker from."""
    payload = json.dumps({**data, "token": token, "market_src": market_src}).replace("</", "<\\/")
    return _PAGE.replace("__DATA__", payload)


def write(wh: Warehouse, out: str | Path = "reports/dashboard.html") -> Path:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    from .market import ROOT

    src = os.path.relpath(ROOT / "index.html", path.parent.resolve()) if ROOT.is_absolute() \
        else os.path.relpath(Path.cwd() / ROOT / "index.html", path.parent.resolve())
    path.write_text(render(collect(wh), market_src=src))
    return path


def serve(wh: Warehouse, log: ActionLog, host: str = "127.0.0.1", port: int = 8765,
          allow_actions: bool = False) -> None:
    token = secrets.token_urlsafe(24) if allow_actions else None
    ops = {"approve": log.approve, "reject": log.reject, "done": log.complete}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            route = self.path.split("?")[0]
            if route == "/market":
                from .market import ROOT

                page = ROOT / "index.html"
                if not page.exists():
                    return self._send(404, b"no market tracker yet: run amz-agents market",
                                      "text/plain")
                return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            if route not in ("/", "/index.html"):
                return self._send(404, b"not found", "text/plain")
            self._send(200, render(collect(wh), token).encode(), "text/html; charset=utf-8")

        def do_POST(self) -> None:
            if not token or self.headers.get("X-Tracker-Token") != token:
                return self._send(403, b'{"error":"actions are off"}', "application/json")
            try:
                n = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(n) or b"{}")
                ops[req["op"]](int(req["id"]))
                status = wh.query("SELECT status FROM actions WHERE id=?", (int(req["id"]),))
                body = {"id": req["id"], "status": status[0]["status"]}
                self._send(200, json.dumps(body).encode(), "application/json")
            except (KeyError, ValueError) as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")

        def log_message(self, *args: Any) -> None:
            pass

    print(f"Agent tracker at http://{host}:{port}"
          + (" (approve/reject/done enabled)" if token else " (read-only)"))
    HTTPServer((host, port), Handler).serve_forever()


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent work tracker</title>
<style>
:root {
  color-scheme: light;
  --bg: #f6f6f4; --surface: #fcfcfb; --surface-2: #f0efec; --border: #e2e1dc;
  --text: #0b0b0b; --text-2: #52514e; --text-3: #75746f;
  --accent: #2a78d6; --accent-soft: #e6f0fc;
  --st-review: #86b6ef; --st-doing: #2a78d6; --st-done: #0ca30c; --st-stopped: #b9b8b2;
  --good: #0ca30c; --good-text: #006300; --good-soft: #e3f4e3;
  --warn: #fab219; --warn-text: #8a5a00; --warn-soft: #fdf1d6;
  --crit: #d03b3b; --crit-text: #b02a2a; --crit-soft: #fbe5e5;
  --shadow: 0 1px 2px rgba(0,0,0,.05);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #121211; --surface: #1a1a19; --surface-2: #232321; --border: #383835;
    --text: #f0efec; --text-2: #c3c2b7; --text-3: #9a998f;
    --accent: #3987e5; --accent-soft: #1d2a3a;
    --st-review: #6da7ec; --st-doing: #3987e5; --st-done: #0ca30c; --st-stopped: #5e5d58;
    --good-text: #4cc24c; --good-soft: #17301a; --warn-text: #fab219; --warn-soft: #33290f;
    --crit-text: #e66767; --crit-soft: #3a1c1c; --shadow: none;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #121211; --surface: #1a1a19; --surface-2: #232321; --border: #383835;
  --text: #f0efec; --text-2: #c3c2b7; --text-3: #9a998f;
  --accent: #3987e5; --accent-soft: #1d2a3a;
  --st-review: #6da7ec; --st-doing: #3987e5; --st-done: #0ca30c; --st-stopped: #5e5d58;
  --good-text: #4cc24c; --good-soft: #17301a; --warn-text: #fab219; --warn-soft: #33290f;
  --crit-text: #e66767; --crit-soft: #3a1c1c; --shadow: none;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
button { font: inherit; color: inherit; }
.wrap { max-width: 1280px; margin: 0 auto; padding: 24px 16px 48px; }
header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-start; gap: 12px; }
h1 { font-size: 22px; margin: 0; font-weight: 650; }
h2 { font-size: 16px; margin: 0; font-weight: 650; }
.meta { color: var(--text-2); font-size: 13px; display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: 4px; }
.theme { border: 1px solid var(--border); background: var(--surface); border-radius: 8px; padding: 6px 10px;
  font-size: 13px; cursor: pointer; color: var(--text-2); }
.headline { font-size: 18px; margin: 20px 0 16px; max-width: 900px; }
.headline b { font-weight: 650; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 2px 8px; border-radius: 999px;
  background: var(--surface-2); color: var(--text-2); font-size: 12px; white-space: nowrap; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }
.panel { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px;
  box-shadow: var(--shadow); }
.sec-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: 6px 12px;
  margin: 28px 0 12px; }
.sec-head p { margin: 0; color: var(--text-3); font-size: 13px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px;
  text-align: left; cursor: pointer; box-shadow: var(--shadow); }
.tile:hover, .tile:focus-visible { border-color: var(--accent); outline: none; }
.tile .label { color: var(--text-2); font-size: 13px; display: flex; align-items: center; gap: 6px; }
.tile .value { font-size: 28px; font-weight: 650; margin-top: 2px; font-variant-numeric: tabular-nums; }
.tile .sub { color: var(--text-3); font-size: 12px; }
.tile.alert .value { color: var(--crit-text); }
/* Inbox */
.inbox { display: grid; gap: 8px; }
.todo { display: grid; grid-template-columns: auto 1fr auto; gap: 4px 12px; align-items: center;
  background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--st-review);
  border-radius: 10px; padding: 12px 14px; box-shadow: var(--shadow); }
.todo.p-now { border-left-color: var(--crit); }
.todo.p-high { border-left-color: var(--accent); }
.todo.health { border-left-color: var(--warn); }
.todo .t { font-weight: 600; cursor: pointer; background: none; border: 0; padding: 0; text-align: left; }
.todo .t:hover { color: var(--accent); }
.todo .s { color: var(--text-2); font-size: 12.5px; grid-column: 2; }
.todo .acts { grid-row: 1 / span 2; grid-column: 3; display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.avatar { width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; font-size: 12px;
  font-weight: 700; background: var(--accent-soft); color: var(--accent); flex: none; grid-row: 1 / span 2; }
.todo.health .avatar { background: var(--warn-soft); color: var(--warn-text); }
.more { color: var(--text-3); font-size: 13px; padding: 4px 2px; }
.caught-up { color: var(--good-text); background: var(--good-soft); border-radius: 10px; padding: 12px 14px; font-weight: 550; }
/* Filters */
.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px; }
.pill { border: 1px solid var(--border); background: var(--surface); border-radius: 999px; padding: 4px 12px;
  font-size: 13px; color: var(--text-2); cursor: pointer; }
.pill[aria-pressed=true] { background: var(--accent-soft); border-color: var(--accent); color: var(--text); font-weight: 600; }
select, input[type=search] { font: inherit; font-size: 13px; color: var(--text); background: var(--surface);
  border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; }
input[type=search] { min-width: 180px; flex: 1; }
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.seg button { font-size: 13px; padding: 6px 12px; border: 0; background: var(--surface); color: var(--text-2); cursor: pointer; }
.seg button + button { border-left: 1px solid var(--border); }
.seg button[aria-pressed=true] { background: var(--accent-soft); color: var(--text); font-weight: 600; }
.clear { font-size: 13px; background: none; border: 0; color: var(--accent); cursor: pointer; }
/* Board */
.board { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; align-items: start; }
.col { background: var(--surface-2); border-radius: 12px; padding: 10px; min-width: 0; }
.col-head { display: flex; align-items: center; gap: 8px; padding: 2px 4px 10px; font-weight: 650; }
.col-head .n { margin-left: auto; color: var(--text-3); font-weight: 500; font-variant-numeric: tabular-nums; }
.col-head .hint { display: block; font-weight: 400; color: var(--text-3); font-size: 12px; }
.col-body { display: grid; gap: 8px; }
.item { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px;
  cursor: pointer; text-align: left; width: 100%; box-shadow: var(--shadow); }
.item:hover, .item:focus-visible { border-color: var(--accent); outline: none; }
.dtags { display: flex; flex-wrap: wrap; gap: 4px; }
.item .tags { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 6px; }
.item .t { font-weight: 550; font-size: 13.5px; overflow-wrap: anywhere; }
.item .who { display: flex; justify-content: space-between; gap: 8px; color: var(--text-3); font-size: 12px; margin-top: 8px; }
.item .who .imp { color: var(--text); font-variant-numeric: tabular-nums; font-weight: 550; }
.tag { font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--border); color: var(--text-2); white-space: nowrap; }
.tag.now { border-color: var(--crit); color: var(--crit-text); font-weight: 600; }
.tag.high { border-color: var(--accent); color: var(--accent); font-weight: 600; }
.tag.ok { background: var(--accent); border-color: var(--accent); color: #fff; }
.tag.you { background: var(--warn-soft); border-color: var(--warn); color: var(--warn-text); }
.tag.bad { background: var(--crit-soft); border-color: var(--crit); color: var(--crit-text); }
.tag.good { background: var(--good-soft); border-color: var(--good); color: var(--good-text); }
.stale { color: var(--warn-text); font-weight: 600; }
.overdue { color: var(--crit-text); font-weight: 600; }
.unowned { color: var(--warn-text); }
.col-more { font-size: 12.5px; color: var(--accent); background: none; border: 0; cursor: pointer; padding: 4px; }
/* List */
.table-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; font-weight: 600; color: var(--text-2); padding: 10px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }
td { padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
tbody tr { cursor: pointer; } tbody tr:hover { background: var(--surface-2); }
tr:last-child td { border-bottom: 0; }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
/* Team */
.team { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
.agent { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; align-items: center; text-align: left;
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; cursor: pointer;
  box-shadow: var(--shadow); }
.agent:hover, .agent:focus-visible { border-color: var(--accent); outline: none; }
.agent.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.agent .nm { font-weight: 650; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.agent .sub { color: var(--text-3); font-size: 12px; }
.agent .line { grid-column: 1 / -1; color: var(--text-2); font-size: 12.5px; margin-top: 8px; }
.agent .line.bad { color: var(--crit-text); }
.bar { grid-column: 1 / -1; display: flex; height: 8px; border-radius: 4px; overflow: hidden; background: var(--surface-2); gap: 2px; margin-top: 8px; }
.bar div { height: 100%; min-width: 3px; }
.counts { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 2px 12px; font-size: 12px; color: var(--text-2); margin-top: 6px; }
.counts b { color: var(--text); font-variant-numeric: tabular-nums; }
.health-ok { color: var(--good-text); } .health-bad { color: var(--crit-text); }
/* Buttons */
.btn { font-size: 12.5px; padding: 5px 12px; border-radius: 7px; cursor: pointer; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); white-space: nowrap; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; }
.btn:disabled { opacity: .5; cursor: default; }
/* Drawer */
.scrim { position: fixed; inset: 0; background: rgba(0,0,0,.25); display: none; z-index: 20; }
.drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(460px, 100%); background: var(--surface);
  border-left: 1px solid var(--border); z-index: 21; transform: translateX(100%); transition: transform .18s ease;
  overflow-y: auto; padding: 20px; }
.open .scrim { display: block; } .open .drawer { transform: none; }
.drawer .x { float: right; background: none; border: 0; font-size: 22px; line-height: 1; cursor: pointer; color: var(--text-2); }
.drawer h3 { font-size: 17px; margin: 8px 30px 12px 0; overflow-wrap: anywhere; }
.steps { display: flex; gap: 4px; margin: 4px 0 16px; }
.steps div { flex: 1; font-size: 11px; color: var(--text-3); border-top: 3px solid var(--surface-2); padding-top: 4px; }
.steps div.on { border-top-color: var(--accent); color: var(--text); font-weight: 600; }
.steps div.stop { border-top-color: var(--crit); color: var(--crit-text); font-weight: 600; }
dl { display: grid; grid-template-columns: 110px 1fr; gap: 8px 12px; margin: 0 0 16px; font-size: 13px; }
dt { color: var(--text-3); } dd { margin: 0; overflow-wrap: anywhere; }
.why-box { background: var(--surface-2); border-radius: 10px; padding: 12px; font-size: 13px; margin-bottom: 16px; white-space: pre-wrap; overflow-wrap: anywhere; }
.drawer .acts { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.note { color: var(--text-3); font-size: 12px; margin-top: 10px; }
.msg { font-size: 12.5px; color: var(--crit-text); }
.empty { padding: 20px; text-align: center; color: var(--text-3); font-size: 13px; }
#tip { position: fixed; pointer-events: none; background: var(--text); color: var(--bg); font-size: 12px;
  padding: 6px 8px; border-radius: 6px; display: none; z-index: 30; white-space: nowrap; }
/* CEO view */
.warn-box { background: var(--warn-soft); border: 1px solid var(--warn); border-radius: 12px; padding: 12px 14px; margin-top: 16px; font-size: 13.5px; }
.warn-box b { color: var(--warn-text); }
.warn-box ul { margin: 6px 0 0; padding-left: 18px; display: grid; gap: 3px; }
.score { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 12px; margin-top: 16px; }
.score .kpis { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
.score .tile { cursor: default; }
.score .tile:hover { border-color: var(--border); }
.delta { font-size: 12px; font-weight: 600; } .delta.up { color: var(--good-text); } .delta.down { color: var(--crit-text); }
.optim { color: var(--text-2); font-size: 12.5px; margin: 8px 0 0; }
.goal { display: grid; grid-template-columns: 1fr auto; gap: 2px 10px; padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
.goal:last-child { border-bottom: 0; }
.goal .gs { color: var(--text-3); font-size: 12px; }
.goal .tag { align-self: center; grid-row: 1 / span 2; grid-column: 2; }
.todo.mkt { border-left-color: var(--st-done); }
.todo.mkt .avatar { background: var(--good-soft); color: var(--good-text); }
.group-label { font-size: 12px; color: var(--text-3); text-transform: uppercase; letter-spacing: .04em; margin: 10px 0 2px; }
.mframe { width: 100%; height: calc(100vh - 140px); min-height: 600px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); margin-top: 16px; }
@media (max-width: 900px) { .score { grid-template-columns: 1fr; } }
/* CEO plan */
.plan td { vertical-align: middle; }
.plan .rk { font-weight: 700; color: var(--text-3); width: 28px; }
.plan .ttl { font-weight: 600; cursor: pointer; background: none; border: 0; padding: 0; text-align: left; }
.plan .ttl:hover { color: var(--accent); }
.plan .kpi { font-size: 12.5px; color: var(--text-2); }
.plan .kpi b { color: var(--text); font-variant-numeric: tabular-nums; }
.plan tr.done td { opacity: .55; }
.draft { background: var(--surface-2); border-radius: 10px; padding: 12px; font: 12.5px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 360px; overflow: auto; margin-bottom: 16px; }
/* Mystery shopper */
.verdict { background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--accent); border-radius: 12px; padding: 16px; margin-top: 16px; }
.verdict .k { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-3); }
.verdict .v { font-size: 22px; font-weight: 650; margin: 2px 0 6px; }
.verdict p { margin: 0; color: var(--text-2); max-width: 900px; }
.sb td { vertical-align: middle; }
.sb tr.me td { background: var(--accent-soft); }
.mini { display: flex; align-items: center; gap: 6px; min-width: 92px; }
.mini .t { flex: 1; height: 6px; background: var(--surface-2); border-radius: 3px; overflow: hidden; }
.mini .t i { display: block; height: 100%; background: var(--accent); border-radius: 3px; }
.mini span { font-size: 12px; font-variant-numeric: tabular-nums; width: 26px; text-align: right; color: var(--text-2); }
.sites { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; }
.site { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px; }
.site.me { border-color: var(--accent); }
.site .hd { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
.site .nm { font-weight: 650; } .site .sc { font-weight: 650; font-variant-numeric: tabular-nums; }
.site .pr { font-size: 12.5px; color: var(--text-3); margin: 2px 0 8px; overflow-wrap: anywhere; }
.site p { margin: 0 0 8px; font-size: 13px; color: var(--text-2); }
.site ul { margin: 0 0 8px; padding: 0; list-style: none; display: grid; gap: 3px; font-size: 12.5px; }
.site li { padding-left: 18px; position: relative; }
.site li::before { position: absolute; left: 0; font-weight: 700; }
.site .like li::before { content: "\2713"; color: var(--good-text); }
.site .dislike li::before { content: "\2715"; color: var(--crit-text); }
.site .chg li::before { content: "\2192"; color: var(--accent); }
.site h5 { margin: 6px 0 3px; font-size: 11.5px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-3); }
.cl td, .cl th { text-align: center; } .cl td:first-child, .cl th:first-child { text-align: left; }
.cl .y { color: var(--good-text); font-weight: 700; } .cl .n { color: var(--text-3); }
.cl th.me, .cl td.me { background: var(--accent-soft); }
/* Amazon ads */
.ads .kpis { margin-top: 16px; }
.finds { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; }
.find { background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--accent); border-radius: 12px; padding: 14px; }
.find.risk { border-left-color: var(--crit); } .find.plan { border-left-color: var(--warn); }
.find .hd { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
.find .ttl { font-weight: 650; } .find p { margin: 6px 0 0; font-size: 13px; color: var(--text-2); }
.find .do { color: var(--text); }
.find details { margin-top: 8px; font-size: 12.5px; } .find summary { cursor: pointer; color: var(--accent); }
.find details table { font-size: 12px; margin-top: 6px; } .find details td, .find details th { padding: 4px 6px; }
.bar { display: flex; align-items: center; gap: 8px; min-width: 120px; }
.bar .t { flex: 1; height: 8px; background: var(--surface-2); border-radius: 4px; overflow: hidden; }
.bar .t i { display: block; height: 100%; background: var(--accent); border-radius: 0 4px 4px 0; }
.bar span { font-size: 12px; font-variant-numeric: tabular-nums; width: 40px; text-align: right; color: var(--text-2); }
.two { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; }
.hbars { display: grid; gap: 6px; margin-top: 10px; }
.hbars .row { display: grid; grid-template-columns: 110px 1fr 70px; gap: 8px; align-items: center; font-size: 12.5px; }
.hbars .row .lab { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hbars .row .v { text-align: right; font-variant-numeric: tabular-nums; color: var(--text-2); }
.hbars .t { height: 14px; background: var(--surface-2); border-radius: 0 4px 4px 0; }
.hbars .t i { display: block; height: 100%; background: var(--accent); border-radius: 0 4px 4px 0; min-width: 2px; }
.sty { max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 900px) { .two { grid-template-columns: 1fr; } }
/* Tabs */
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin: 18px 0 0; }
.tabs button { background: none; border: 0; border-bottom: 2px solid transparent; padding: 8px 12px; margin-bottom: -1px;
  font-size: 14px; color: var(--text-2); cursor: pointer; }
.tabs button[aria-selected=true] { color: var(--text); border-bottom-color: var(--accent); font-weight: 650; }
.lead { color: var(--text-2); max-width: 820px; margin: 16px 0 0; }
/* Run order */
.flow { display: flex; flex-wrap: wrap; align-items: stretch; gap: 8px; }
.stage { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 10px 12px; min-width: 0; flex: 1 1 200px; }
.stage h4 { margin: 0 0 6px; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-3); }
.stage .chips { display: flex; flex-wrap: wrap; gap: 4px; }
.arrow { align-self: center; color: var(--text-3); font-size: 18px; }
.ag { display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px 3px 3px; border-radius: 999px;
  background: var(--surface-2); font-size: 12.5px; white-space: nowrap; }
.ag .avatar { width: 20px; height: 20px; font-size: 9px; grid-row: auto; }
.ag .n { color: var(--text-3); font-variant-numeric: tabular-nums; }
/* Walkthrough */
.callout { background: var(--accent-soft); border-radius: 12px; padding: 12px 14px; font-size: 13.5px; margin-bottom: 14px; }
.callout b { font-weight: 650; }
.timeline { list-style: none; margin: 0; padding: 0; position: relative; }
.timeline li { display: grid; grid-template-columns: 34px 1fr; gap: 12px; padding-bottom: 14px; position: relative; }
.timeline li::before { content: ""; position: absolute; left: 16px; top: 30px; bottom: 0; width: 2px; background: var(--border); }
.timeline li:last-child::before { display: none; }
.timeline .num { width: 34px; height: 34px; border-radius: 50%; display: grid; place-items: center; font-weight: 700; font-size: 13px;
  background: var(--accent-soft); color: var(--accent); }
.timeline .num.person { background: var(--warn-soft); color: var(--warn-text); }
.timeline .num.system { background: var(--surface-2); color: var(--text-2); }
.timeline .who { font-size: 12px; color: var(--text-3); display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.timeline .ttl { font-weight: 650; margin: 1px 0 3px; }
.timeline .txt { color: var(--text-2); font-size: 13px; max-width: 760px; }
.key { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12px; color: var(--text-2); margin: 0 0 12px; }
.key span { display: inline-flex; align-items: center; gap: 6px; }
/* Handoffs */
.hand { display: grid; grid-template-columns: minmax(150px, 220px) 1fr; gap: 6px 16px; padding: 12px 0; border-bottom: 1px solid var(--border); }
.hand:last-child { border-bottom: 0; }
.hand .route { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.hand .what { font-weight: 600; } .hand .why { color: var(--text-2); font-size: 13px; }
/* Agent cards */
.roster { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.role { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px; box-shadow: var(--shadow); }
.role .hd { display: flex; gap: 10px; align-items: center; }
.role .hd .avatar { grid-row: auto; }
.role .nm { font-weight: 650; } .role .tm { color: var(--text-3); font-size: 12px; }
.role p { color: var(--text-2); font-size: 13px; margin: 8px 0 10px; }
.role h5 { margin: 10px 0 4px; font-size: 11.5px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-3); }
.role ul { margin: 0; padding: 0; list-style: none; display: grid; gap: 5px; font-size: 13px; }
.role li .by { display: block; color: var(--text-3); font-size: 11.5px; }
.role .links { font-size: 12.5px; color: var(--text-2); }
.role .links b { color: var(--text); font-weight: 600; }
/* Decision rules */
.rules { margin: 0; padding: 0; list-style: none; display: grid; gap: 6px; font-size: 12.5px; }
.rules li { border-left: 2px solid var(--border); padding-left: 8px; }
.rules .w { color: var(--text-2); } .rules .t { font-weight: 600; }
.decide { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
.decide h3 { font-size: 14px; margin: 0 0 4px; } .decide .sub { color: var(--text-3); font-size: 12.5px; margin: 0 0 10px; }
.decide table { font-size: 13px; } .decide td { padding: 6px 8px 6px 0; border-bottom: 1px solid var(--border); }
.decide tr:last-child td { border-bottom: 0; }
.decide code { font-size: 11px; color: var(--text-3); }
.decide ol { margin: 0; padding-left: 18px; font-size: 13px; color: var(--text-2); display: grid; gap: 4px; }
@media (max-width: 600px) { .hand { grid-template-columns: 1fr; } .arrow { transform: rotate(90deg); width: 100%; text-align: center; } }
@media (max-width: 900px) { .board { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 600px) {
  .board { grid-template-columns: 1fr; }
  .todo { grid-template-columns: auto 1fr; }
  .todo .acts { grid-row: auto; grid-column: 1 / -1; justify-content: flex-start; }
  .hide-sm { display: none; } input[type=search] { min-width: 100%; }
  dl { grid-template-columns: 90px 1fr; }
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Agent work tracker</h1>
      <div class="meta" id="meta"></div>
    </div>
    <button class="theme" id="theme" type="button">Dark / light</button>
  </header>
  <nav class="tabs" role="tablist" aria-label="Views">
    <button type="button" role="tab" id="t-work" aria-controls="tab-work" aria-selected="true">Work</button>
    <button type="button" role="tab" id="t-ads" aria-controls="tab-ads" aria-selected="false">Amazon ads</button>
    <button type="button" role="tab" id="t-market" aria-controls="tab-market" aria-selected="false">Market &amp; customers</button>
    <button type="button" role="tab" id="t-shop" aria-controls="tab-shop" aria-selected="false">Mystery shopper</button>
    <button type="button" role="tab" id="t-how" aria-controls="tab-how" aria-selected="false">How the agents work together</button>
  </nav>
  <div id="tab-work" role="tabpanel" aria-labelledby="t-work">
  <div id="setup"></div>
  <section class="score" id="score" aria-label="This week"></section>
  <div class="sec-head"><h2>CEO plan</h2><p id="plan-note">From plan.toml. Approved work, in priority order.</p></div>
  <section class="table-wrap" id="plan"></section>
  <div class="sec-head"><h2>Agent work</h2><p></p></div>
  <p class="headline" id="headline" style="margin-top:0"></p>
  <section class="kpis" id="kpis" aria-label="Summary"></section>

  <div class="sec-head"><h2>Needs you</h2><p id="inbox-note"></p></div>
  <section class="inbox" id="inbox" aria-label="Needs you"></section>

  <div class="sec-head"><h2>All work</h2><p id="work-note"></p></div>
  <div class="filters">
    <div class="seg" role="group" aria-label="View">
      <button type="button" id="v-board" aria-pressed="true">Board</button>
      <button type="button" id="v-list" aria-pressed="false">List</button>
    </div>
    <span id="f-teams" role="group" aria-label="Team" style="display:contents"></span>
    <select id="f-agent" aria-label="Agent"></select>
    <input type="search" id="f-q" placeholder="Search work" aria-label="Search work">
    <button class="clear" id="f-clear" type="button">Clear</button>
  </div>
  <section id="board" class="board" aria-label="Board"></section>
  <section id="list" class="table-wrap" hidden>
    <table>
      <thead><tr><th>Work</th><th>Owner</th><th>Stage</th><th class="hide-sm">Priority</th>
        <th class="num">Est. impact</th><th class="num">Due</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </section>

  <div class="sec-head"><h2>The team</h2><p>Click an agent to see only their work.</p></div>
  <section class="team" id="team" aria-label="Agents"></section>
  <p class="note" id="foot"></p>
  </div>
  <div id="tab-ads" class="ads" role="tabpanel" aria-labelledby="t-ads" hidden>
    <p class="lead" id="ads-lead"></p>
    <section class="kpis" id="ads-kpis" aria-label="Amazon totals"></section>
    <div class="sec-head"><h2>What to do about it</h2><p>Bids and budgets are Quartile&rsquo;s: take the ad findings to them.</p></div>
    <section class="finds" id="ads-finds"></section>
    <div class="sec-head"><h2>Styles</h2><p>Each parent listing with its variants. &ldquo;From ads&rdquo;: share of its sales that ads brought in.</p></div>
    <section class="table-wrap" id="ads-styles"></section>
    <div class="two" style="margin-top:12px">
      <div class="panel"><h2>Sales by size</h2><div class="hbars" id="ads-sizes"></div></div>
      <div class="panel"><h2>Sales by color (top 12)</h2><div class="hbars" id="ads-colors"></div></div>
    </div>
    <div class="sec-head"><h2>Top 25 products</h2><p id="ads-top-note"></p></div>
    <section class="table-wrap" id="ads-top"></section>
    <p class="note" id="ads-foot"></p>
  </div>
  <div id="tab-market" role="tabpanel" aria-labelledby="t-market" hidden>
    <p class="lead" id="market-lead"></p>
    <div id="market-box"></div>
  </div>
  <div id="tab-shop" role="tabpanel" aria-labelledby="t-shop" hidden>
    <div id="live"></div>
    <div class="sec-head" id="auto-head" hidden><h2>Weekly automated shop</h2><p>The same checks every week, so changes show up.</p></div>
    <p class="lead" id="shop-lead"></p>
    <div id="shop-verdict"></div>
    <div class="sec-head"><h2>Scoreboard</h2><p>0&ndash;10 per area, from what the shopper found. Shaded: us.</p></div>
    <section class="table-wrap" id="shop-board"></section>
    <div class="sec-head"><h2>Each site, as a shopper saw it</h2><p id="shop-sites-note"></p></div>
    <section class="sites" id="shop-sites"></section>
    <div class="sec-head"><h2>What each store shows a shopper</h2><p>The checks behind the scores, on the product page and its policies.</p></div>
    <section class="table-wrap" id="shop-checks"></section>
    <p class="note" id="shop-foot"></p>
  </div>
  <div id="tab-how" role="tabpanel" aria-labelledby="t-how" hidden>
    <p class="lead">Every run, the agents go in a fixed order. Each one reads what the earlier ones
      found and leaves its own findings for the ones after it. No agent changes anything on its own:
      everything goes to the chief of staff, who turns it into one ranked plan and tells each team its part.</p>
    <div class="sec-head"><h2>The daily run</h2><p>Numbers are the order they run in.</p></div>
    <section class="flow" id="flow"></section>
    <div class="sec-head"><h2>Example: a new ad needs to be built</h2><p>Who does what, and where it shows up in the Work tab.</p></div>
    <div class="callout" id="strategist"></div>
    <div class="key" id="tl-key"></div>
    <section class="panel"><ol class="timeline" id="timeline"></ol></section>
    <div class="sec-head"><h2>Who tells whom</h2><p>What each agent passes along during a run.</p></div>
    <section class="panel" id="handoffs" style="padding-top:4px;padding-bottom:4px"></section>
    <div class="sec-head"><h2>How decisions are made</h2><p>Rules, not guesses. Every number below comes from the code or your .env.</p></div>
    <section class="decide" id="decide"></section>
    <div class="sec-head"><h2>What each agent does</h2><p>Its rules, what it can suggest, who carries that out, and the data it needs.</p></div>
    <section class="roster" id="roster"></section>
  </div>
</div>
<div id="shell">
  <div class="scrim" id="scrim"></div>
  <aside class="drawer" id="drawer" aria-label="Details" aria-hidden="true"></aside>
</div>
<div id="tip" role="tooltip"></div>
<script type="application/json" id="data">__DATA__</script>
<script>
(() => {
  const D = JSON.parse(document.getElementById('data').textContent);
  // Board columns, in the order work moves through them.
  const COLS = [
    ['review', 'To review', 'Suggested by an agent, or waiting for your OK', 'var(--st-review)'],
    ['doing', 'Being done', 'Approved; a person or the API is on it', 'var(--st-doing)'],
    ['done', 'Done', 'Finished in the last ' + D.window_days + ' days', 'var(--st-done)'],
    ['stopped', 'Stopped', 'Rejected or failed', 'var(--st-stopped)'],
  ];
  const COL_OF = {waiting: 'review', in_progress: 'doing', done: 'done', rejected: 'stopped', failed: 'stopped'};
  const COL_LABEL = Object.fromEntries(COLS.map(c => [c[0], c[1]]));
  const COL_COLOR = Object.fromEntries(COLS.map(c => [c[0], c[3]]));
  const STATUS = {
    proposed: ['Suggestion', ''], pending_approval: ['Needs your OK', 'ok'],
    approved: ['Approved', ''], acknowledged: ['Your team to do', 'you'],
    executed: ['Done by API', 'good'], done: ['Done', 'good'],
    rejected: ['Rejected', ''], failed: ['Failed', 'bad'],
  };
  const PRI = {now: 0, high: 1, medium: 2, low: 3, judgment: 4, '': 5};
  const PRI_LABEL = {now: 'Urgent', high: 'High', medium: 'Medium', low: 'Low', judgment: 'Your call'};
  const EFFORT_LABEL = {XS: 'Minutes', S: 'Under an hour', M: 'A couple of hours', L: 'About a day', XL: 'Several days'};
  const NAME = n => n === 'chief_of_staff' ? 'Chief of staff' : n === 'ux' ? 'UX' : n === 'ceo' ? 'CEO plan' : n[0].toUpperCase() + n.slice(1).replace(/_/g, ' ');
  const INITIALS = n => n === 'chief_of_staff' ? 'CS' : n === 'ux' ? 'UX' : n === 'ceo' ? 'CEO' : n.slice(0, 2).toUpperCase();
  const el = (tag, attrs = {}, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null) continue;
      if (k === 'class') e.className = v; else if (k === 'text') e.textContent = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v);
    }
    kids.flat().forEach(k => k != null && k !== false && e.append(k));
    return e;
  };
  const usd = v => v == null ? '—' : (v < 0 ? '−$' : '$') + Math.abs(Math.round(v)).toLocaleString();
  const fmt = n => n.toLocaleString();
  const age = d => d === 0 ? 'today' : d === 1 ? '1 day' : d + ' days';
  const plural = (n, w) => n + ' ' + w + (n === 1 ? '' : 's');
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const day = iso => MON[+iso.slice(5, 7) - 1] + ' ' + +iso.slice(8, 10);
  const ownerEl = a => a.owner ? el('span', {text: a.owner}) : el('span', {class: 'unowned', text: 'Unassigned'});
  const dueEl = a => !a.due ? null : a.overdue
    ? el('span', {class: 'overdue', text: 'overdue since ' + day(a.due)}) : el('span', {text: 'due ' + day(a.due)});

  // Theme toggle
  const root = document.documentElement;
  try { const t = localStorage.getItem('tracker-theme'); if (t) root.dataset.theme = t; } catch (e) {}
  document.getElementById('theme').onclick = () => {
    const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme = dark ? 'light' : 'dark';
    try { localStorage.setItem('tracker-theme', root.dataset.theme); } catch (e) {}
  };

  const tip = document.getElementById('tip');
  const showTip = (e, text) => { tip.textContent = text; tip.style.display = 'block';
    tip.style.left = Math.min(e.clientX + 12, innerWidth - tip.offsetWidth - 8) + 'px';
    tip.style.top = (e.clientY + 14) + 'px'; };
  const hideTip = () => { tip.style.display = 'none'; };

  for (const a of D.actions) a.col = COL_OF[a.stage] || 'review';
  const byPri = (x, y) => PRI[x.priority] - PRI[y.priority] || (y.impact || 0) - (x.impact || 0) || y.age - x.age;
  const needsOk = a => a.status === 'pending_approval';
  const needsDone = a => a.status === 'acknowledged';
  // Suggestions only reach the inbox when they're urgent or a judgment call.
  // CEO plan work has its own table; it reaches the inbox only when overdue.
  const isTodo = a => (a.source === 'plan' ? a.overdue && needsDone(a) : needsOk(a) || needsDone(a))
    || a.status === 'failed' || (a.status === 'proposed' && (a.priority === 'now' || a.priority === 'high'));

  // Header
  const meta = document.getElementById('meta');
  if (D.run) meta.append(el('span', {text: 'Last run ' + D.run.finished + ' UTC'}),
    el('span', {class: 'chip', text: {recommend: 'Suggest only', approve: 'Approve first', auto: 'Auto within limits'}[D.run.mode] || D.run.mode}));
  else meta.append(el('span', {text: 'The agents haven’t run yet'}));
  meta.append(el('span', {text: 'Sales data through ' + (D.data_through || 'n/a')}));

  // Setup problems: decisions are only as good as these.
  const gaps = [...(D.sample ? ['This is sample data (a water-bottle company from amz-agents demo), not Dagacci.'] : []), ...D.setup_gaps];
  if (gaps.length) document.getElementById('setup').append(el('div', {class: 'warn-box'},
    el('b', {text: 'Fix before trusting these numbers: '}), 'the agents are only as good as the data and goals they get.',
    el('ul', {}, gaps.map(g => el('li', {text: g})))));

  // This week: the business, and the goals the chief of staff steers by.
  const S = D.scorecard, score = document.getElementById('score');
  const chg = (a, b, inverse) => {
    if (a == null || b == null || !b) return null;
    const d = a / b - 1, good = inverse ? d < 0 : d > 0;
    return el('span', {class: 'delta ' + (Math.abs(d) < 0.005 ? '' : good ? 'up' : 'down'),
      text: (d > 0 ? '\u25b2 ' : d < 0 ? '\u25bc ' : '') + Math.abs(d * 100).toFixed(1) + '% vs prior week'});
  };
  const stile = (label, value, delta, sub) => el('div', {class: 'tile'}, el('div', {class: 'label', text: label}),
    el('div', {class: 'value', text: value}), delta, sub ? el('div', {class: 'sub', text: sub}) : null);
  const pct = v => v == null ? '\u2014' : (v * 100).toFixed(1) + '%';
  const biz = el('div', {class: 'panel'}, el('h2', {text: S ? 'This week (7 days to ' + S.through + ')' : 'This week'}));
  if (S) {
    biz.append(el('div', {class: 'kpis', style: 'margin-top:12px'},
      stile('Contribution profit', usd(S.now.profit), chg(S.now.profit, S.prior.profit), S.now.profit == null ? 'needs product costs' : null),
      stile('Revenue', usd(S.now.revenue), chg(S.now.revenue, S.prior.revenue)),
      stile('Units', fmt(S.now.units), chg(S.now.units, S.prior.units)),
      stile('TACoS', pct(S.now.tacos), chg(S.now.tacos, S.prior.tacos, true), 'ad spend \u00f7 revenue')));
  } else if (D.ads) {
    const T = D.ads.totals;
    biz.querySelector('h2').textContent = 'Amazon, ' + day(D.ads.start) + ' \u2013 ' + day(D.ads.end) + ' (Quartile)';
    biz.append(el('div', {class: 'kpis', style: 'margin-top:12px'},
      stile('Sales', usd(T.sales), null, '\u2248 ' + usd(T.monthly) + ' a month'),
      stile('Ad spend', usd(T.spend), null, D.ads.ad_types),
      stile('TACoS', pct(T.tacos), null, 'ad spend \u00f7 sales'),
      stile('ACoS', pct(T.acos), null, 'ad spend \u00f7 ad sales')),
      el('p', {class: 'optim', text: 'Period totals from Quartile, not this week. Once Seller Central is connected this shows revenue, profit, units and ad efficiency each week.'}));
  } else biz.append(el('p', {class: 'optim', text: 'No sales data yet. Once Seller Central is connected this shows revenue, profit, units and ad efficiency each week.'}));
  biz.append(el('p', {class: 'optim'}, el('b', {text: 'What the agents optimize: '}),
    'contribution profit (sales \u2212 product cost \u2212 Amazon fees \u2212 ad spend). Every suggestion is ranked by how much profit it should add over 30 days per hour of your team\u2019s time, then checked against the goals on the right.'));
  const gl = el('div', {class: 'panel'}, el('h2', {text: 'Goals'}), el('p', {class: 'optim', style: 'margin-top:2px', text: 'From goals.toml. Last 7 days vs days 15\u201328 ago.'}));
  const GS = {'on track': 'good', behind: 'you', 'off track': 'bad', 'no data': ''};
  if (!D.goals.length) gl.append(el('p', {class: 'optim', text: 'No goals set. Add them to goals.toml.'}));
  for (const g of D.goals) gl.append(el('div', {class: 'goal'}, el('div', {text: g.name}),
    el('span', {class: 'tag ' + (g.known ? GS[g.status] || '' : 'bad'), text: g.known ? g.status : 'not our product'}),
    el('div', {class: 'gs', text: 'Target ' + g.target + ' \u00b7 now ' + g.actual + (g.scope !== 'all' ? ' \u00b7 ' + g.scope : '')})));
  score.append(biz, gl);

  // CEO plan
  const planBox = document.getElementById('plan');
  if (!D.plan.length) planBox.append(el('div', {class: 'empty', text: 'No plan.toml yet.'}));
  else {
    const byId = Object.fromEntries(D.actions.map(a => [a.id, a]));
    const ST = {met: ['Target met', 'good'], no: ['Not yet', 'bad'], manual: ['Check by hand', '']};
    const gainTxt = p => p.gain_high ? '$' + fmt(p.gain_low) + '\u2013' + fmt(p.gain_high) + '/mo' : 'unknown';
    planBox.append(el('table', {class: 'plan'},
      el('thead', {}, el('tr', {}, el('th', {text: '#'}), el('th', {text: 'Change'}), el('th', {text: 'Owner'}),
        el('th', {class: 'hide-sm', text: 'KPI: baseline \u2192 now \u2192 target'}), el('th', {class: 'num hide-sm', text: 'Est. gain'}),
        el('th', {class: 'hide-sm', text: 'Effort'}), el('th', {class: 'num', text: 'Due'}))),
      el('tbody', {}, D.plan.map(p => {
        const a = byId[p.action_id], done = p.status === 'done';
        const sc = p.score || {}, st = done ? ['Done', 'good'] : sc.met === true ? ST.met : sc.met === false ? ST.no : ST.manual;
        const team = D.teams[p.team] || p.team;
        return el('tr', {class: done ? 'done' : ''},
          el('td', {class: 'rk', text: p.rank}),
          el('td', {}, a ? el('button', {type: 'button', class: 'ttl', text: p.title, onclick: () => open(a)}) : el('span', {text: p.title}),
            p.merged ? el('div', {class: 'why', style: 'color:var(--text-3);font-size:12px', text: 'merges ' + plural(p.merged, 'agent suggestion')}) : null),
          el('td', {}, p.owner ? el('span', {text: p.owner}) : el('span', {class: 'unowned', text: 'Unassigned'}),
            el('div', {style: 'color:var(--text-3);font-size:12px', text: team})),
          el('td', {class: 'hide-sm'}, el('div', {class: 'kpi'}, p.kpi),
            el('div', {class: 'kpi'}, p.baseline + ' \u2192 ', el('b', {text: sc.now || '?'}), ' \u2192 ' + p.target + ' ',
              el('span', {class: 'tag ' + st[1], text: st[0]}))),
          el('td', {class: 'num hide-sm', text: gainTxt(p)}),
          el('td', {class: 'hide-sm', text: p.loe || p.effort}),
          el('td', {class: 'num' + (p.overdue && !done ? ' overdue' : ''), text: done ? 'done' : p.due ? (p.overdue ? 'overdue' : day(p.due)) : '\u2014'}));
      }))));
    const lo = D.plan.reduce((n, p) => n + (p.gain_low || 0), 0), hi = D.plan.reduce((n, p) => n + (p.gain_high || 0), 0);
    const met = D.plan.filter(p => p.status === 'done' || (p.score && p.score.met)).length;
    document.getElementById('plan-note').textContent = met + ' of ' + D.plan.length + ' at target \u00b7 est. $'
      + fmt(lo) + '\u2013' + fmt(hi) + '/mo if all land \u00b7 click a change for its steps and first draft';
  }

  // Headline sentence
  const K = D.kpis;
  // Work only a person can move goes first, then the best ideas.
  const mustAct = a => needsOk(a) || needsDone(a) || a.status === 'failed' ? 0 : 1;
  const todos = D.actions.filter(isTodo).sort((x, y) => mustAct(x) - mustAct(y) || byPri(x, y));
  const sick = D.agents.filter(a => a.problems.length);
  const hl = document.getElementById('headline');
  if (!D.run) {
    hl.append('Nothing to track yet. Run ', el('b', {text: 'amz-agents run'}),
      ' (or ', el('b', {text: 'amz-agents demo'}), ' for sample data) and reload this page.');
  } else {
    const parts = [];
    if (todos.length) parts.push([el('b', {text: plural(todos.length, 'item')}), ' need you']);
    if (K.in_progress) parts.push([el('b', {text: fmt(K.in_progress)}), ' being done']);
    parts.push([el('b', {text: fmt(K.done)}), ' finished in ' + D.window_days + ' days']);
    parts.forEach((p, i) => { if (i) hl.append(i === parts.length - 1 ? ' and ' : ', '); hl.append(...p); });
    hl.append('.');
    if (K.waiting_impact > 0) hl.append(' Open suggestions are worth about ', el('b', {text: usd(K.waiting_impact)}), ' over 30 days.');
    if (sick.length) hl.append(' ', el('b', {text: plural(sick.length, 'agent')}), sick.length === 1 ? ' needs' : ' need', ' attention.');
  }

  // State and filters
  const state = {view: 'board', team: '', agent: '', col: '', q: ''};
  try { const v = localStorage.getItem('tracker-view'); if (v === 'list' || v === 'board') state.view = v; } catch (e) {}
  const focus = sel => { const t = document.querySelector(sel); if (t) t.scrollIntoView({behavior: 'smooth', block: 'start'}); };

  // KPI tiles: each one filters the board.
  const tile = (label, value, sub, color, onclick, alert) => el('button', {type: 'button', class: 'tile' + (alert ? ' alert' : ''), onclick},
    el('div', {class: 'label'}, color ? el('i', {class: 'dot', style: 'background:' + color}) : null, label),
    el('div', {class: 'value', text: value}), sub ? el('div', {class: 'sub', text: sub}) : null);
  const show = col => () => { Object.assign(state, {col, team: '', agent: '', q: ''}); fQ.value = ''; sync(); focus('#f-q'); };
  document.getElementById('kpis').append(
    tile('Needs you', fmt(todos.length), todos.length ? 'approvals, to-dos and urgent ideas' : 'all caught up', null,
      () => focus('#inbox'), todos.some(a => a.priority === 'now')),
    tile('To review', fmt(K.waiting), usd(K.waiting_impact) + ' est. 30-day value', 'var(--st-review)', show('review')),
    tile('Being done', fmt(K.in_progress), 'approved, in motion', 'var(--st-doing)', show('doing')),
    tile('Done', fmt(K.done), 'last ' + D.window_days + ' days', 'var(--st-done)', show('done')),
    tile('Agents healthy', K.healthy + ' / ' + K.agents, K.healthy < K.agents ? 'some are missing data' : 'all running', null,
      () => focus('#team'), K.healthy < K.agents));

  // Actions against the server (only when it was started with --allow-actions)
  async function act(a, op, btn) {
    const box = btn.parentElement;
    box.querySelectorAll('button').forEach(b => b.disabled = true);
    try {
      const r = await fetch('/api/action', {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Tracker-Token': D.token},
        body: JSON.stringify({id: a.id, op})});
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || r.status);
      location.reload();
    } catch (e) {
      box.querySelectorAll('button').forEach(b => b.disabled = false);
      box.append(el('span', {class: 'msg', text: 'Could not ' + op + ': ' + e.message}));
    }
  }
  const buttons = a => {
    if (!D.token) return [];
    if (needsOk(a)) return [el('button', {type: 'button', class: 'btn primary', text: 'Approve', onclick: e => { e.stopPropagation(); act(a, 'approve', e.target); }}),
      el('button', {type: 'button', class: 'btn', text: 'Reject', onclick: e => { e.stopPropagation(); act(a, 'reject', e.target); }})];
    if (needsDone(a)) return [el('button', {type: 'button', class: 'btn primary', text: 'Mark done', onclick: e => { e.stopPropagation(); act(a, 'done', e.target); }})];
    return [];
  };
  const tags = a => {
    const [label, cls] = STATUS[a.status] || [a.status, ''];
    return [
      a.priority && a.priority !== 'medium' && a.priority !== 'low' ? el('span', {class: 'tag ' + a.priority, text: PRI_LABEL[a.priority]}) : null,
      el('span', {class: 'tag ' + cls, text: label}),
      a.verdict ? el('span', {class: 'tag ' + (a.verdict === 'helped' ? 'good' : a.verdict === 'hurt' ? 'bad' : ''), text: 'Result: ' + a.verdict}) : null,
    ];
  };

  // Details drawer
  const shell = document.getElementById('shell'), drawer = document.getElementById('drawer');
  let lastFocus = null;
  const close = () => { shell.classList.remove('open'); drawer.setAttribute('aria-hidden', 'true'); if (lastFocus) lastFocus.focus(); };
  document.getElementById('scrim').onclick = close;
  addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  function open(a) {
    lastFocus = document.activeElement;
    const order = ['review', 'doing', 'done'];
    const steps = el('div', {class: 'steps'}, ...order.map((c, i) => el('div', {
      class: a.col === 'stopped' ? (i === 0 ? 'stop' : '') : (order.indexOf(a.col) >= i ? 'on' : ''),
      text: a.col === 'stopped' && i === 0 ? (a.status === 'failed' ? 'Failed' : 'Rejected') : COL_LABEL[c]})));
    const row = (k, v) => v == null || v === '' ? [] : [el('dt', {text: k}), el('dd', {text: v})];
    const acts = buttons(a);
    let help = null;
    if (!D.token && (needsOk(a) || needsDone(a))) help = el('p', {class: 'note', text: 'To approve or finish this here, start the tracker with: amz-agents dashboard --serve --allow-actions'});
    else if (a.status === 'proposed') help = el('p', {class: 'note', text: 'Agents are in suggest-only mode, so this is advice: act on it yourself, or switch AGENT_MODE to approve so suggestions queue for your OK.'});
    drawer.replaceChildren(...[
      el('button', {type: 'button', class: 'x', 'aria-label': 'Close', text: '×', onclick: close}),
      el('div', {class: 'dtags'}, tags(a)),
      el('h3', {text: a.title}), steps,
      el('div', {class: 'why-box', text: (a.reason || 'No reason recorded.') + (a.notes ? '\n\n' + a.notes : '')}),
      a.draft && a.draft.text ? [el('h5', {style: 'margin:0 0 4px;font-size:12px;color:var(--text-3)',
        text: 'FIRST DRAFT \u00b7 ' + NAME(a.draft.by || 'agent').toUpperCase() + ' AGENT' + (a.draft.voice === 'claude' ? '' : ' (OUTLINE)')}),
        el('div', {class: 'draft', text: a.draft.text})] : null,
      acts.length ? el('div', {class: 'acts'}, acts) : null,
      a.steps && a.steps.length ? [el('h5', {style: 'margin:0 0 4px;font-size:12px;color:var(--text-3)', text: 'WHAT TO DO'}),
        el('ul', {style: 'margin:0 0 16px;padding-left:18px;font-size:13px'}, a.steps.map(t => el('li', {text: t})))] : null,
      el('dl', {}, row('Owner', a.owner || 'Unassigned (set it in teams.toml)'), row('Due', a.due ? day(a.due) + (a.overdue ? ' (overdue)' : '') : ''),
        row('Agent', NAME(a.agent)), row('Team', D.teams[a.team] || ''), row('Est. impact', a.impact == null ? '' : usd(a.impact) + ' over 30 days'),
        row('Effort', a.loe || (a.effort ? EFFORT_LABEL[a.effort] || a.effort : '')), row('Return', a.roi == null ? '' : a.roi + '× the time it takes'),
        row('Product', a.product ? a.product + ' (' + a.sku + ')' : a.sku), row('Raised', a.created + ' (' + age(a.age) + (a.age ? ' ago' : '') + ')'), row('Item', '#' + a.id + ' · ' + a.kind.replace(/_/g, ' '))),
      a.url && /^https:\/\//.test(a.url) ? el('a', {href: a.url, target: '_blank', rel: 'noopener', text: 'Open the product page'}) : null,
      help].flat().filter(Boolean));
    shell.classList.add('open'); drawer.setAttribute('aria-hidden', 'false');
    drawer.querySelector('.x').focus();
  }

  function openRec(r) {
    lastFocus = document.activeElement;
    drawer.replaceChildren(...[
      el('button', {type: 'button', class: 'x', 'aria-label': 'Close', text: '\u00d7', onclick: close}),
      el('div', {class: 'dtags'}, el('span', {class: 'tag ' + (r.when === 1 ? 'now' : 'high'), text: r.when_label}),
        el('span', {class: 'tag', text: r.area}), el('span', {class: 'tag', text: r.effort + ' effort'})),
      el('h3', {text: r.title}),
      el('h5', {style: 'margin:0 0 4px;font-size:12px;color:var(--text-3)', text: 'WHAT WE SEE'}), el('div', {class: 'why-box', text: r.see}),
      el('h5', {style: 'margin:0 0 4px;font-size:12px;color:var(--text-3)', text: 'WHY IT MATTERS'}), el('div', {class: 'why-box', text: r.why}),
      el('h5', {style: 'margin:0 0 4px;font-size:12px;color:var(--text-3)', text: 'WHAT TO DO'}),
      el('ul', {style: 'margin:0 0 16px;padding-left:18px;font-size:13px'}, (r.do || []).map(t => el('li', {text: t}))),
      r.kpi ? el('dl', {}, el('dt', {text: 'Track'}), el('dd', {text: r.kpi}), el('dt', {text: 'Now \u2192 target'}), el('dd', {text: r.now + ' \u2192 ' + r.target})) : null,
      r.url && /^https:\/\//.test(r.url) ? el('a', {href: r.url, target: '_blank', rel: 'noopener', text: 'Open the product page'}) : null,
      el('p', {class: 'note', text: 'From the market tracker. It isn\u2019t an agent action yet, so assign it to a team yourself.'}),
    ].filter(Boolean));
    shell.classList.add('open'); drawer.setAttribute('aria-hidden', 'false');
    drawer.querySelector('.x').focus();
  }

  // Inbox: what needs a person now, plus agents that are missing data.
  const INBOX_MAX = 6;
  const inbox = document.getElementById('inbox');
  function renderInbox() {
    inbox.replaceChildren();
    if (!todos.length && !sick.length && (D.actions.some(a => a.source === 'market') || !(D.market && D.market.recs && D.market.recs.some(r => r.when <= 2)))) { inbox.append(el('div', {class: 'caught-up', text: D.run ? 'You’re all caught up. Nothing needs you right now.' : 'Nothing yet.'})); return; }
    for (const a of todos.slice(0, INBOX_MAX)) {
      const what = needsOk(a) ? 'Approve or reject' : needsDone(a) ? 'Do it, then mark done' : a.status === 'failed' ? 'Failed: take a look' : 'Worth acting on';
      inbox.append(el('div', {class: 'todo p-' + (a.priority || 'none')},
        el('span', {class: 'avatar', title: NAME(a.agent), text: INITIALS(a.agent)}),
        el('button', {type: 'button', class: 't', text: a.title, onclick: () => open(a)}),
        el('div', {class: 'acts'}, buttons(a).length ? buttons(a) : el('button', {type: 'button', class: 'btn', text: 'Details', onclick: () => open(a)})),
        el('div', {class: 's'}, what + ' · ', ownerEl(a), ' (' + (D.teams[a.team] || NAME(a.agent)) + ')' + (a.impact > 0 ? ' · ' + usd(a.impact) : '') + ' · ',
          dueEl(a) || el('span', {class: a.age >= D.stale_days ? 'stale' : '', text: a.age ? 'waiting ' + age(a.age) : 'new today'}))));
    }
    if (todos.length > INBOX_MAX) inbox.append(el('div', {class: 'more', text: '+ ' + (todos.length - INBOX_MAX) + ' more in To review below'}));
    // Once the agents have queued the market tracker's findings, they're in the list above.
    const queued = D.actions.some(a => a.source === 'market');
    const mrecs = queued ? [] : (D.market && D.market.recs || []).filter(r => r.when <= 2).slice(0, 4);
    if (mrecs.length) {
      inbox.append(el('div', {class: 'group-label', text: 'From the market tracker (dagacci.com, reviews, competitors)'}));
      for (const r of mrecs) inbox.append(el('div', {class: 'todo mkt'},
        el('span', {class: 'avatar', text: 'MT'}),
        el('button', {type: 'button', class: 't', text: r.title, onclick: () => openRec(r)}),
        el('div', {class: 'acts'}, el('button', {type: 'button', class: 'btn', text: 'Details', onclick: () => openRec(r)})),
        el('div', {class: 's', text: r.when_label + ' \u00b7 ' + r.area + ' \u00b7 ' + r.effort + ' effort' + (r.kpi ? ' \u00b7 track ' + r.kpi + ': ' + r.now + ' \u2192 ' + r.target : '')})));
    }
    for (const g of sick) inbox.append(el('div', {class: 'todo health'},
      el('span', {class: 'avatar', text: INITIALS(g.name)}),
      el('button', {type: 'button', class: 't', text: NAME(g.name) + ' agent can’t do its job', onclick: () => { state.agent = g.name; sync(); focus('#f-q'); }}),
      el('div', {class: 'acts'}),
      el('div', {class: 's', text: g.problems.join('; ')})));
  }
  renderInbox();
  document.getElementById('inbox-note').textContent = D.token ? 'Approve, reject or mark done right here.' : 'Click any item for details.';

  // Filters
  const teamBox = document.getElementById('f-teams');
  const teamBtns = [['', 'All teams'], ...Object.entries(D.teams)].map(([k, v]) => {
    const b = el('button', {type: 'button', class: 'pill', text: v, onclick: () => { state.team = state.team === k ? '' : k; sync(); }});
    b.dataset.k = k; teamBox.append(b); return b;
  });
  const fAgent = document.getElementById('f-agent'), fQ = document.getElementById('f-q');
  const agentNames = [...new Set([...D.agents.map(a => a.name), ...D.actions.map(a => a.agent)])];
  fAgent.append(el('option', {value: '', text: 'All agents'}), ...agentNames.map(n => el('option', {value: n, text: NAME(n)})));
  fAgent.onchange = () => { state.agent = fAgent.value; sync(); };
  fQ.oninput = () => { state.q = fQ.value.trim().toLowerCase(); sync(); };
  document.getElementById('f-clear').onclick = () => { Object.assign(state, {team: '', agent: '', col: '', q: ''}); fQ.value = ''; sync(); };
  const vBoard = document.getElementById('v-board'), vList = document.getElementById('v-list');
  const setView = v => { state.view = v; try { localStorage.setItem('tracker-view', v); } catch (e) {} sync(); };
  vBoard.onclick = () => setView('board'); vList.onclick = () => setView('list');

  // Team cards
  const teamEl = document.getElementById('team');
  const agentCards = {};
  for (const g of D.agents) {
    const st = {review: g.stages.waiting || 0, doing: g.stages.in_progress || 0, done: g.stages.done || 0,
      stopped: (g.stages.rejected || 0) + (g.stages.failed || 0)};
    const total = Object.values(st).reduce((x, y) => x + y, 0);
    const bar = el('div', {class: 'bar', role: 'img', 'aria-label': COLS.filter(c => st[c[0]]).map(c => c[1] + ' ' + st[c[0]]).join(', ') || 'No work yet'});
    for (const [k, label, , color] of COLS) {
      if (!st[k]) continue;
      const s = el('div', {style: 'flex:' + st[k] + ';background:' + color});
      s.addEventListener('mousemove', e => showTip(e, label + ': ' + st[k] + ' of ' + total));
      s.addEventListener('mouseleave', hideTip);
      bar.append(s);
    }
    const ok = !g.problems.length;
    let line = ok ? g.mission : g.problems.join('; ');
    if (ok && g.oldest_waiting != null && g.oldest_waiting >= D.stale_days) line = 'Oldest item has waited ' + age(g.oldest_waiting) + '.';
    const card = el('button', {type: 'button', class: 'agent', 'aria-pressed': 'false',
      onclick: () => { state.agent = state.agent === g.name ? '' : g.name; sync(); focus('#f-q'); }},
      el('span', {class: 'avatar', style: 'grid-row:1 / span 2', text: INITIALS(g.name)}),
      el('div', {class: 'nm'}, NAME(g.name),
        el('span', {class: 'chip ' + (ok ? 'health-ok' : 'health-bad')}, el('i', {class: 'dot', style: 'background:' + (ok ? 'var(--good)' : 'var(--crit)')}), ok ? 'Healthy' : 'Needs attention')),
      el('div', {class: 'sub', text: g.team_label + ' · ' + plural(g.proposed_last_run, 'idea') + ' last run'}),
      el('div', {class: 'line' + (ok ? '' : ' bad'), text: line}),
      total ? bar : null,
      el('div', {class: 'counts'}, total ? COLS.filter(c => st[c[0]]).map(c => el('span', {}, el('b', {text: st[c[0]]}), ' ' + c[1].toLowerCase()))
        : el('span', {text: 'No work yet'}),
        g.helped || g.hurt ? el('span', {text: g.helped + ' helped · ' + g.hurt + ' hurt'}) : null));
    agentCards[g.name] = card;
    teamEl.append(card);
  }

  // Board and list
  const COL_MAX = 8;
  const expanded = {};
  const board = document.getElementById('board'), list = document.getElementById('list'), tbody = document.getElementById('rows');
  function card(a) {
    return el('button', {type: 'button', class: 'item', onclick: () => open(a)},
      el('div', {class: 'tags'}, tags(a)),
      el('div', {class: 't', text: a.title}),
      el('div', {class: 'who'}, el('span', {}, ownerEl(a), ' · ',
          (a.col === 'review' || a.col === 'doing') && a.due ? dueEl(a) : el('span', {text: age(a.age) + (a.age ? ' ago' : '')})),
        a.impact > 0 ? el('span', {class: 'imp', text: usd(a.impact)}) : null));
  }
  function sync() {
    fAgent.value = state.agent; vBoard.setAttribute('aria-pressed', String(state.view === 'board')); vList.setAttribute('aria-pressed', String(state.view === 'list'));
    teamBtns.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.k === state.team)));
    for (const [n, c] of Object.entries(agentCards)) { c.classList.toggle('selected', n === state.agent); c.setAttribute('aria-pressed', String(n === state.agent)); }
    const rows = D.actions.filter(a => (!state.team || a.team === state.team) && (!state.agent || a.agent === state.agent)
      && (!state.col || a.col === state.col)
      && (!state.q || (a.title + ' ' + a.reason + ' ' + a.sku + ' ' + NAME(a.agent)).toLowerCase().includes(state.q)));
    board.hidden = state.view !== 'board'; list.hidden = state.view !== 'list';
    board.replaceChildren();
    for (const [k, label, hint, color] of COLS) {
      if (state.col && state.col !== k) continue;
      const mine = rows.filter(a => a.col === k).sort(k === 'review' ? byPri : (x, y) => x.age - y.age || y.id - x.id);
      const body = el('div', {class: 'col-body'});
      const cap = expanded[k] || state.col ? mine.length : COL_MAX;
      mine.slice(0, cap).forEach(a => body.append(card(a)));
      if (!mine.length) body.append(el('div', {class: 'empty', text: 'Nothing here'}));
      if (mine.length > cap) body.append(el('button', {type: 'button', class: 'col-more', text: 'Show ' + (mine.length - cap) + ' more', onclick: () => { expanded[k] = true; sync(); }}));
      board.append(el('div', {class: 'col'},
        el('div', {class: 'col-head'}, el('i', {class: 'dot', style: 'background:' + color}),
          el('span', {}, label, el('span', {class: 'hint', text: hint})), el('span', {class: 'n', text: mine.length})), body));
    }
    board.style.gridTemplateColumns = state.col ? '1fr' : '';
    tbody.replaceChildren();
    const sorted = [...rows].sort((x, y) => COLS.findIndex(c => c[0] === x.col) - COLS.findIndex(c => c[0] === y.col) || byPri(x, y));
    if (!sorted.length) tbody.append(el('tr', {}, el('td', {colspan: '6', class: 'empty', text: 'Nothing matches these filters.'})));
    for (const a of sorted) {
      const [label] = STATUS[a.status] || [a.status];
      tbody.append(el('tr', {onclick: () => open(a)},
        el('td', {}, el('div', {style: 'font-weight:550', text: a.title})),
        el('td', {}, ownerEl(a), el('div', {class: 'why', style: 'color:var(--text-3);font-size:12px', text: NAME(a.agent) + ' agent'})),
        el('td', {}, el('span', {class: 'chip'}, el('i', {class: 'dot', style: 'background:' + COL_COLOR[a.col]}), label)),
        el('td', {class: 'hide-sm', text: PRI_LABEL[a.priority] || '—'}),
        el('td', {class: 'num', text: usd(a.impact)}),
        el('td', {class: 'num' + (a.overdue ? ' overdue' : a.col === 'review' && a.age >= D.stale_days ? ' stale' : ''), text: a.due && (a.col === 'review' || a.col === 'doing') ? (a.overdue ? 'overdue' : 'due ' + day(a.due)) : age(a.age)})));
    }
    const filtered = state.team || state.agent || state.col || state.q;
    document.getElementById('work-note').textContent = (filtered ? rows.length + ' of ' : '') + plural(D.actions.length, 'item') + ' · click any card for details';
  }
  sync();

  // ------------------------------------------------ How the agents work together
  const KIND = {reorder: 'Reorder stock', flag_defect: 'Flag a product defect', update_price: 'Change an Amazon price',
    update_bid: 'Change a keyword bid', add_keyword: 'Add a search keyword', add_negative: 'Block a wasted search term',
    update_listing_copy: 'Rewrite listing copy', update_campaign_budget: 'Change a campaign budget',
    pause_campaign: 'Pause a campaign', email_campaign: 'Draft an email campaign',
    update_channel_price: 'Change a price on another channel', list_on_channel: 'List on a new channel',
    send_outreach: 'Draft wholesale outreach', refresh_creative: 'Refresh a tired ad', pause_creative: 'Pause an ad',
    new_creative_variants: 'Make variants of a winning ad', improve_listing_assets: 'Add Amazon images, video or A+',
    fix_listing_title: 'Fix a listing title', fix_funnel_step: 'Fix a store checkout step',
    improve_page_speed: 'Speed up a page', fix_accessibility: 'Fix an accessibility issue',
    fix_landing_page: 'Fix an ad landing page', improve_listing_conversion: 'Improve an Amazon page that converts poorly',
    fix_fit: 'Fix fit or the size chart', restock_style: 'Restock a style on dagacci.com',
    improve_product: 'Fix what buyers dislike', plan_launch: 'Plan a launch (new styles or colors)',
    improve_shopping_experience: 'Fix what the mystery shopper missed on dagacci.com',
    opportunity: 'Raise an opportunity for another agent', escalation: 'Escalate a decision to you'};
  const TABLE = {products: 'products & costs', metrics_daily: 'daily sales', reviews: 'reviews', keywords: 'ad keywords',
    search_terms: 'search terms', external_campaigns: 'Meta / Google / TikTok campaigns', channel_listings: 'other-channel listings',
    leads: 'wholesale leads', creatives: 'ad creative results', listing_assets: 'Amazon images & A+',
    web_funnel: 'store funnel (GA4)', web_pages: 'page speed', asin_traffic: 'Amazon page traffic'};
  const R = Object.fromEntries(D.roster.map(r => [r.name, r]));
  const PSEUDO = {everyone: ['··', 'Every agent'], market: ['MT', 'Market tracker']};
  const chip = n => el('span', {class: 'ag'}, el('span', {class: 'avatar', text: PSEUDO[n] ? PSEUDO[n][0] : INITIALS(n)}),
    PSEUDO[n] ? PSEUDO[n][1] : NAME(n), R[n] ? el('span', {class: 'n', text: R[n].step}) : null);

  // Run order, grouped the way work moves.
  const agents = D.roster.filter(r => r.name !== 'chief_of_staff');
  const STAGES = [
    ['1 · Find', 'Amazon operations first, then outside channels, then the reviewers', agents.map(r => chip(r.name))],
    ['2 · Plan', 'Size, rank, assign a team, escalate', [chip('chief_of_staff')]],
    ['3 · Tell', 'Daily brief, a brief per team (email), this tracker', [el('span', {class: 'ag', text: 'Briefs + Work tab'})]],
    ['4 · Do', 'Approved price, bid and budget changes go through the API; everything else is a person', [
      el('span', {class: 'ag', text: 'API'}), el('span', {class: 'ag', text: 'Your teams'})]],
  ];
  const flow = document.getElementById('flow');
  STAGES.forEach(([h, sub, chips], i) => {
    if (i) flow.append(el('span', {class: 'arrow', 'aria-hidden': 'true', text: '\u2192'}));
    flow.append(el('div', {class: 'stage', style: i ? '' : 'flex-basis:420px;flex-grow:3'},
      el('h4', {text: h}), el('div', {class: 'chips'}, chips), el('div', {class: 'sub', style: 'color:var(--text-3);font-size:12px;margin-top:6px', text: sub})));
  });

  // Walkthrough
  document.getElementById('strategist').append(el('b', {text: 'Where\u2019s the strategist? '}),
    'There isn\u2019t a separate strategist agent. The job is split three ways: the ',
    el('b', {text: 'Creative'}), ' agent spots ads that need to be built, the ', el('b', {text: 'Data'}),
    ' agent finds opportunities the others\u2019 rules miss and routes them to the right agent, and the ',
    el('b', {text: 'Chief of staff'}), ' ranks everything and tells each team what\u2019s theirs.');
  const ACTOR = {agent: ['Agent', 'var(--accent)'], person: ['A person', 'var(--warn)'], system: ['Data feed', 'var(--text-3)']};
  document.getElementById('tl-key').append(...Object.values(ACTOR).map(([l, c]) => el('span', {}, el('i', {class: 'dot', style: 'background:' + c}), l)));
  const tl = document.getElementById('timeline');
  D.walkthrough.forEach((st, i) => {
    const [label, cls] = STATUS[st.status] || [];
    tl.append(el('li', {},
      el('span', {class: 'num ' + st.actor, text: i + 1}),
      el('div', {},
        el('div', {class: 'who'}, R[st.who] ? chip(st.who) : el('span', {text: st.who}),
          label ? el('span', {class: 'tag ' + cls, text: 'Work tab: ' + label}) : null),
        el('div', {class: 'ttl', text: st.title}), el('div', {class: 'txt', text: st.text}))));
  });

  // Handoffs
  const hands = document.getElementById('handoffs');
  for (const h of D.handoffs) hands.append(el('div', {class: 'hand'},
    el('div', {class: 'route'}, chip(h.from), el('span', {class: 'arrow', text: '\u2192', style: 'font-size:14px;transform:none;width:auto'})),
    el('div', {}, el('div', {class: 'route'}, h.to.map(chip)), el('div', {class: 'what', style: 'margin-top:6px', text: h.what}),
      el('div', {class: 'why', text: h.why}))));

  // How decisions are made
  const RU = D.rules;
  const ruleList = rows => el('ul', {class: 'rules'}, rows.map(r => el('li', {},
    el('span', {class: 'w', text: r.then ? 'When ' + r.when + ' \u2192 ' : r.when}), r.then ? el('span', {class: 't', text: r.then}) : null)));
  document.getElementById('decide').append(
    el('div', {class: 'panel'}, el('h3', {text: '1. Each agent checks its rules'}),
      el('p', {class: 'sub', text: 'Every run, each agent compares your latest numbers to fixed thresholds. When one is crossed it suggests a specific change and records the numbers behind it as the reason. See each agent\u2019s card below for its rules.'}),
      el('ol', {}, el('li', {text: 'Is there enough data to judge? (e.g. a minimum number of clicks)'}),
        el('li', {text: 'Is a threshold crossed?'}), el('li', {text: 'Does it clash with what another agent found? (e.g. low stock blocks more ad spend)'}),
        el('li', {text: 'Suggest the change, within the hard limits.'}))),
    el('div', {class: 'panel'}, el('h3', {text: '2. Hard limits nothing can break'}),
      el('p', {class: 'sub', text: 'Any suggestion outside these is pulled back inside. Change them in .env.'}),
      el('table', {}, el('tbody', {}, RU.limits.map(l => el('tr', {}, el('td', {text: l.what}),
        el('td', {}, el('div', {style: 'font-weight:600', text: l.value}), el('code', {text: l.env}))))))),
    el('div', {class: 'panel'}, el('h3', {text: '3. The chief of staff ranks it'}),
      el('p', {class: 'sub', text: 'Each suggestion gets a 30-day profit estimate and a time cost (effort hours \u00d7 $' + RU.hourly_cost + '/hr). ROI = value \u00f7 time cost.'}),
      el('table', {}, el('tbody', {}, RU.ranking.map(r => el('tr', {}, el('td', {style: 'font-weight:600;white-space:nowrap', text: r.level}), el('td', {text: r.rule}))))),
      el('p', {class: 'sub', style: 'margin:10px 0 0', text: 'A week after a change goes live, it checks whether daily profit moved ' + Math.round(RU.verdict_pct * 100) + '%+ and marks it helped or hurt.'})),
    el('div', {class: 'panel'}, el('h3', {text: 'Where AI (Claude) comes in'}),
      el('p', {class: 'sub', text: 'Only for writing, never for choosing numbers. The rules run the same with or without an Anthropic key.'}),
      el('ul', {class: 'rules'}, ['Email campaign and wholesale outreach drafts', 'Listing titles and bullets',
        'Summaries of what reviews complain about', 'Checking drafts against your brand voice', 'The written summary at the top of the daily brief']
        .map(t => el('li', {text: t}))),
      el('p', {class: 'sub', style: 'margin:10px 0 0', text: 'These are generic starting numbers, not tuned to your business: set your margins and goals in .env and goals.toml.'})));

  // Agent cards
  const roster = document.getElementById('roster');
  const listen = n => [...new Set(D.handoffs.filter(h => h.to.includes(n)).map(h => PSEUDO[h.from] ? PSEUDO[h.from][1].toLowerCase() : NAME(h.from)))];
  const tell = n => [...new Set(D.handoffs.filter(h => h.from === n).flatMap(h => h.to.map(NAME)))];
  for (const r of D.roster) {
    const hears = listen(r.name), tells = tell(r.name);
    roster.append(el('div', {class: 'role'},
      el('div', {class: 'hd'}, el('span', {class: 'avatar', text: INITIALS(r.name)}),
        el('div', {}, el('div', {class: 'nm', text: r.step + '. ' + NAME(r.name)}), el('div', {class: 'tm', text: D.teams[r.team] || 'Coordinates every team'}))),
      el('p', {text: r.mission}),
      RU.agents[r.name] ? [el('h5', {text: 'How it decides'}), ruleList(RU.agents[r.name])] : null,
      el('h5', {text: 'Can suggest'}),
      el('ul', {}, r.proposes.map(k => el('li', {}, el('span', {text: KIND[k.kind] || k.kind.replace(/_/g, ' ')}),
        el('span', {class: 'by', text: (k.by === 'api' ? 'system, once approved' : 'a person') + (k.effort ? ' · ' + (EFFORT_LABEL[k.effort] || k.effort).toLowerCase() : '')})))),
      r.needs.length ? [el('h5', {text: 'Needs data'}), el('div', {class: 'links', text: r.needs.map(t => TABLE[t] || t).join(', ')})] : null,
      hears.length || tells.length ? el('h5', {text: 'Works with'}) : null,
      hears.length ? el('div', {class: 'links'}, el('b', {text: 'Hears from: '}), hears.join(', ')) : null,
      tells.length ? el('div', {class: 'links'}, el('b', {text: 'Tells: '}), tells.join(', ')) : null));
  }

  // Market tab: the public market tracker, loaded on first open.
  const M = D.market;
  document.getElementById('market-lead').textContent = M && !M.error
    ? 'What customers and competitors show in public data (dagacci.com catalog and stock, Judge.me reviews, Amazon pages, Cozyfit and 7 other scrub brands), updated daily. Data from ' + M.date + '.'
    : 'The market tracker hasn\u2019t run yet. Run amz-agents market to build it.';
  let framed = false;
  const loadMarket = () => {
    if (framed || !M || M.error) return;
    framed = true;
    document.getElementById('market-box').append(el('iframe', {class: 'mframe', src: D.market_src, title: 'Market tracker'}),
      el('p', {class: 'note'}, el('a', {href: D.market_src, target: '_blank', rel: 'noopener', text: 'Open it full screen'})));
  };

  // Mystery shopper
  const SH = D.shopper;
  const siteCard = (title, score, link, linkText, summary, r, me) => el('div', {class: 'site' + (me ? ' me' : '')},
    el('div', {class: 'hd'}, el('span', {class: 'nm', text: title}), el('span', {class: 'sc', text: score || ''})),
    link && /^https:\/\//.test(link) ? el('a', {class: 'pr', href: link, target: '_blank', rel: 'noopener', text: (linkText || '').slice(0, 80)})
      : el('div', {class: 'pr', text: linkText || ''}),
    summary ? el('p', {text: summary}) : null,
    r.likes && r.likes.length ? [el('h5', {text: 'Likes'}), el('ul', {class: 'like'}, r.likes.map(t => el('li', {text: t})))] : null,
    r.dislikes && r.dislikes.length ? [el('h5', {text: 'Dislikes'}), el('ul', {class: 'dislike'}, r.dislikes.map(t => el('li', {text: t})))] : null,
    r.change && r.change.length ? [el('h5', {text: 'Would change'}), el('ul', {class: 'chg'}, r.change.map(t => el('li', {text: t})))] : null);
  if (SH && SH.live) {
    const L = SH.live, box = document.getElementById('live');
    box.append(el('div', {class: 'verdict', style: 'margin-top:20px'},
      el('div', {class: 'k', text: 'Today\u2019s verdict \u00b7 live walkthrough, ' + L.date}),
      el('div', {class: 'v', text: 'I\u2019d buy ' + L.verdict.choice + ' on ' + L.verdict.where}),
      el('p', {text: L.verdict.why})),
      el('p', {class: 'note', text: L.method}),
      el('div', {class: 'sec-head'}, el('h2', {text: 'What I saw, site by site'}), el('p', {text: 'Shopping by hand in a real browser, Walmart included.'})),
      el('section', {class: 'sites'}, L.sites.map(x => siteCard(x.brand + ' \u00b7 ' + x.where, x.price, x.url, x.url ? x.url.replace(/^https:\/\/(www\.)?/, '').slice(0, 60) : '', '', x, x.brand === (SH.self || 'Dagacci')))));
    document.getElementById('auto-head').hidden = false;
  }
  if (!SH) document.getElementById('shop-lead').textContent = 'No mystery shop yet. Run amz-agents shop (the daily market workflow does it weekly).';
  else {
    const where = v => v.site === 'amazon' ? 'Amazon' : 'own site';
    const isMe = v => v.brand === SH.self;
    document.getElementById('shop-lead').textContent = 'The shopper is ' + SH.persona + '. Shopped ' + SH.date
      + '. ' + (SH.voice === 'claude' ? 'The write-up is the shopper\u2019s own words (Claude), from the checks below.'
        : 'The write-up is built from the checks below; add an Anthropic key for the shopper\u2019s own words.');
    const V = SH.verdict, rank = (V.ranking || []).findIndex(r => r.brand === SH.self);
    document.getElementById('shop-verdict').append(el('div', {class: 'verdict'},
      el('div', {class: 'k', text: 'If I were buying scrubs today'}),
      el('div', {class: 'v', text: V.choice ? V.choice + (V.site === 'amazon' ? ' on Amazon' : ' on its own site') : 'No pick'}),
      el('p', {text: V.why || ''}),
      rank >= 0 ? el('p', {style: 'margin-top:8px', text: SH.self + ' ranks #' + (rank + 1) + ' of ' + V.ranking.length + ' sites shopped.'}) : null));
    const visits = [...SH.visits].sort((a, b) => (b.overall ?? -1) - (a.overall ?? -1));
    const mini = x => x == null ? el('span', {class: 'n', text: '\u2014'}) : el('div', {class: 'mini'},
      el('div', {class: 't'}, el('i', {style: 'width:' + x * 10 + '%'})), el('span', {text: x.toFixed(1)}));
    document.getElementById('shop-board').append(el('table', {class: 'sb'},
      el('thead', {}, el('tr', {}, el('th', {text: 'Site'}), el('th', {class: 'num', text: 'Overall'}),
        SH.categories.map(c => el('th', {class: 'hide-sm', text: c})), el('th', {class: 'num', text: 'Set price'}),
        el('th', {class: 'num hide-sm', text: 'Page'}))),
      el('tbody', {}, visits.map(v => el('tr', {class: isMe(v) ? 'me' : ''},
        el('td', {}, el('b', {text: v.brand}), ' \u00b7 ' + where(v)),
        el('td', {class: 'num', style: 'font-weight:650', text: v.overall == null ? '\u2014' : v.overall.toFixed(1)}),
        SH.categories.map(c => el('td', {class: 'hide-sm'}, mini(v.scores ? v.scores[c] : null))),
        el('td', {class: 'num', text: usd(v.site === 'amazon' ? v.median_price : v.outfit)}),
        el('td', {class: 'num hide-sm', text: v.page_kb ? (v.page_kb / 1024).toFixed(1) + ' MB' : '\u2014'}))))));
    document.getElementById('shop-sites-note').textContent = 'Likes, dislikes, and what the shopper would change.';
    const sites = document.getElementById('shop-sites');
    for (const v of [...visits].sort((a, b) => isMe(b) - isMe(a))) {
      const r = v.review || {}, p = v.product || {};
      sites.append(el('div', {class: 'site' + (isMe(v) ? ' me' : '')},
        el('div', {class: 'hd'}, el('span', {class: 'nm', text: v.brand + ' \u00b7 ' + where(v)}),
          el('span', {class: 'sc', text: v.overall == null ? '' : v.overall.toFixed(1) + '/10'})),
        p.url && /^https:\/\//.test(p.url) ? el('a', {class: 'pr', href: p.url, target: '_blank', rel: 'noopener', text: (p.title || '').slice(0, 80)})
          : el('div', {class: 'pr', text: p.title || ''}),
        el('p', {text: r.summary || ''}),
        r.likes && r.likes.length ? [el('h5', {text: 'Likes'}), el('ul', {class: 'like'}, r.likes.map(t => el('li', {text: t})))] : null,
        r.dislikes && r.dislikes.length ? [el('h5', {text: 'Dislikes'}), el('ul', {class: 'dislike'}, r.dislikes.map(t => el('li', {text: t})))] : null,
        r.change && r.change.length ? [el('h5', {text: 'Would change'}), el('ul', {class: 'chg'}, r.change.map(t => el('li', {text: t})))] : null));
    }
    const stores = visits.filter(v => v.site === 'store' && !v.error).sort((a, b) => isMe(b) - isMe(a));
    document.getElementById('shop-checks').append(el('table', {class: 'cl'},
      el('thead', {}, el('tr', {}, el('th', {text: 'What a shopper looks for'}), stores.map(v => el('th', {class: isMe(v) ? 'me' : '', text: v.brand})))),
      el('tbody', {}, SH.checks.map(c => el('tr', {}, el('td', {text: c.label}),
        stores.map(v => el('td', {class: (isMe(v) ? 'me ' : '') + (v.found[c.key] ? 'y' : 'n'), title: v.found[c.key] || 'not found',
          text: v.found[c.key] ? '\u2713' : '\u2013'})))),
        el('tr', {}, el('td', {text: 'Product page weight'}), stores.map(v => el('td', {class: isMe(v) ? 'me' : '', text: (v.page_kb / 1024).toFixed(1) + ' MB'}))),
        el('tr', {}, el('td', {text: 'Return window'}), stores.map(v => el('td', {class: isMe(v) ? 'me' : '', text: v.return_days ? v.return_days + ' days' : '\u2014'}))),
        el('tr', {}, el('td', {text: 'Free shipping over'}), stores.map(v => el('td', {class: isMe(v) ? 'me' : '', text: v.free_ship_over ? '$' + v.free_ship_over : '\u2014'}))))));
    document.getElementById('shop-foot').textContent = 'Not visited: ' + (SH.not_visited || []).map(n => n.where + ' (' + n.why + ')').join('; ')
      + ' Amazon listings are judged from the market tracker\u2019s reads, not visited again. Checks read page text, so something shown only in an image counts as missing.';
  }

  // Amazon ads (Quartile export)
  const A = D.ads;
  const pc = v => v == null ? '\u2014' : (v * 100).toFixed(1) + '%';
  const bar = (v, label) => v == null ? el('span', {class: 'n', text: '\u2014'}) : el('div', {class: 'bar'},
    el('div', {class: 't'}, el('i', {style: 'width:' + Math.min(100, v * 100).toFixed(1) + '%'})), el('span', {text: label || pc(v)}));
  const hbars = (id, rows, key) => {
    const max = Math.max(...rows.map(r => r.sales)), sum = rows.reduce((t, r) => t + r.sales, 0);
    document.getElementById(id).append(...rows.map(r => {
      const row = el('div', {class: 'row'}, el('span', {class: 'lab', text: r[key], title: r[key]}),
        el('div', {class: 't'}, el('i', {style: 'width:' + (r.sales / max * 100).toFixed(1) + '%'})),
        el('span', {class: 'v', text: usd(r.sales)}));
      row.addEventListener('mousemove', e => showTip(e, r[key] + ': ' + usd(r.sales) + ' \u00b7 ' + pc(r.sales / sum) + ' of these'));
      row.addEventListener('mouseleave', hideTip);
      return row;
    }));
  };
  const asinTable = rows => el('table', {},
    el('thead', {}, el('tr', {}, el('th', {text: 'Product'}), el('th', {class: 'num', text: 'Sales'}),
      el('th', {class: 'num', text: 'Ad spend'}), el('th', {class: 'num', text: 'Ad sales'}),
      el('th', {class: 'num', text: 'ACoS'}), el('th', {class: 'num hide-sm', text: 'TACoS'}))),
    el('tbody', {}, rows.map(r => el('tr', {},
      el('td', {}, el('a', {href: 'https://www.amazon.com/dp/' + r.asin, target: '_blank', rel: 'noopener', text: r.asin}),
        el('div', {class: 'sty', style: 'font-size:12px;color:var(--text-3)', title: r.name, text: r.name})),
      el('td', {class: 'num', text: usd(r.sales)}), el('td', {class: 'num', text: usd(r.spend)}),
      el('td', {class: 'num', text: usd(r.ad_sales)}), el('td', {class: 'num', text: pc(r.acos)}),
      el('td', {class: 'num hide-sm', text: pc(r.tacos)})))));
  if (!A) {
    document.getElementById('ads-lead').textContent = 'No ad data yet. In portal.quartile.com: Reports \u2192 Product Performance \u2192 Amazon Products \u2192 ASIN Analysis, then \u22ef on the ASIN Performance table \u2192 Export data. Load it with amz-agents quartile FILE.';
  } else {
    const T = A.totals;
    document.getElementById('ads-lead').textContent = 'From ' + A.source + '\u2019s ASIN Analysis export: ' + day(A.start) + ' \u2013 ' + day(A.end)
      + ' (' + A.days + ' days), ' + A.ad_types + ', ' + fmt(T.asins) + ' products. Totals for the period, not daily: daily figures come once Seller Central is connected.';
    document.getElementById('ads-kpis').append(
      stile('Amazon sales', usd(T.sales), null, '\u2248 ' + usd(T.monthly) + ' a month'),
      stile('Ad spend', usd(T.spend), null, usd(T.spend * 30 / A.days) + ' a month'),
      stile('ACoS', pc(T.acos), null, 'ad spend \u00f7 ad sales \u00b7 ROAS ' + (T.roas || 0).toFixed(1)),
      stile('TACoS', pc(T.tacos), null, 'ad spend \u00f7 all sales'),
      stile('Sales from ads', pc(T.ad_sales / T.sales), null, usd(T.ad_sales) + ' credited to ads'),
      stile('Products selling', fmt(T.selling), null, 'of ' + fmt(T.asins) + ' \u00b7 top 10 = ' + pc(T.top10_share) + ' of sales'));
    const WHO = {risk: 'Risk', listing: 'Listing work', ask_agency: 'Ask Quartile', catalog: 'Catalog', plan: 'CEO plan'};
    document.getElementById('ads-finds').append(...A.findings.map(f => el('div', {class: 'find ' + f.kind},
      el('div', {class: 'hd'}, el('span', {class: 'ttl', text: f.title}), el('span', {class: 'tag', text: WHO[f.kind] || f.kind})),
      f.see ? el('p', {text: f.see}) : null,
      el('p', {class: 'do', text: f.do}),
      el('p', {text: 'Owner: ' + f.who + (f.gain ? ' \u00b7 est. ' + usd(f.gain) + '/mo' : '')}),
      f.items && f.items.some(a => A.asins[a]) ? el('details', {}, el('summary', {text: 'Show ' + plural(f.items.filter(a => A.asins[a]).length, 'product')}),
        el('div', {class: 'table-wrap'}, asinTable(f.items.map(a => A.asins[a]).filter(Boolean)))) : null)));
    document.getElementById('ads-styles').append(el('table', {},
      el('thead', {}, el('tr', {}, el('th', {text: 'Style'}), el('th', {class: 'num', text: 'Sales'}),
        el('th', {text: 'Share of sales'}), el('th', {text: 'From ads'}), el('th', {class: 'num', text: 'Ad spend'}),
        el('th', {class: 'num', text: 'ACoS'}), el('th', {class: 'num hide-sm', text: 'Variants selling'}))),
      el('tbody', {}, A.styles.filter(x => x.sales >= 1000).map(x => el('tr', {},
        el('td', {}, el('div', {class: 'sty', title: x.name, text: x.name}), el('div', {style: 'font-size:12px;color:var(--text-3)', text: x.parent})),
        el('td', {class: 'num', text: usd(x.sales)}), el('td', {}, bar(x.share)), el('td', {}, bar(x.ad_share)),
        el('td', {class: 'num', text: usd(x.spend)}), el('td', {class: 'num', text: pc(x.acos)}),
        el('td', {class: 'num hide-sm', text: x.selling + ' of ' + x.asins}))))));
    const small = A.styles.filter(x => x.sales < 1000);
    if (small.length) document.getElementById('ads-styles').append(el('p', {class: 'note', style: 'padding:0 12px 10px',
      text: plural(small.length, 'more style') + ' sold under $1,000 each (' + usd(small.reduce((t, x) => t + x.sales, 0)) + ' together).'}));
    hbars('ads-sizes', A.sizes, 'size');
    hbars('ads-colors', A.colors, 'color');
    document.getElementById('ads-top-note').textContent = 'By sales. Links open the Amazon page.';
    document.getElementById('ads-top').append(asinTable(A.top));
    document.getElementById('ads-foot').textContent = 'Ad sales can be higher than a product\u2019s own sales: Amazon credits an ad with other products bought after the click. '
      + 'Sizes and colors are read from product names; ' + (A.ad_types === 'Sponsored Products' ? 'Sponsored Brands and Display aren\u2019t in this export, so real ad spend is higher. ' : '')
      + 'Load a new export with amz-agents quartile FILE.';
  }

  // Tabs (remembered in the URL hash so a link opens the right one)
  const tabs = {work: ['t-work', 'tab-work'], ads: ['t-ads', 'tab-ads'], market: ['t-market', 'tab-market'], shop: ['t-shop', 'tab-shop'], how: ['t-how', 'tab-how']};
  const setTab = t => {
    for (const [k, [b, p]] of Object.entries(tabs)) {
      document.getElementById(b).setAttribute('aria-selected', String(k === t));
      document.getElementById(p).hidden = k !== t;
    }
    if (t === 'market') loadMarket();
    if (location.hash.slice(1) !== t) history.replaceState(null, '', t === 'work' ? location.pathname + location.search : '#' + t);
  };
  for (const t of Object.keys(tabs)) document.getElementById('t-' + t).onclick = () => setTab(t);
  setTab(tabs[location.hash.slice(1)] ? location.hash.slice(1) : 'work');

  document.getElementById('foot').textContent = 'Covers the last ' + D.window_days + ' days plus anything still open. '
    + (D.token ? 'Approve / reject / mark done are on.' : 'Read-only. To approve here: amz-agents dashboard --serve --allow-actions');
})();
</script>
</body>
</html>
"""
