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
import secrets
from collections import Counter
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from .actions import HUMAN_ONLY, Action, ActionLog
from .agents import ads, listing, pricing, reviews, sales, ux
from .agents import chief_of_staff as cos
from .agents import creative as cr
from .agents import data as da
from .agents import inventory as inv
from .agents import marketing as mk
from .agents.base import EFFORT_HOURS, TEAMS
from .agents.chief_of_staff import ChiefOfStaff, _describe
from .config import Guardrails
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


def _agents() -> list[tuple[str, Any]]:
    from .orchestrator import DEFAULT_AGENTS

    return [(a.name, a.role) for a in DEFAULT_AGENTS]


def collect(wh: Warehouse, today: date | None = None,
            guardrails: Guardrails | None = None) -> dict[str, Any]:
    """Everything the page shows, as plain JSON-able data."""
    today = today or date.today()
    run = (wh.query("SELECT * FROM runs ORDER BY id DESC LIMIT 1") or [None])[0]
    health = {r["agent"]: r for r in wh.query(
        "SELECT * FROM agent_runs WHERE run_id=?", (run["id"],))} if run else {}
    last_data = wh.query("SELECT MAX(date) AS d FROM metrics_daily")[0]["d"]

    rows = wh.query(
        "SELECT * FROM actions WHERE created >= date(?, ?) "
        "OR status IN ('pending_approval', 'approved', 'acknowledged') ORDER BY id DESC",
        (today.isoformat(), f"-{WINDOW_DAYS} days"),
    )
    verdicts = {o["action_id"]: o for o in wh.query("SELECT * FROM outcomes")}
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
        actions.append({
            "id": r["id"], "agent": r["agent"], "team": r["team"] or "", "kind": r["kind"],
            "title": title, "reason": r["reason"] or "", "notes": r["notes"] or "",
            "status": r["status"], "stage": STAGE.get(r["status"], "waiting"),
            "priority": r["priority"] or "", "impact": r["impact_usd"], "effort": r["effort"] or "",
            "roi": r["roi"], "sku": r["sku"] or "", "created": created,
            "age": (today - date.fromisoformat(created)).days,
            "verdict": o["verdict"] if o else None,
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
    }


def render(data: dict[str, Any], token: str | None = None) -> str:
    """The page. `token` turns on the action buttons (server mode only)."""
    payload = json.dumps({**data, "token": token}).replace("</", "<\\/")
    return _PAGE.replace("__DATA__", payload)


def write(wh: Warehouse, out: str | Path = "reports/dashboard.html") -> Path:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(collect(wh)))
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
            if self.path.split("?")[0] not in ("/", "/index.html"):
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
    <button type="button" role="tab" id="t-how" aria-controls="tab-how" aria-selected="false">How the agents work together</button>
  </nav>
  <div id="tab-work" role="tabpanel" aria-labelledby="t-work">
  <p class="headline" id="headline"></p>
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
      <thead><tr><th>Work</th><th>Agent</th><th>Stage</th><th class="hide-sm">Priority</th>
        <th class="num">Est. impact</th><th class="num">Age</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </section>

  <div class="sec-head"><h2>The team</h2><p>Click an agent to see only their work.</p></div>
  <section class="team" id="team" aria-label="Agents"></section>
  <p class="note" id="foot"></p>
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
  const NAME = n => n === 'chief_of_staff' ? 'Chief of staff' : n === 'ux' ? 'UX' : n[0].toUpperCase() + n.slice(1).replace(/_/g, ' ');
  const INITIALS = n => n === 'chief_of_staff' ? 'CS' : n === 'ux' ? 'UX' : n.slice(0, 2).toUpperCase();
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
  const isTodo = a => needsOk(a) || needsDone(a) || a.status === 'failed'
    || (a.status === 'proposed' && (a.priority === 'now' || a.priority === 'high'));

  // Header
  const meta = document.getElementById('meta');
  if (D.run) meta.append(el('span', {text: 'Last run ' + D.run.finished + ' UTC'}),
    el('span', {class: 'chip', text: {recommend: 'Suggest only', approve: 'Approve first', auto: 'Auto within limits'}[D.run.mode] || D.run.mode}));
  else meta.append(el('span', {text: 'The agents haven’t run yet'}));
  meta.append(el('span', {text: 'Sales data through ' + (D.data_through || 'n/a')}));

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
      acts.length ? el('div', {class: 'acts'}, acts) : null,
      el('dl', {}, row('Agent', NAME(a.agent)), row('Team', D.teams[a.team] || ''), row('Est. impact', a.impact == null ? '' : usd(a.impact) + ' over 30 days'),
        row('Effort', a.effort ? EFFORT_LABEL[a.effort] || a.effort : ''), row('Return', a.roi == null ? '' : a.roi + '× the time it takes'),
        row('Product', a.sku), row('Raised', a.created + ' (' + age(a.age) + (a.age ? ' ago' : '') + ')'), row('Item', '#' + a.id + ' · ' + a.kind.replace(/_/g, ' '))),
      help].filter(Boolean));
    shell.classList.add('open'); drawer.setAttribute('aria-hidden', 'false');
    drawer.querySelector('.x').focus();
  }

  // Inbox: what needs a person now, plus agents that are missing data.
  const INBOX_MAX = 6;
  const inbox = document.getElementById('inbox');
  function renderInbox() {
    inbox.replaceChildren();
    if (!todos.length && !sick.length) { inbox.append(el('div', {class: 'caught-up', text: D.run ? 'You’re all caught up. Nothing needs you right now.' : 'Nothing yet.'})); return; }
    for (const a of todos.slice(0, INBOX_MAX)) {
      const what = needsOk(a) ? 'Approve or reject' : needsDone(a) ? 'Do it, then mark done' : a.status === 'failed' ? 'Failed: take a look' : 'Worth acting on';
      inbox.append(el('div', {class: 'todo p-' + (a.priority || 'none')},
        el('span', {class: 'avatar', title: NAME(a.agent), text: INITIALS(a.agent)}),
        el('button', {type: 'button', class: 't', text: a.title, onclick: () => open(a)}),
        el('div', {class: 'acts'}, buttons(a).length ? buttons(a) : el('button', {type: 'button', class: 'btn', text: 'Details', onclick: () => open(a)})),
        el('div', {class: 's'}, what + ' · ' + NAME(a.agent) + (a.impact > 0 ? ' · ' + usd(a.impact) : '') + ' · ',
          el('span', {class: a.age >= D.stale_days ? 'stale' : '', text: a.age ? 'waiting ' + age(a.age) : 'new today'}))));
    }
    if (todos.length > INBOX_MAX) inbox.append(el('div', {class: 'more', text: '+ ' + (todos.length - INBOX_MAX) + ' more in To review below'}));
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
      el('div', {class: 'who'}, el('span', {text: NAME(a.agent) + ' · ' + (a.col === 'review' ? (a.age ? 'waiting ' + age(a.age) : 'new') : age(a.age) + (a.age ? ' ago' : ''))}),
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
        el('td', {text: NAME(a.agent)}),
        el('td', {}, el('span', {class: 'chip'}, el('i', {class: 'dot', style: 'background:' + COL_COLOR[a.col]}), label)),
        el('td', {class: 'hide-sm', text: PRI_LABEL[a.priority] || '—'}),
        el('td', {class: 'num', text: usd(a.impact)}),
        el('td', {class: 'num' + (a.col === 'review' && a.age >= D.stale_days ? ' stale' : ''), text: age(a.age)})));
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
    opportunity: 'Raise an opportunity for another agent', escalation: 'Escalate a decision to you'};
  const TABLE = {products: 'products & costs', metrics_daily: 'daily sales', reviews: 'reviews', keywords: 'ad keywords',
    search_terms: 'search terms', external_campaigns: 'Meta / Google / TikTok campaigns', channel_listings: 'other-channel listings',
    leads: 'wholesale leads', creatives: 'ad creative results', listing_assets: 'Amazon images & A+',
    web_funnel: 'store funnel (GA4)', web_pages: 'page speed', asin_traffic: 'Amazon page traffic'};
  const R = Object.fromEntries(D.roster.map(r => [r.name, r]));
  const chip = n => el('span', {class: 'ag'}, el('span', {class: 'avatar', text: n === 'everyone' ? '··' : INITIALS(n)}),
    n === 'everyone' ? 'Every agent' : NAME(n), R[n] ? el('span', {class: 'n', text: R[n].step}) : null);

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
  const listen = n => [...new Set(D.handoffs.filter(h => h.to.includes(n)).map(h => h.from === 'everyone' ? 'every agent' : NAME(h.from)))];
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

  // Tabs (remembered in the URL hash so a link opens the right one)
  const tabs = {work: ['t-work', 'tab-work'], how: ['t-how', 'tab-how']};
  const setTab = t => {
    for (const [k, [b, p]] of Object.entries(tabs)) {
      document.getElementById(b).setAttribute('aria-selected', String(k === t));
      document.getElementById(p).hidden = k !== t;
    }
    if (location.hash.slice(1) !== t) history.replaceState(null, '', t === 'work' ? location.pathname + location.search : '#how');
  };
  document.getElementById('t-work').onclick = () => setTab('work');
  document.getElementById('t-how').onclick = () => setTab('how');
  setTab(location.hash === '#how' ? 'how' : 'work');

  document.getElementById('foot').textContent = 'Covers the last ' + D.window_days + ' days plus anything still open. '
    + (D.token ? 'Approve / reject / mark done are on.' : 'Read-only. To approve here: amz-agents dashboard --serve --allow-actions');
})();
</script>
</body>
</html>
"""
