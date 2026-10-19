"""Chief of staff: turns the agents' proposals into a plan a busy person can act on.

It doesn't propose changes of its own. It:
  1. tracks progress against the company's goals (goals.toml),
  2. estimates the 30-day profit impact of every proposed action and ranks them,
  3. tags each action with the goal it serves or works against,
  4. escalates what needs a human: goal conflicts and sudden sales/spend anomalies,
  5. checks whether executed actions actually helped, and says what to tighten or trust,
  6. writes the brief.

Estimates are deliberately simple and their assumptions are stated, so a
person can see why something ranked where it did. Use them to rank, not forecast.
"""

from __future__ import annotations

import os
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .. import llm
from ..actions import Action
from .base import Context, days_of_cover

# Assumptions behind the impact estimates (tune with the company's own data).
PRICE_ELASTICITY = -1.5  # % change in units per % change in price
BID_SALES_SENSITIVITY = 0.5  # sales move half as much as bids
HARVEST_UPLIFT = 0.25  # extra profit share from moving a term to exact match
BUDGET_SALES_SENSITIVITY = 0.7  # the next $ of external budget earns 70% of the average $
SAFETY_DAYS = 14

WINDOW = 7  # days, for goal progress and outcome checks
VERDICT_THRESHOLD = 0.03  # +/- change in daily profit that counts as helped/hurt
ANOMALY_DROP = 0.5  # yesterday's units below this share of the 14-day average
SPEND_SPIKE = 2.0  # yesterday's ad spend above this multiple of average

GROW, EFFICIENCY, SHRINK = "grow", "efficiency", "shrink"

# Spend outside Amazon doesn't count toward TACoS (Amazon ad spend / Amazon revenue).
EXTERNAL_KINDS = {"update_campaign_budget", "pause_campaign", "email_campaign",
                  "update_channel_price", "list_on_channel", "send_outreach"}


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

    def __init__(self, ctx: Context, goals: list[Goal] | None = None, today: date | None = None):
        self.ctx = ctx
        self.goals = goals if goals is not None else load_goals()
        self.today = today or date.today()
        self.products = {p["sku"]: p for p in ctx.wh.query("SELECT * FROM products")}

    # ------------------------------------------------------------------ review
    def review(self, actions: list[Action]) -> list[Action]:
        """Attach SKU, impact estimate, confidence and goal tag to each action."""
        for a in actions:
            a.sku = a.sku or self._sku_for(a)
            a.impact_usd, a.confidence, note = self._estimate(a)
            if note:
                a.notes = "; ".join(x for x in (a.notes, note) if x)
            a.goal = self._goal_tag(a)
        return actions

    def plan(self, actions: list[Action]) -> list[Action]:
        """Add escalations, then order everything for the person approving."""
        self.progress = self.goal_progress()
        escalations = self.conflicts(actions) + self.anomalies()
        ranked = sorted(
            actions,
            key=lambda a: (a.impact_usd is None, -(a.impact_usd or 0)),
        )
        return escalations + ranked

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

            if avg_units >= 3 and y["units"] < avg_units * ANOMALY_DROP:
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
        esc = [(i, a) for i, a in submitted if a.kind == "escalation"]
        acts = [(i, a) for i, a in submitted if a.kind != "escalation"]
        top = [(i, a) for i, a in acts if (a.impact_usd or 0) > 0][:5]
        negative = [(i, a) for i, a in acts if a.impact_usd is not None and a.impact_usd < 0]
        # Judgment calls carry no estimate; wholesale leads go first, biggest first.
        judgment = sorted(
            [(i, a) for i, a in acts if a.impact_usd is None],
            key=lambda x: -(x[1].after.get("est_annual_value") or 0)
            if isinstance(x[1].after, dict) else 0,
        )
        shown = {i for i, _ in top + negative + judgment}
        rest = [(i, a) for i, a in acts if i not in shown]

        L = [f"# Chief of staff brief: {self.today.isoformat()}", "",
             (f"Mode: **{self.ctx.settings.mode.value}** · {len(acts)} proposed actions · "
             f"{len(esc)} escalations"), ""]

        price_ids = {a.sku: i for i, a in acts if a.kind == "update_price"}

        def link(a: Action) -> str:
            ref = "(see the pricing action for this SKU)"
            if ref in a.reason and a.sku in price_ids:
                return a.reason.replace(ref, f"(see action [{price_ids[a.sku]}])")
            return a.reason

        gaps = sorted(s for s, p in self.products.items()
                      if None in (p["landed_cost"], p["lead_time_days"], p["fees"]))
        if gaps:
            L += [(f"_Skipped for missing cost, fee or lead-time data: {', '.join(gaps)}. "
                   "Load them with `amz-agents ingest --costs FILE`._"), ""]

        L += ["## Needs your attention", ""]
        L += [f"- **[{i}]** {link(a)}" for i, a in esc] or ["Nothing urgent."]

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

        L += ["", "## Top actions by estimated 30-day impact", ""]
        if top:
            L += ["| # | Action | Est. impact | Confidence | Goal | Why |", "|---|---|---|---|---|---|"]
            L += [f"| {i} | {_describe(a)} | {_usd(a.impact_usd)} | {a.confidence} | "
                  f"{a.goal or '—'} | {a.reason}" + (f" ({a.notes})" if a.notes else "") + " |"
                  for i, a in top]
            if self.ctx.settings.mode.value == "approve":
                ids = " ".join(str(i) for i, _ in top)
                L += ["", f"Approve all {len(top)}: `amz-agents approve {ids}`"]
        else:
            L += ["No positive-impact actions today."]

        if negative:
            L += ["", "## Consider rejecting", "",
                  "These are estimated to cost profit. Approve only if the reason outweighs it.", ""]
            L += [f"- **[{i}]** {_describe(a)}: est. {_usd(a.impact_usd)}. {a.reason}"
                  + (f" ({a.notes})" if a.notes else "") for i, a in negative]

        if judgment:
            L += ["", "## Needs your judgment", ""]
            L += [f"- **[{i}]** {_describe(a)}: {a.reason}" for i, a in judgment]

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

        if rest:
            L += ["", "## Everything else", ""]
            L += [f"- [{i}] {_describe(a)}: {a.reason}" for i, a in rest]

        L += ["", "---", (f"_Impact estimates assume price elasticity {PRICE_ELASTICITY}, "
              f"sales moving {BID_SALES_SENSITIVITY:.0%} as much as bids, the next dollar of "
              f"external budget earning {BUDGET_SALES_SENSITIVITY:.0%} of the average, "
              "and trailing 30-day data. "
              "Use them to rank, not to forecast._")]

        text = "\n".join(L)
        narrative = llm.complete(
            "You are chief of staff to the head of a consumer brand that sells mainly on "
            "Amazon, plus its own store, other channels and wholesale. Write a 4-sentence note to "
            "open the brief: the one thing to decide today, the biggest opportunity, how goals "
            "are tracking, and anything the numbers can't tell you. Plain language. No hype.",
            text, self.ctx.settings.anthropic_model, max_tokens=400,
        )
        if narrative:
            text = text.replace("\n\n## Needs your attention", f"\n\n{narrative.strip()}\n\n"
                                "## Needs your attention", 1)
        return text + "\n"

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
        sql = ("SELECT COUNT(DISTINCT date) AS days, COALESCE(SUM(units),0) AS units, "
               "COALESCE(SUM(revenue),0) AS revenue, COALESCE(SUM(ad_spend),0) AS ad_spend, "
               "COALESCE(SUM(ad_sales),0) AS ad_sales FROM metrics_daily WHERE date BETWEEN ? AND ?")
        params: tuple = (start, end)
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
    return f"{a.kind} {a.target}"
