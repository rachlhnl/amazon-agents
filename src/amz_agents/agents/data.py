"""Data agent: reviews the warehouse, finds opportunities the other agents' rules
don't cover, and recommends what to do about them.

It runs last, after every other agent has proposed, so it can work with them:
  - each opportunity is routed to the agent (and so the team) whose role covers it,
    using the roster the orchestrator shares;
  - it links the other agents' proposals that already address an opportunity, so
    nobody does the work twice and the brief shows what's in progress;
  - it estimates 30-day impact and level of effort, so the chief of staff can rank
    opportunities against everyone else's actions by priority and ROI.

It also checks the data itself (stale, missing, inconsistent), since every other
agent's decisions depend on it. Opportunities are recommendations for a person:
they never execute anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..actions import Action
from .base import Agent, Context, Role
from .product import owned as product_owned

# Assumptions behind opportunity estimates (tune with the company's own results).
ADS_LIFT = 0.30  # extra units from advertising a SKU that has no ads, vs. organic
FEE_SHARE_HIGH = 0.35  # Amazon fees above this share of price are worth a look
REFERRAL_FEE_PCT = 0.15  # most categories; fixed, so only fulfillment fees can shrink
FEE_SAVING = 0.15  # share of fulfillment fees a size-tier fix or multipack can save
AD_DEPENDENCE_HIGH = 0.40  # ad-attributed share of revenue that signals weak organic rank
ORGANIC_SAVING = 0.20  # share of ad spend better listings/ratings could replace
TREND_DROP = -0.15  # last 7 days vs prior 4 weeks
TREND_RISE = 0.20
MIN_UNITS_FOR_TREND = 3.0
ELASTICITY_GAP = 1.0  # measured vs assumed price elasticity worth reporting
CONCENTRATION_HIGH = 0.75  # Amazon share of units
LOW_RATING = 4.0
MIN_REVIEWS = 3
STALE_DAYS = 2


# Market tracker area -> the agent whose role covers it.
MARKET_OWNER = {"Marketing": "marketing", "Pricing": "pricing", "Search": "listing",
                "Amazon listing": "listing", "Ads": "ads", "Data": "data"}


@dataclass
class Opportunity:
    area: str  # short slug, e.g. "fees"
    title: str
    finding: str
    recommendation: str
    owner: str  # agent whose role covers it
    effort: str  # key of EFFORT_HOURS
    sku: str | None = None
    impact_usd: float | None = None
    confidence: str = "low"
    related: list[tuple[str, str]] = field(default_factory=list)  # (kind, target)
    when: int | None = None  # market tracker rank: 1 act this week ... 4 test
    source: str = "data"


class DataAgent(Agent):
    name = "data"
    role = Role(
        mission="Review the data, find opportunities the other agents' rules miss, check "
                "data quality, and size each opportunity (impact, effort) for prioritizing.",
        team="data", proposes={"opportunity": "S"},
        needs=("products", "metrics_daily"),
    )

    def __init__(self, today: date | None = None):
        self.today = today or date.today()

    def run(self, ctx: Context) -> list[Action]:
        self.ctx = ctx
        self.products = {p["sku"]: p for p in ctx.wh.query("SELECT * FROM products")}
        self.proposals: list[Action] = ctx.shared.get("proposals", [])
        roster = ctx.shared.get("roster", {})

        found: list[Opportunity] = []
        for check in (self._data_quality, self._unadvertised, self._fee_heavy,
                      self._ad_dependence, self._trends, self._elasticity,
                      self._concentration, self._ratings, self._market):
            found += check()

        out = []
        for o in found:
            if o.owner not in roster and o.owner != self.name:
                o.recommendation += f" (No {o.owner} agent is running; assign it to someone.)"
            related = list(dict.fromkeys(o.related))  # dedupe, keep order
            out.append(Action(
                agent=self.name, kind="opportunity", target=f"{o.area}/{o.sku or 'all'}",
                sku=o.sku, before=None,
                after={
                    "title": o.title, "finding": o.finding, "recommendation": o.recommendation,
                    "owner": o.owner, "effort": o.effort, "impact_usd": o.impact_usd,
                    "confidence": o.confidence, "related": [list(r) for r in related],
                    **({"when": o.when, "source": o.source} if o.source != "data" else {}),
                },
                reason=o.finding,
            ))
        return out

    def _market(self) -> list[Opportunity]:
        """The market tracker's findings outside product, routed to the agent that owns
        the area (the product agent works the product ones itself)."""
        out = []
        for r in self.ctx.wh.query("SELECT * FROM market_recs ORDER BY rank"):
            if product_owned(r["area"]) or r["area"] == "Website":
                continue  # the product and UX agents work these themselves
            steps = json.loads(r["actions"] or "[]")
            kpi = (f" Track {r['kpi']}: now {r['kpi_now']}, target {r['kpi_target']}."
                   if r["kpi"] else "")
            out.append(Opportunity(
                area="market-" + re.sub(r"[^a-z0-9]+", "-", r["title"].lower()).strip("-")[:60],
                title=r["title"], finding=r["see"],
                recommendation=" ".join([r["why"], *steps]) + kpi,
                owner=MARKET_OWNER.get(r["area"], self.name),
                effort={"Low": "S", "Medium": "M", "High": "L"}.get(r["effort"], "M"),
                confidence="medium" if r["rank"] <= 2 else "low",
                when=r["rank"], source="market",
            ))
        return out

    # ------------------------------------------------------------- helpers
    def _related(self, sku: str | None, kinds: set[str]) -> list[tuple[str, str]]:
        """Other agents' proposals in this run that already work on the opportunity."""
        return [(a.kind, a.target) for a in self.proposals
                if a.kind in kinds and (sku is None or a.sku == sku or a.target == sku)]

    def _metrics(self, sku: str | None, start_ago: int, end_ago: int) -> dict[str, float]:
        start = (self.today - timedelta(days=start_ago)).isoformat()
        end = (self.today - timedelta(days=end_ago)).isoformat()
        sql = ("SELECT COUNT(DISTINCT date) AS days, COALESCE(SUM(units),0) AS units, "
               "COALESCE(SUM(revenue),0) AS revenue, COALESCE(SUM(ad_spend),0) AS ad_spend, "
               "COALESCE(SUM(ad_sales),0) AS ad_sales FROM metrics_daily "
               "WHERE date BETWEEN ? AND ?")
        params: tuple = (start, end)
        if sku:
            sql += " AND sku=?"
            params += (sku,)
        return self.ctx.wh.query(sql, params)[0]

    @staticmethod
    def _complete(p: dict[str, Any]) -> bool:
        return None not in (p["price"], p["landed_cost"], p["fees"])

    @staticmethod
    def _unit_profit(p: dict[str, Any]) -> float:
        return p["price"] - p["landed_cost"] - p["fees"]

    # -------------------------------------------------------------- checks
    def _data_quality(self) -> list[Opportunity]:
        out = []
        last = self.ctx.wh.query("SELECT MAX(date) AS d FROM metrics_daily")[0]["d"]
        if not last or date.fromisoformat(last) < self.today - timedelta(days=STALE_DAYS + 1):
            out.append(Opportunity(
                "data", "Daily sales data is stale",
                f"metrics_daily ends {last or 'nowhere (empty)'}: goals, anomalies, outcome "
                "checks and velocity are running on old numbers.",
                "Check the nightly `amz-agents ingest` job (metrics, inventory).",
                owner=self.name, effort="S", confidence="high",
            ))
        missing = sorted(s for s, p in self.products.items()
                         if None in (p["landed_cost"], p["lead_time_days"], p["fees"]))
        if missing:
            out.append(Opportunity(
                "data", "Products missing cost data",
                f"{len(missing)} SKUs have no landed cost, fees or lead time "
                f"({', '.join(missing[:5])}{'…' if len(missing) > 5 else ''}); pricing, "
                "inventory and every profit estimate skip them.",
                "Add them to the costs CSV and run `amz-agents ingest --costs FILE`.",
                owner=self.name, effort="S", confidence="high",
            ))
        no_attr = self.ctx.wh.query(
            "SELECT campaign_id, name FROM external_campaigns WHERE destination='amazon' "
            "AND status='enabled' AND spend_30d > 0 AND COALESCE(sales_30d, 0) = 0 "
            "AND clicks_30d >= 50"
        )
        if no_attr:
            out.append(Opportunity(
                "data", "Amazon-bound campaigns with no attributed sales",
                f"{len(no_attr)} campaigns send shoppers to Amazon but show $0 in sales "
                f"({', '.join(c['name'] for c in no_attr[:3])}). Either they don't work or "
                "Amazon Attribution results weren't loaded; the marketing agent can't tell.",
                "Load the Attribution report (`amz-agents import external_campaigns FILE`) "
                "before cutting these.",
                owner=self.name, effort="S", confidence="medium",
            ))
        orphans = self.ctx.wh.query(
            "SELECT COUNT(*) AS n FROM keywords WHERE sku IS NULL OR sku NOT IN "
            "(SELECT sku FROM products)"
        )[0]["n"]
        if orphans:
            out.append(Opportunity(
                "data", "Keywords not tied to a product",
                f"{orphans} keywords have no matching SKU, so their bids skip stock checks "
                "and profit estimates.",
                "Check the ad group → SKU mapping in the Ads ingest.",
                owner=self.name, effort="S", confidence="high",
            ))
        return out

    def _unadvertised(self) -> list[Opportunity]:
        low = self.ctx.shared.get("low_stock", set())
        over = self.ctx.shared.get("overstock", set())
        advertised = {r["sku"] for r in self.ctx.wh.query("SELECT DISTINCT sku FROM keywords")}
        g = self.ctx.settings.guardrails
        out = []
        for sku, p in self.products.items():
            v = p["daily_velocity"] or 0
            if v <= 0 or sku in low or sku in advertised or not self._complete(p):
                continue
            if self._metrics(sku, 30, 1)["ad_spend"] > 0:
                continue  # advertised some other way (auto, product targeting)
            per_unit = self._unit_profit(p) - p["price"] * g.target_acos
            if per_unit <= 0:
                continue
            impact = round(v * 30 * ADS_LIFT * per_unit, 2)
            out.append(Opportunity(
                "ads", f"{sku} isn't advertised",
                f"{sku} sells {v:g}/day with no ad spend"
                + (" and is overstocked" if sku in over else "")
                + f"; at {g.target_acos:.0%} ACoS each extra unit still makes "
                f"${per_unit:.2f}.",
                "Launch a Sponsored Products auto campaign to find converting terms; the ads "
                "agent will harvest and tune from there.",
                owner="ads", effort="M", sku=sku, impact_usd=impact,
                related=self._related(sku, {"email_campaign"}),
            ))
        return out

    def _fee_heavy(self) -> list[Opportunity]:
        out = []
        for sku, p in self.products.items():
            if not self._complete(p) or not p["price"]:
                continue
            share = p["fees"] / p["price"]
            if share < FEE_SHARE_HIGH:
                continue
            fulfillment = max(p["fees"] - p["price"] * REFERRAL_FEE_PCT, 0)
            impact = round((p["daily_velocity"] or 0) * 30 * fulfillment * FEE_SAVING, 2)
            out.append(Opportunity(
                "fees", f"Fees eat {share:.0%} of {sku}'s price",
                f"Amazon fees are ${p['fees']:.2f} on a ${p['price']:.2f} item, about "
                f"${fulfillment:.2f} of it fulfillment.",
                "Check the FBA size tier and measured dimensions in Seller Central; if the "
                "tier is right, consider a multipack to spread the fixed fee.",
                owner="pricing", effort="M", sku=sku, impact_usd=impact or None,
            ))
        return out

    def _ad_dependence(self) -> list[Opportunity]:
        out = []
        for sku in self.products:
            m = self._metrics(sku, 30, 1)
            if m["revenue"] <= 0 or m["ad_sales"] / m["revenue"] < AD_DEPENDENCE_HIGH:
                continue
            share = m["ad_sales"] / m["revenue"]
            out.append(Opportunity(
                "organic", f"{sku} depends on ads for {share:.0%} of sales",
                f"${m['ad_sales']:,.0f} of ${m['revenue']:,.0f} in 30-day sales came through "
                "ads: organic rank is weak.",
                "Improve the listing (title, images, A+) and ratings so organic search "
                "carries more of the sales.",
                owner="listing", effort="M", sku=sku,
                impact_usd=round(m["ad_spend"] * ORGANIC_SAVING, 2),
                related=self._related(sku, {"update_listing_copy", "flag_defect"}),
            ))
        return out

    def _trends(self) -> list[Opportunity]:
        low = self.ctx.shared.get("low_stock", set())
        out = []
        for sku, p in self.products.items():
            recent, prior = self._metrics(sku, 7, 1), self._metrics(sku, 35, 8)
            if not recent["days"] or not prior["days"]:
                continue
            r, b = recent["units"] / recent["days"], prior["units"] / prior["days"]
            if b < MIN_UNITS_FOR_TREND:
                continue
            change = r / b - 1
            if change <= TREND_DROP:
                lost = (b - r) * 30 * (self._unit_profit(p) if self._complete(p) else 0)
                out.append(Opportunity(
                    "trend", f"{sku} sales down {abs(change):.0%}",
                    f"{r:.1f}/day over the last week vs {b:.1f}/day over the four before.",
                    "Check the Buy Box, price changes by competitors, listing suppression and "
                    "new negative reviews.",
                    owner="pricing", effort="M", sku=sku, impact_usd=round(lost, 2) or None,
                    confidence="medium",
                    related=self._related(sku, {"update_price", "update_listing_copy"}),
                ))
            elif change >= TREND_RISE and sku not in low:
                out.append(Opportunity(
                    "trend", f"{sku} sales up {change:.0%}",
                    f"{r:.1f}/day over the last week vs {b:.1f}/day over the four before, "
                    "with stock to spare.",
                    "Lean in: check bids aren't capping impressions and that stock covers the "
                    "new pace through the next reorder.",
                    owner="ads", effort="S", sku=sku, confidence="medium",
                    related=self._related(sku, {"update_bid", "add_keyword", "reorder"}),
                ))
        return out

    def _elasticity(self) -> list[Opportunity]:
        """Measure how units actually moved after executed price changes."""
        from .chief_of_staff import PRICE_ELASTICITY

        out = []
        for a in self.ctx.wh.query(
            "SELECT * FROM actions WHERE kind='update_price' AND status='executed' "
            "AND executed_at IS NOT NULL"
        ):
            ago = (self.today - date.fromisoformat(a["executed_at"][:10])).days
            if ago < 8:
                continue  # needs a week of sales after the change
            before = json.loads(a["before"])["price"]
            after = json.loads(a["after"])["price"]
            pre = self._metrics(a["target"], ago + 7, ago + 1)  # week before
            post = self._metrics(a["target"], ago - 1, ago - 7)  # week after
            if not pre["days"] or not post["days"] or before == after:
                continue
            u0, u1 = pre["units"] / pre["days"], post["units"] / post["days"]
            if not u0:
                continue
            measured = (u1 / u0 - 1) / (after / before - 1)
            if abs(measured - PRICE_ELASTICITY) < ELASTICITY_GAP:
                continue
            more = abs(measured) > abs(PRICE_ELASTICITY)
            out.append(Opportunity(
                "elasticity", f"{a['target']} is {'more' if more else 'less'} price-sensitive "
                "than assumed",
                f"After the {before:.2f} → {after:.2f} change, units went {u0:.1f} → "
                f"{u1:.1f}/day: elasticity {measured:.1f} vs the {PRICE_ELASTICITY} the "
                "estimates assume.",
                ("Price moves on this SKU pay off more than estimated; "
                 if more else "Price cuts on this SKU buy fewer units than estimated; ")
                + "review its pricing rules, and update PRICE_ELASTICITY once a few SKUs agree.",
                owner="pricing", effort="XS", sku=a["target"], confidence="low",
                related=self._related(a["target"], {"update_price"}),
            ))
        return out

    def _concentration(self) -> list[Opportunity]:
        amazon = sum((p["daily_velocity"] or 0) * 30 for p in self.products.values())
        other = self.ctx.wh.query(
            "SELECT COALESCE(SUM(units_30d), 0) AS u FROM channel_listings WHERE listed = 1"
        )[0]["u"]
        total = amazon + other
        if not total or amazon / total < CONCENTRATION_HIGH:
            return []
        return [Opportunity(
            "channels", f"Amazon is {amazon / total:.0%} of unit sales",
            f"{amazon:,.0f} of {total:,.0f} units in the last 30 days sold on Amazon. One "
            "account suspension, listing problem or fee change hits most of the business.",
            "Set a target for off-Amazon share and work the sales agent's channel and "
            "wholesale suggestions toward it.",
            owner="sales", effort="L", confidence="high",
            related=self._related(None, {"list_on_channel", "send_outreach"}),
        )]

    def _ratings(self) -> list[Opportunity]:
        out = []
        by_asin = {p["asin"]: sku for sku, p in self.products.items()}
        for r in self.ctx.wh.query(
            "SELECT asin, AVG(rating) AS avg, COUNT(*) AS n FROM reviews "
            "WHERE asin NOT LIKE 'store:%' GROUP BY asin "  # store ratings: product agent
            "HAVING n >= ? AND avg < ?", (MIN_REVIEWS, LOW_RATING)
        ):
            sku = by_asin.get(r["asin"])
            themes = self.ctx.shared.get("review_themes", {}).get(r["asin"], [])
            out.append(Opportunity(
                "ratings", f"{sku or r['asin']} rated {r['avg']:.1f} recently",
                f"{r['n']} recent reviews average {r['avg']:.1f} stars"
                + (f"; complaints: {', '.join(themes)}" if themes else "")
                + ". Ratings under 4 cut conversion on every channel and raise ad costs.",
                "Fix the root cause with the supplier first, then set expectations in the "
                "listing.",
                owner="reviews", effort="L", sku=sku, confidence="medium",
                related=self._related(sku, {"flag_defect", "update_listing_copy"})
                + self._related(r["asin"], {"flag_defect"}),
            ))
        return out
