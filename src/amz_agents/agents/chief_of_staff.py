"""Chief of staff: turns the agents' proposals into a plan a busy person can act on.

It doesn't propose changes of its own. It knows each agent's role (mission, team,
the action types it proposes and their level of effort, the data it needs) and:
  1. tracks progress against the company's goals (goals.toml),
  2. estimates the 30-day profit impact, effort and ROI of every proposed action
     (and every data-agent opportunity), and ranks them by priority,
  3. tags each action with the goal it serves or works against,
  4. escalates what needs a human: goal conflicts and sudden sales/spend anomalies,
  5. checks whether executed actions actually helped, and says what to tighten or trust,
  6. checks on the agents: failures, missing data, undeclared action types,
  7. writes the brief, with a section per team (and a brief per team to email).

Estimates are deliberately simple and their assumptions are stated, so a
person can see why something ranked where it did. Use them to rank, not forecast.
"""

from __future__ import annotations

import math
import os
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .. import llm
from ..actions import HUMAN_ONLY, Action
from .base import EFFORT_HOURS, TEAMS, Agent, Context, Role, days_of_cover

# Assumptions behind the impact estimates (tune with the company's own data).
PRICE_ELASTICITY = -1.5  # % change in units per % change in price
BID_SALES_SENSITIVITY = 0.5  # sales move half as much as bids
HARVEST_UPLIFT = 0.25  # extra profit share from moving a term to exact match
BUDGET_SALES_SENSITIVITY = 0.7  # the next $ of external budget earns 70% of the average $
SAFETY_DAYS = 14

WINDOW = 7  # days, for goal progress and outcome checks
VERDICT_THRESHOLD = 0.03  # +/- change in daily profit that counts as helped/hurt
ANOMALY_DROP = 0.5  # yesterday's units below this share of the 14-day average
ANOMALY_P = 0.02  # ...and that few would happen by chance under 2% of days (Poisson):
# across a 1,000-variant catalog, small sellers have slow days every day.
SPEND_SPIKE = 2.0  # yesterday's ad spend above this multiple of average

# Priority: ROI = 30-day impact / cost of the person's time the action takes.
HOURLY_COST = float(os.getenv("HOURLY_COST", "75"))
HIGH_IMPACT = 500.0  # $ over 30 days
HIGH_ROI = 3.0
MEDIUM_ROI = 1.0
TIER = {"now": 0, "high": 1, "medium": 2, "low": 3, "judgment": 4}
TOP_N = 7

GROW, EFFICIENCY, SHRINK = "grow", "efficiency", "shrink"

# Spend outside Amazon doesn't count toward TACoS (Amazon ad spend / Amazon revenue).
EXTERNAL_KINDS = {"update_campaign_budget", "pause_campaign", "email_campaign",
                  "update_channel_price", "list_on_channel", "send_outreach",
                  "refresh_creative", "pause_creative", "new_creative_variants",
                  "fix_funnel_step", "improve_page_speed", "fix_accessibility",
                  "fix_landing_page"}


def _poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k) for X ~ Poisson(lam)."""
    term = total = math.exp(-lam)
    for i in range(1, int(k) + 1):
        term *= lam / i
        total += term
    return total


@dataclass
class Goal:
    name: str
    metric: str  # units | revenue | tacos
    scope: str  # SKU or "all"
    change: float | None = None
    target: float | None = None

    def covers(self, sku: str | None) -> bool:
        return self.scope == "all" or self.scope == sku


def load_goals(path: str | Path | None = None) -> list[Goal]:
    path = Path(path or os.getenv("GOALS_PATH", "goals.toml"))
    if not path.exists():
        return []
    with path.open("rb") as f:
        return [Goal(**g) for g in tomllib.load(f).get("goal", [])]


class ChiefOfStaff:
    name = "chief_of_staff"

    def __init__(self, ctx: Context, goals: list[Goal] | None = None, today: date | None = None,
                 agents: list[Agent] | None = None):
        self.ctx = ctx
        self.goals = goals if goals is not None else load_goals()
        self.today = today or date.today()
        self.products = {p["sku"]: p for p in ctx.wh.query("SELECT * FROM products")}
        self.roster: dict[str, Role] = {a.name: a.role for a in agents or []}
        self.health: dict[str, dict[str, Any]] = {}  # filled in by the orchestrator

    # ------------------------------------------------------------------ review
    def review(self, actions: list[Action]) -> list[Action]:
        """Attach SKU, impact, confidence, goal, owning team, effort, ROI and priority."""
        for a in actions:
            a.sku = a.sku or self._sku_for(a)
            a.impact_usd, a.confidence, note = self._estimate(a)
            if note:
                a.notes = "; ".join(x for x in (a.notes, note) if x)
            a.goal = self._goal_tag(a)
            a.team = self._team(a)
            a.effort = self._effort(a)
            a.roi, a.priority = self._priority(a)
        return actions

    def plan(self, actions: list[Action]) -> list[Action]:
        """Add escalations, then order everything by priority for the person approving."""
        self.progress = self.goal_progress()
        escalations = self.conflicts(actions) + self.anomalies()
        for e in escalations:
            e.team, e.priority = "amazon_ops", "now"

        def key(a: Action) -> tuple:
            lead = a.after.get("est_annual_value") if isinstance(a.after, dict) else None
            return (TIER.get(a.priority, 9), -(a.impact_usd or 0), -(lead or 0))

        return escalations + sorted(actions, key=key)

    # ------------------------------------------------------- roles and priority
    def _team(self, a: Action) -> str:
        if a.kind == "opportunity":
            owner = self.roster.get(a.after.get("owner", ""))
            return owner.team if owner else "data"
        role = self.roster.get(a.agent)
        return role.team if role else ""

    def _effort(self, a: Action) -> str:
        if a.kind == "opportunity":
            return a.after.get("effort", "")
        role = self.roster.get(a.agent)
        effort = role.proposes.get(a.kind, "") if role else ""
        if effort and self.ctx.settings.mode.value == "auto" and a.kind not in HUMAN_ONLY:
            return "XS"  # the system carries it out
        return effort

    def _priority(self, a: Action) -> tuple[float | None, str]:
        if a.impact_usd is None:
            # No dollar estimate, but the market tracker says it can't wait.
            urgent = isinstance(a.after, dict) and a.after.get("when") == 1
            return None, "now" if urgent else "judgment"
        hours = EFFORT_HOURS.get(a.effort)
        roi = round(a.impact_usd / (hours * HOURLY_COST), 1) if hours else None
        if a.impact_usd <= 0:
            return roi, "low"
        if a.impact_usd >= HIGH_IMPACT and (roi is None or roi >= HIGH_ROI):
            return roi, "high"
        if roi is None or roi >= MEDIUM_ROI:
            return roi, "medium"
        return roi, "low"

    def agent_health(self) -> list[dict[str, Any]]:
        """Per agent: did it run, does it have its data, did it stay in its lane."""
        out = []
        last = self.ctx.wh.query("SELECT MAX(date) AS d FROM metrics_daily")[0]["d"]
        stale = not last or date.fromisoformat(last) < self.today - timedelta(days=3)
        for name, role in self.roster.items():
            h = self.health.get(name, {})
            problems = []
            if h.get("error"):
                problems.append(f"failed: {h['error']}")
            for table in role.needs:
                if not self.ctx.wh.query(f"SELECT 1 FROM {table} LIMIT 1"):
                    problems.append(f"no data in {table}")
                elif table == "metrics_daily" and stale:
                    problems.append(f"metrics_daily stale (last {last})")
            if h.get("undeclared"):
                problems.append(f"proposed undeclared types: {', '.join(sorted(h['undeclared']))}")
            out.append({"agent": name, "role": role, "proposed": h.get("proposed", 0),
                        "problems": problems})
        return out

    # --------------------------------------------------------------- estimates
    def _estimate(self, a: Action) -> tuple[float | None, str, str]:
        p = self.products.get(a.sku or "")
        if p and None in (p["price"], p["landed_cost"], p["fees"]):
            p = None  # no cost data: no estimate
        if a.kind == "update_bid" and p:
            k = self.ctx.wh.query("SELECT * FROM keywords WHERE keyword_id=?", (a.target,))
            if not k:
                return None, "", ""
            k, r = k[0], a.after["bid"] / a.before["bid"]
            margin = self._margin_pct(p)
            if r < 1:
                saved = k["spend"] * (1 - r)
                lost = k["sales"] * (1 - r) * BID_SALES_SENSITIVITY
                return round(saved - lost * margin, 2), "medium", ""
            extra_spend = k["spend"] * (r - 1)
            extra_sales = k["sales"] * (r - 1) * BID_SALES_SENSITIVITY
            return round(extra_sales * margin - extra_spend, 2), "medium", ""

        if a.kind == "add_negative":
            campaign, ad_group = a.target.split("/")
            t = self.ctx.wh.query(
                "SELECT SUM(spend) AS spend FROM search_terms "
                "WHERE campaign_id=? AND ad_group_id=? AND lower(search_term)=?",
                (campaign, ad_group, a.after["keyword"]),
            )
            return round(t[0]["spend"] or 0, 2), "high", ""

        if a.kind == "add_keyword" and p:
            t = self.ctx.wh.query(
                "SELECT SUM(sales) AS sales FROM search_terms WHERE sku=? AND lower(search_term)=?",
                (a.sku, a.after["keyword"]),
            )
            return round((t[0]["sales"] or 0) * self._margin_pct(p) * HARVEST_UPLIFT, 2), "low", ""

        if a.kind == "update_price" and p:
            old, new = a.before["price"], a.after["price"]
            cost = p["landed_cost"] + p["fees"]
            stock = p["units_on_hand"] + p["units_inbound"]
            units = p["daily_velocity"] * 30
            units_new = units * (1 + PRICE_ELASTICITY * (new - old) / old)
            impact = min(units_new, stock) * (new - cost) - min(units, stock) * (old - cost)
            note = ""
            if p["buybox_price"] and p["buybox_price"] < old:
                note = "if you've lost the Buy Box, matching likely pays more than estimated"
            return round(impact, 2), "low", note

        # Agents that size their own work (data, creative, UX) put it on the action.
        if isinstance(a.after, dict) and "impact_usd" in a.after:
            return a.after.get("impact_usd"), a.after.get("confidence", "low"), ""

        if a.kind in ("update_campaign_budget", "pause_campaign"):
            c = self.ctx.wh.query("SELECT * FROM external_campaigns WHERE campaign_id=?", (a.target,))
            ppd = a.before.get("profit_per_dollar")
            if not c or ppd is None:
                return None, "", ""
            c = c[0]
            if a.kind == "pause_campaign" and "low stock" in a.reason:
                # The stock sells organically before the reorder lands; the spend is saved.
                return round(c["spend_30d"], 2), "medium", ""
            ratio = 0.0 if a.kind == "pause_campaign" else a.after["daily_budget"] / a.before["daily_budget"]
            d_spend = c["spend_30d"] * (ratio - 1)
            return round(d_spend * (ppd * BUDGET_SALES_SENSITIVITY - 1), 2), "medium", ""

        if a.kind == "update_channel_price" and p:
            ch, _ = a.target.split("/", 1)
            r = self.ctx.wh.query(
                "SELECT * FROM channel_listings WHERE channel=? AND sku=?", (ch, a.sku)
            )
            if not r:
                return None, "", ""
            r = r[0]
            old, new = a.before["price"], a.after["price"]
            cost = p["landed_cost"] + r["fees"]
            units = r["units_30d"] or 0
            units_new = units * (1 + PRICE_ELASTICITY * (new - old) / old)
            impact = units_new * (new - cost) - units * (old - cost)
            note = ("protects the Amazon Buy Box; that benefit isn't in the estimate"
                    if "fair pricing" in a.reason else "")
            return round(impact, 2), "low", note

        if a.kind == "reorder" and p and p["lead_time_days"] is not None:
            gap = max(0.0, p["lead_time_days"] + SAFETY_DAYS - days_of_cover(p))
            at_risk = gap * p["daily_velocity"] * self._unit_profit(p)
            return round(at_risk, 2), "high", f"{gap:.0f} days of sales at risk if not ordered"

        return None, "", ""  # listing copy, defects: judgment calls, not dollar estimates

    # ------------------------------------------------------------------- goals
    def goal_progress(self) -> list[dict[str, Any]]:
        out = []
        for g in self.goals:
            sku = None if g.scope == "all" else g.scope
            cur = self._window(sku, WINDOW, 1)
            if g.metric == "tacos":
                value = cur["ad_spend"] / cur["revenue"] if cur["revenue"] else None
                on_track = value is not None and value <= g.target
                out.append({
                    "goal": g, "target": f"≤ {g.target:.0%}",
                    "actual": f"{value:.1%}" if value is not None else "n/a",
                    "status": "on track" if on_track else "behind", "pct": value,
                })
                continue
            base = self._window(sku, 28, 15)
            b, c = base[g.metric] / max(base["days"], 1), cur[g.metric] / max(cur["days"], 1)
            pct = (c / b - 1) if b else None
            if pct is None:
                status = "no data"
            elif pct >= g.change:
                status = "on track"
            elif pct > 0:
                status = "behind"
            else:
                status = "off track"
            out.append({
                "goal": g, "target": f"+{g.change:.0%}",
                "actual": f"{pct:+.1%}" if pct is not None else "n/a",
                "status": status, "pct": pct,
            })
        return out

    def _goal_tag(self, a: Action) -> str:
        d = _direction(a)
        if a.kind in EXTERNAL_KINDS and (a.before or {}).get("destination") != "amazon":
            d = None  # goals are measured on Amazon sales
        for g in self.goals:
            if not g.covers(a.sku) or d is None:
                continue
            if g.metric in ("units", "revenue"):
                if d == GROW:
                    return f"serves: {g.name}"
                if d == SHRINK:
                    return f"works against: {g.name}"
            elif g.metric == "tacos" and a.kind not in EXTERNAL_KINDS:
                if d == EFFICIENCY:
                    return f"serves: {g.name}"
                if a.kind == "update_bid" and d == GROW:
                    return f"works against: {g.name}"
        return ""

    # ------------------------------------------------------------- escalations
    def conflicts(self, actions: list[Action]) -> list[Action]:
        out = []
        low = self.ctx.shared.get("low_stock", set())
        for gp in self.progress:
            g = gp["goal"]
            if g.metric not in ("units", "revenue") or g.scope not in low:
                continue
            p = self.products[g.scope]
            against = [a for a in actions if a.goal == f"works against: {g.name}"]
            out.append(Action(
                agent=self.name, kind="escalation", target=g.scope, sku=g.scope,
                before=None,
                after={"goal": g.name, "progress": gp["actual"], "target": gp["target"]},
                reason=(
                    f"Goal '{g.name}' can't be met on current stock: {days_of_cover(p):.0f} days "
                    f"left vs {p['lead_time_days']}-day lead time. "
                    + (f"Proposed to stretch stock, against the goal: "
                       f"{'; '.join(_describe(a) for a in against)}. " if against else "")
                    + "Decide: expedite inventory, or pause the goal until stock lands."
                ),
                confidence="high",
            ))
        return out

    def anomalies(self) -> list[Action]:
        out = []
        yesterday = (self.today - timedelta(days=1)).isoformat()
        for sku, p in self.products.items():
            y = self.ctx.wh.query(
                "SELECT * FROM metrics_daily WHERE sku=? AND date=?", (sku, yesterday)
            )
            if not y:
                continue
            y, hist = y[0], self._window(sku, 15, 2)
            if not hist["days"]:
                continue
            avg_units = hist["units"] / hist["days"]
            avg_spend = hist["ad_spend"] / hist["days"]

            if (avg_units >= 3 and y["units"] < avg_units * ANOMALY_DROP
                    and _poisson_cdf(y["units"], avg_units) < ANOMALY_P):
                causes = []
                if p["units_on_hand"] <= 0:
                    causes.append("out of stock")
                else:
                    causes.append(f"not a stockout ({p['units_on_hand']} on hand)")
                    if p["buybox_price"] and p["buybox_price"] < p["price"]:
                        causes.append(
                            f"likely lost the Buy Box: competitor at {p['buybox_price']:.2f} "
                            f"vs your {p['price']:.2f} (see the pricing action for this SKU)"
                        )
                    causes.append("check the listing isn't suppressed in Seller Central")
                causes[0] = causes[0][0].upper() + causes[0][1:]
                out.append(Action(
                    agent=self.name, kind="escalation", target=sku, sku=sku, before=None,
                    after={"units_yesterday": y["units"], "avg_units": round(avg_units, 1)},
                    reason=(
                        f"{sku} sold {y['units']} yesterday vs {avg_units:.1f}/day average. "
                        + "; ".join(causes) + "."
                    ),
                    confidence="high",
                ))
            if avg_spend > 0 and y["ad_spend"] > avg_spend * SPEND_SPIKE:
                out.append(Action(
                    agent=self.name, kind="escalation", target=sku, sku=sku, before=None,
                    after={"ad_spend_yesterday": y["ad_spend"], "avg_spend": round(avg_spend, 2)},
                    reason=(
                        f"{sku} ad spend was ${y['ad_spend']:.2f} yesterday vs "
                        f"${avg_spend:.2f}/day average. Check for a runaway campaign or bid change."
                    ),
                    confidence="high",
                ))
        return out

    # ---------------------------------------------------------------- outcomes
    def evaluate_outcomes(self) -> list[dict[str, Any]]:
        """Before/after daily profit for executed actions that have had a week to land."""
        cutoff = (self.today - timedelta(days=WINDOW + 1)).isoformat()
        rows = self.ctx.wh.query(
            "SELECT a.* FROM actions a LEFT JOIN outcomes o ON o.action_id = a.id "
            "WHERE a.status='executed' AND a.executed_at <= ? AND a.sku IS NOT NULL "
            "AND o.action_id IS NULL",
            (cutoff,),
        )
        out = []
        for a in rows:
            p = self.products.get(a["sku"])
            if not p or p["landed_cost"] is None or p["fees"] is None:
                continue
            ex = date.fromisoformat(a["executed_at"][:10])
            before = self._profit(a["sku"], ex - timedelta(days=WINDOW), ex - timedelta(days=1), p)
            after = self._profit(a["sku"], ex + timedelta(days=1), ex + timedelta(days=WINDOW), p)
            if before is None or after is None:
                continue
            change = (after - before) / abs(before) if before else 0
            verdict = (
                "helped" if change > VERDICT_THRESHOLD
                else "hurt" if change < -VERDICT_THRESHOLD
                else "neutral"
            )
            self.ctx.wh.execute(
                "INSERT INTO outcomes (action_id, metric, before, after, verdict) VALUES (?,?,?,?,?)",
                (a["id"], "daily_profit", round(before, 2), round(after, 2), verdict),
            )
            out.append({**a, "profit_before": before, "profit_after": after, "verdict": verdict})
        return out

    def lessons(self) -> list[str]:
        """What the track record says about how much to trust each action type."""
        rows = self.ctx.wh.query(
            "SELECT a.kind, o.verdict FROM outcomes o JOIN actions a ON a.id = o.action_id"
        )
        by_kind: dict[str, list[str]] = defaultdict(list)
        for r in rows:
            by_kind[r["kind"]].append(r["verdict"])
        out = []
        for kind, verdicts in sorted(by_kind.items()):
            n, hurt, helped = len(verdicts), verdicts.count("hurt"), verdicts.count("helped")
            if n >= 3 and hurt / n >= 0.5:
                out.append(f"`{kind}` hurt {hurt} of {n} times: tighten its guardrails "
                           "or keep it in approve mode.")
            elif n >= 5 and helped / n >= 0.8:
                out.append(f"`{kind}` helped {helped} of {n} times: a candidate for auto mode.")
            else:
                out.append(f"`{kind}`: {helped} helped, {hurt} hurt, "
                           f"{n - helped - hurt} neutral so far.")
        return out

    # ------------------------------------------------------------------- brief
    def brief(self, submitted: list[tuple[int, Action]], outcomes: list[dict[str, Any]]) -> str:
        """The full brief for whoever runs the business: what needs a decision, the top
        priorities across teams, the data agent's opportunities, then each team's queue."""
        esc = [(i, a) for i, a in submitted if a.kind == "escalation"]
        acts = [(i, a) for i, a in submitted if a.kind not in ("escalation", "opportunity")]
        opps = [(i, a) for i, a in submitted if a.kind == "opportunity"]
        top = [(i, a) for i, a in acts if a.priority in ("high", "medium")][:TOP_N]
        ids = self._ids(submitted)

        L = [f"# Chief of staff brief: {self.today.isoformat()}", "",
             (f"Mode: **{self.ctx.settings.mode.value}** · {len(acts)} proposed actions · "
              f"{len(opps)} opportunities · {len(esc)} escalations"), ""]

        gaps = sorted(s for s, p in self.products.items()
                      if None in (p["landed_cost"], p["lead_time_days"], p["fees"]))
        if gaps:
            L += [(f"_Skipped for missing cost, fee or lead-time data: {', '.join(gaps)}. "
                   "Load them with `amz-agents ingest --costs FILE`._"), ""]

        health = self.agent_health()
        L += ["## Needs your attention", ""]
        L += [f"- **[{i}]** {self._link(a, ids)}" for i, a in esc]
        L += [f"- Agent **{h['agent']}**: {'; '.join(h['problems'])}"
              for h in health if h["problems"]]
        if L[-1] == "":
            L += ["Nothing urgent."]

        if self.progress:
            L += ["", "## Goals", "", "| Goal | Target | Last 7 days vs baseline | Status |",
                  "|---|---|---|---|"]
            L += [f"| {g['goal'].name} | {g['target']} | {g['actual']} | {g['status']} |"
                  for g in self.progress]

        mix = self.channel_mix()
        if len(mix) > 1:
            L += ["", "## Sales by channel (last 30 days)", "", "| Channel | Units | Share |",
                  "|---|---|---|"]
            total = sum(mix.values()) or 1
            L += [f"| {ch} | {u:,.0f} | {u / total:.0%} |" for ch, u in mix.items()]

        L += ["", "## Top priorities across teams", ""]
        if top:
            L += _table(top, team=True, ids=ids)
            if self.ctx.settings.mode.value == "approve":
                approvable = " ".join(str(i) for i, _ in top)
                L += ["", f"Approve all {len(top)}: `amz-agents approve {approvable}`"]
        else:
            L += ["No positive-impact actions today."]

        if opps:
            L += ["", "## Opportunities from the data agent", ""]
            L += self._opportunities(opps, ids, owner=True)

        shown = {i for i, _ in top}
        for team, label in TEAMS.items():
            rest = [(i, a) for i, a in acts if a.team == team and i not in shown]
            if rest:
                L += ["", f"## {label}", ""]
                L += self._team_queue(rest, ids)

        L += ["", "## Did past changes work?", ""]
        if outcomes:
            L += ["| # | Change | Daily profit before → after | Verdict |", "|---|---|---|---|"]
            L += [f"| {o['id']} | {o['kind']} {o['target']} | {_usd(o['profit_before'])} → "
                  f"{_usd(o['profit_after'])} | {o['verdict']} |" for o in outcomes]
            L += ["", "_Before/after comparisons of the affected SKU, not controlled tests._"]
        else:
            L += ["No executed changes old enough to judge yet."]
        lessons = self.lessons()
        if lessons:
            L += [""] + [f"- {x}" for x in lessons]

        if health:
            L += ["", "## Agent health", "", "| Agent | Team | Role | Proposed | Status |",
                  "|---|---|---|---|---|"]
            L += [f"| {h['agent']} | {TEAMS.get(h['role'].team, h['role'].team)} | "
                  f"{h['role'].mission} | {h['proposed']} | "
                  f"{'; '.join(h['problems']) or 'ok'} |" for h in health]

        L += ["", "---", self._footer()]

        text = "\n".join(L)
        roster = "\n".join(f"- {n} ({TEAMS.get(r.team, r.team)}): {r.mission}"
                           for n, r in self.roster.items())
        narrative = llm.complete(
            "You are chief of staff to the head of a consumer brand that sells mainly on "
            "Amazon, plus its own store, other channels and wholesale. These agents report "
            f"to you:\n{roster}\n\nWrite a 4-sentence note to open the brief: the one thing "
            "to decide today, the biggest opportunity, how goals are tracking, and anything "
            "the numbers can't tell you (including where agents' proposals pull against each "
            "other). Plain language. No hype.",
            text, self.ctx.settings.anthropic_model, max_tokens=400,
        )
        if narrative:
            text = text.replace("\n\n## Needs your attention", f"\n\n{narrative.strip()}\n\n"
                                "## Needs your attention", 1)
        return text + "\n"

    def team_brief(self, team: str, submitted: list[tuple[int, Action]]) -> str | None:
        """One team's part: its escalations, its full queue, opportunities it owns, and
        how its agents are doing. None if the team has nothing today."""
        mine = [(i, a) for i, a in submitted if a.team == team]
        if not mine:
            return None
        ids = self._ids(submitted)
        esc = [(i, a) for i, a in mine if a.kind == "escalation"]
        acts = [(i, a) for i, a in mine if a.kind not in ("escalation", "opportunity")]
        opps = [(i, a) for i, a in mine if a.kind == "opportunity"]
        agents = [f"**{n}** ({r.mission})" for n, r in self.roster.items() if r.team == team]

        L = [f"# {TEAMS.get(team, team)} brief: {self.today.isoformat()}", ""]
        if agents:
            L += ["Your agents: " + "; ".join(agents), ""]
        if esc:
            L += ["## Needs your attention", ""]
            L += [f"- **[{i}]** {self._link(a, ids)}" for i, a in esc]
            L += [""]
        if acts:
            L += ["## Your queue, by priority", ""] + self._team_queue(acts, ids) + [""]
        if opps:
            L += ["## Opportunities for your team", ""]
            L += self._opportunities(opps, ids, owner=False) + [""]
        problems = [h for h in self.agent_health()
                    if h["role"].team == team and h["problems"]]
        if problems:
            L += ["## Agent problems", ""]
            L += [f"- **{h['agent']}**: {'; '.join(h['problems'])}" for h in problems] + [""]
        L += ["---", self._footer()]
        return "\n".join(L) + "\n"

    # ------------------------------------------------------------ brief parts
    @staticmethod
    def _ids(submitted: list[tuple[int, Action]]) -> dict[tuple[str, str], int]:
        return {(a.kind, a.target): i for i, a in submitted}

    def _link(self, a: Action, ids: dict[tuple[str, str], int]) -> str:
        ref = "(see the pricing action for this SKU)"
        if ref in a.reason and ("update_price", a.sku) in ids:
            return a.reason.replace(ref, f"(see action [{ids[('update_price', a.sku)]}])")
        return a.reason

    def _team_queue(self, items: list[tuple[int, Action]],
                    ids: dict[tuple[str, str], int] | None = None) -> list[str]:
        ranked = [(i, a) for i, a in items if a.priority in ("high", "medium")]
        negative = [(i, a) for i, a in items if a.impact_usd is not None and a.impact_usd < 0]
        judgment = [(i, a) for i, a in items if a.priority == "judgment"]
        low = [(i, a) for i, a in items if a.priority == "low" and (i, a) not in negative]
        L: list[str] = []
        if ranked:
            L += _table(ranked, ids=ids)
        if low:
            L += ["", "### Low priority", ""]
            L += [f"- [{i}] {_describe(a)}: {_usd(a.impact_usd)}, effort {a.effort}"
                  f"{_roi(a.roi, ', ROI ')}. {_why(a, ids)}" for i, a in low]
        if negative:
            L += ["", "### Consider rejecting", "",
                  "Estimated to cost profit. Approve only if the reason outweighs it.", ""]
            L += [f"- **[{i}]** {_describe(a)}: est. {_usd(a.impact_usd)}. {_why(a, ids)}"
                  for i, a in negative]
        if judgment:
            L += ["", "### Needs your judgment", ""]
            L += [f"- **[{i}]** {_describe(a)} (effort {a.effort or '?'}): {_why(a, ids)}"
                  for i, a in judgment]
        while L and L[0] == "":
            L.pop(0)
        return L

    def _opportunities(self, opps: list[tuple[int, Action]], ids: dict[tuple[str, str], int],
                       owner: bool) -> list[str]:
        L: list[str] = []
        for i, a in opps:
            o = a.after
            who = o["owner"]
            role = self.roster.get(who)
            team = TEAMS.get(role.team, role.team) if role else "unassigned"
            facts = [f"priority {a.priority}"]
            if a.impact_usd is not None:
                facts.append(f"est. {_usd(a.impact_usd)}/30 days")
            facts.append(f"effort {a.effort}")
            if a.roi is not None:
                facts.append(f"ROI {a.roi:g}x")
            if owner:
                facts.insert(0, f"owner: {who} agent → {team}")
            L += [f"- **[{i}] {o['title']}** ({' · '.join(facts)})",
                  f"  {o['finding']} **Recommendation:** {o['recommendation']}"]
            linked = [f"[{ids[tuple(r)]}]" for r in o.get("related", []) if tuple(r) in ids]
            if linked:
                L += [f"  In progress via other agents: {', '.join(linked)}."]
        return L

    @staticmethod
    def _footer() -> str:
        return (f"_Impact estimates assume price elasticity {PRICE_ELASTICITY}, "
                f"sales moving {BID_SALES_SENSITIVITY:.0%} as much as bids, the next dollar of "
                f"external budget earning {BUDGET_SALES_SENSITIVITY:.0%} of the average, "
                "and trailing 30-day data. ROI is 30-day impact over the cost of the "
                f"effort at ${HOURLY_COST:.0f}/hour (XS {EFFORT_HOURS['XS']:g}h, "
                f"S {EFFORT_HOURS['S']:g}h, M {EFFORT_HOURS['M']:g}h, L {EFFORT_HOURS['L']:g}h). "
                "Use them to rank, not to forecast._")

    def channel_mix(self) -> dict[str, float]:
        """Units over the last 30 days by channel: Amazon plus every off-Amazon channel."""
        mix = {"amazon": sum((p["daily_velocity"] or 0) * 30 for p in self.products.values())}
        for r in self.ctx.wh.query(
            "SELECT channel, SUM(units_30d) AS units FROM channel_listings "
            "WHERE listed = 1 GROUP BY channel ORDER BY units DESC"
        ):
            mix[r["channel"]] = r["units"] or 0
        return mix

    # ----------------------------------------------------------------- helpers
    def _sku_for(self, a: Action) -> str | None:
        if a.target in self.products:
            return a.target
        if a.kind == "update_bid":
            r = self.ctx.wh.query("SELECT sku FROM keywords WHERE keyword_id=?", (a.target,))
            return r[0]["sku"] if r else None
        if a.kind == "add_negative":
            campaign, ad_group = a.target.split("/")
            r = self.ctx.wh.query(
                "SELECT sku FROM search_terms WHERE campaign_id=? AND ad_group_id=? LIMIT 1",
                (campaign, ad_group),
            )
            return r[0]["sku"] if r else None
        for sku, p in self.products.items():  # flag_defect targets an ASIN
            if p["asin"] == a.target:
                return sku
        return None

    def _window(self, sku: str | None, start_ago: int, end_ago: int) -> dict[str, float]:
        start = (self.today - timedelta(days=start_ago)).isoformat()
        end = (self.today - timedelta(days=end_ago)).isoformat()
        return self._sum(sku, start, end)

    def _sum(self, sku: str | None, start: str, end: str) -> dict[str, float]:
        # days = days the feed covers, not days this SKU sold: a day with no sales has no row.
        sql = ("SELECT (SELECT COUNT(DISTINCT date) FROM metrics_daily WHERE date BETWEEN ? "
               "AND ?) AS days, COALESCE(SUM(units),0) AS units, "
               "COALESCE(SUM(revenue),0) AS revenue, COALESCE(SUM(ad_spend),0) AS ad_spend, "
               "COALESCE(SUM(ad_sales),0) AS ad_sales FROM metrics_daily WHERE date BETWEEN ? AND ?")
        params: tuple = (start, end, start, end)
        if sku:
            sql += " AND sku=?"
            params += (sku,)
        return self.ctx.wh.query(sql, params)[0]

    def _profit(self, sku: str, start: date, end: date, p: dict) -> float | None:
        w = self._sum(sku, start.isoformat(), end.isoformat())
        if not w["days"]:
            return None
        unit_cost = p["landed_cost"] + p["fees"]
        return (w["revenue"] - w["units"] * unit_cost - w["ad_spend"]) / w["days"]

    @staticmethod
    def _unit_profit(p: dict) -> float:
        return p["price"] - p["landed_cost"] - p["fees"]

    def _margin_pct(self, p: dict) -> float:
        return self._unit_profit(p) / p["price"] if p["price"] else 0.0


def _direction(a: Action) -> str | None:
    if a.kind == "update_bid":
        return GROW if a.after["bid"] > a.before["bid"] else EFFICIENCY
    if a.kind == "update_price":
        return GROW if a.after["price"] < a.before["price"] else SHRINK
    if a.kind == "add_negative":
        return EFFICIENCY
    if a.kind == "update_campaign_budget":
        return GROW if a.after["daily_budget"] > a.before["daily_budget"] else EFFICIENCY
    if a.kind == "pause_campaign":
        return EFFICIENCY
    if a.kind in ("add_keyword", "reorder", "update_listing_copy"):
        return GROW
    return None


def _roi(v: float | None, prefix: str = "") -> str:
    return f"{prefix}{v:g}x" if v is not None else ""


def _why(a: Action, ids: dict[tuple[str, str], int] | None = None) -> str:
    why = a.reason + (f" ({a.notes})" if a.notes else "")
    if isinstance(a.after, dict):
        if a.after.get("recommendation") and a.kind != "opportunity":
            why += f" → {a.after['recommendation']}"
        linked = [f"[{ids[tuple(r)]}]" for r in a.after.get("related") or []
                  if ids and tuple(r) in ids]
        if linked and a.kind != "opportunity":
            why += f" Related: {', '.join(linked)}."
    return why


def _table(items: list[tuple[int, Action]], team: bool = False,
           ids: dict[tuple[str, str], int] | None = None) -> list[str]:
    head = "| # | Action | " + ("Team | " if team else "") +         "Priority | Est. impact | Effort | ROI | Goal | Why |"
    L = [head, "|" + "---|" * (head.count("|") - 1)]
    for i, a in items:
        why = _why(a, ids)
        L.append(f"| {i} | {_describe(a)} | "
                 + (f"{TEAMS.get(a.team, a.team or '—')} | " if team else "")
                 + f"{a.priority} | {_usd(a.impact_usd)} | {a.effort or '?'} | "
                 f"{_roi(a.roi) or '—'} | {a.goal or '—'} | {why} |")
    return L


def _platform(a: Action) -> str:
    p = a.before["platform"]
    return {"meta": "Meta", "google": "Google", "tiktok": "TikTok"}.get(p, p.title())


def _usd(v: float) -> str:
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _describe(a: Action) -> str:
    if a.kind == "update_bid":
        return f"Bid '{a.before.get('keyword', a.target)}' {a.before['bid']:.2f} → {a.after['bid']:.2f}"
    if a.kind == "update_price":
        return f"Price {a.target} {a.before['price']:.2f} → {a.after['price']:.2f}"
    if a.kind == "add_negative":
        return f"Negate '{a.after['keyword']}'"
    if a.kind == "add_keyword":
        return f"Add exact '{a.after['keyword']}' at {a.after['bid']:.2f}"
    if a.kind == "reorder":
        return f"Reorder {a.after['order_units']:,} × {a.target}"
    if a.kind == "update_listing_copy":
        return f"Revise listing {a.target}"
    if a.kind == "flag_defect":
        return f"Defect on {a.sku or a.target}: {a.after['theme']}"
    if a.kind == "update_campaign_budget":
        return (f"{_platform(a)} budget '{a.before['name']}' "
                f"{a.before['daily_budget']:.2f} → {a.after['daily_budget']:.2f}/day")
    if a.kind == "pause_campaign":
        return f"Pause {_platform(a)} campaign '{a.before['name']}'"
    if a.kind == "email_campaign":
        return f"Email customers about {a.target}"
    if a.kind == "update_channel_price":
        return f"Price {a.target} {a.before['price']:.2f} → {a.after['price']:.2f}"
    if a.kind == "list_on_channel":
        return f"List {a.sku} on {a.after['channel']}"
    if a.kind == "send_outreach":
        return f"Follow up with {a.after['company']}"
    if isinstance(a.after, dict) and a.after.get("title"):
        return a.after["title"]  # opportunities, creative and UX work
    return f"{a.kind} {a.target}"
