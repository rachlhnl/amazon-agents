"""Promotions agent: the Data & promotions analyst's desk. Runs the deal calendar.

For each event in the calendar (promotions.toml) it:
  - picks deal candidates from best sellers with enough stock for the event,
  - estimates the deal price each event's rules require and checks the margin with the
    pricing agent's floor,
  - holds prices on those candidates before the event, so a price cut doesn't lower the
    30-day low the deal has to beat (the pricing agent reads ctx.shared["price_hold"]),
  - sends stock that won't cover the event to Amazon operations,
  - asks for Walmart prices to match when the event runs there too,
  - writes each finished event's results for the ad agency,
  - and says exactly which data is missing and who can supply it.

Runs after inventory and reviews (it reads their findings) and before pricing.
Deal prices are estimates from daily average selling prices; Amazon's deal screen shows
the price an event actually requires.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ..actions import Action
from ..assumptions import is_assumed
from ..guardrails import price_floor
from ..styles import styles
from .base import Agent, Context, Role, days_of_cover

HORIZON_DAYS = 75  # plan events whose submission or start is this close
RESULTS_DAYS = 21  # report an event's results this long after it ends
CANDIDATES = 15  # best sellers put forward per event
EVENT_LIFT = 2.5  # units a day during a short event vs a normal day, until our own results replace it
LONG_LIFT = 1.3  # the same for promotions longer than a week
STALE_DAYS = 7  # sales and stock older than this make the stock checks unreliable
MIN_RATING = 4.0  # Amazon price discounts need rated products at four stars or more
BEFORE_DAYS = 14  # baseline window for an event's results


@dataclass
class Event:
    name: str
    channel: str  # amazon | walmart | shopify
    starts: date
    ends: date
    submit_by: date | None = None
    hold_days: int = 0  # price hold before the event starts
    deal_type: str = ""
    min_discount: float = 0.0  # vs the recent selling price
    below_30d_low: float = 0.0  # deal price must sit this far under the 30-day low
    upfront_fee: float = 0.0
    pct_fee: float = 0.0
    fee_cap: float | None = None
    confirmed: bool = False  # dates and rules checked against the channel's own pages
    source: str = ""
    notes: str = ""
    pick: str = "best_sellers"  # best_sellers | overstock (clearance)
    lift: float | None = None  # expected demand vs a normal day; default by event length
    skus: list[str] = field(default_factory=list)  # optional fixed picks
    group: str = ""  # the occasion this platform's promotion belongs to, e.g. "Black Friday"
    deadline: str = "ours"  # whose date submit_by is: amazon | walmart | ours (our lead time)
    fee_basis: str = "unknown"  # per_deal (each parent style) | per_campaign | none | unknown
    discount_proposed: bool = True  # min_discount is a suggestion until someone sets it
    alt_deal_type: str = ""  # a second way to run it on the same platform, priced alongside
    alt_fee_basis: str = ""

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", f"{self.name} {self.starts}".lower()).strip("-")

    @property
    def hold_from(self) -> date:
        return self.starts - timedelta(days=self.hold_days)


def load_calendar(path: str | Path | None = None) -> list[Event]:
    """PROMOTIONS_PATH, else promotions.local.toml (git-ignored), else promotions.toml."""
    if path is None:
        env, local = os.getenv("PROMOTIONS_PATH"), Path("promotions.local.toml")
        path = env or (local if local.exists() else Path("promotions.toml"))
    path = Path(path)
    if not path.exists():
        return []
    with path.open("rb") as f:
        raw = tomllib.load(f).get("event", [])
    out = []
    for e in raw:
        e = dict(e)
        for k in ("starts", "ends", "submit_by"):
            if isinstance(e.get(k), str):
                e[k] = date.fromisoformat(e[k])
        out.append(Event(**e))
    return sorted(out, key=lambda e: e.starts)


def estimate_cost(ev: Event, picks: list[dict], style_of: dict[str, tuple] | None = None) -> dict:
    """What an event costs: discount given away plus channel fees, from each pick's sales
    rate. Estimates; margin needs real unit costs."""
    days = (ev.ends - ev.starts).days + 1
    lift = ev.lift or (EVENT_LIFT if days <= 7 else LONG_LIFT)
    units = sales = given = 0.0
    for p in picks:
        u = (p.get("per_day") or 0) * lift * days
        units += u
        sales += u * p["deal_price"]
        given += u * max(0.0, (p["reference"] or 0) - p["deal_price"])
    styles_n = len({(style_of or {}).get(p["sku"], (p["sku"],))[0] for p in picks})
    deals = {"per_deal": styles_n, "per_campaign": 1 if picks else 0}.get(ev.fee_basis, 0)
    upfront = ev.upfront_fee * deals
    pct = ev.pct_fee * sales
    if ev.fee_cap is not None and deals:
        pct = min(pct, ev.fee_cap * deals)
    fees = upfront + pct if ev.fee_basis in ("per_deal", "per_campaign") else None
    return {"est_units": round(units), "deal_sales": round(sales), "discount_given": round(given),
            "deals": deals, "upfront_fees": round(upfront) if fees is not None else None,
            "pct_fees": round(pct) if fees is not None else None,
            "fees_total": round(fees) if fees is not None else None,
            "cost_total": round(given + fees) if fees is not None else round(given),
            "fee_basis": ev.fee_basis, "lift": lift, "days": days}


class PromotionsAgent(Agent):
    name = "promotions"
    role = Role(
        mission="Run the deal calendar: pick what goes on deal, at what price, with enough "
                "stock; hold prices before events; match channels; report results.",
        team="promotions",
        proposes={"plan_deal": "M", "hold_price": "XS", "event_stock": "S",
                  "match_channel_price": "S", "promotion_results": "S", "request_data": "XS"},
        needs=("products", "metrics_daily"),
    )

    def __init__(self, calendar: list[Event] | None = None, today: date | None = None):
        self._calendar = calendar
        self._today = today

    def run(self, ctx: Context) -> list[Action]:
        today = self._today or date.today()
        events = self._calendar if self._calendar is not None else load_calendar()
        prices = self._prices(ctx, today)
        ratings = {r["asin"]: r["stars"] for r in ctx.wh.query(
            "SELECT asin, AVG(rating) AS stars FROM reviews GROUP BY asin")}
        walmart = {r["sku"]: r for r in ctx.wh.query(
            "SELECT * FROM channel_listings WHERE channel = 'walmart'")}
        low = ctx.shared.get("low_stock", set())
        g = ctx.settings.guardrails
        hold: dict[str, str] = {}
        missing: dict[str, str] = {}
        out: list[Action] = []
        latest = ctx.wh.query("SELECT MAX(date) AS d FROM metrics_daily")[0]["d"]
        if latest and (today - date.fromisoformat(latest)).days > STALE_DAYS:
            missing["stale"] = (f"Sales and stock are as of {latest}: download fresh All Orders "
                                "and FBA Inventory reports (or add SP-API keys) before choosing "
                                "deal stock.")

        if not events:
            ctx.shared["price_hold"] = {}
            return []  # no calendar, nothing to run
        style_of = styles(ctx.wh)
        planned: list[tuple[Event, list[dict], Action]] = []
        for ev in events:
            # Event-run windows end in the past, so this event is only results now.
            if ev.ends < today:
                if (today - ev.ends).days <= RESULTS_DAYS:
                    out += self._results(ctx, ev, prices, missing)
                continue
            soon = min(d for d in (ev.submit_by, ev.hold_from, ev.starts) if d)
            if (soon - today).days > HORIZON_DAYS:
                continue
            picks, excluded, short = self._candidates(ctx, ev, today, prices, ratings, low, g)
            if not ev.confirmed:
                missing[f"confirm:{ev.slug}"] = (
                    f"{ev.name}: dates, fees and discount rules are from secondary sources. "
                    "Confirm them on the channel's deal screen before submitting.")
            if any(p["margin"] is None for p in picks):
                missing["costs"] = ("Real landed cost per SKU (COSTS_CSV) and real Amazon fees "
                                    "(SP-API keys, or Seller Central's Fee Preview report): "
                                    "without them deal margins can't be checked.")
            if ev.channel == "amazon" and any(p["rating"] is None for p in picks):
                missing["ratings"] = ("Amazon star ratings per ASIN: the warehouse only has store "
                                      "reviews. Load the Voice of the Customer listing summary "
                                      "(it has each listing's star rating); Amazon price discounts "
                                      "need four stars or more.")
            if not prices:
                missing["price_history"] = ("Daily sales by SKU for the last 30 days (All Orders "
                                            "report, or SP-API): needed for the price a deal has "
                                            "to beat.")
            plan = self._plan(ev, today, picks, excluded, estimate_cost(ev, picks, style_of))
            planned.append((ev, picks, plan))
            out.append(plan)
            if ev.hold_days and picks and ev.hold_from <= today + timedelta(days=7):
                for p in picks:
                    hold[p["sku"]] = ev.name
                out.append(self._hold(ev, picks))
            if short:
                out.append(self._stock(ev, short))
            if ev.channel == "walmart":
                out += self._walmart(ev, picks, walmart, missing)

        # Two promotions on the same platform at once stack discounts and fees.
        for ev, picks, plan in planned:
            mine = {p["sku"] for p in picks}
            plan.after["overlaps"] = [
                {"event": o.name, "shared_skus": len(mine & {p["sku"] for p in op})}
                for o, op, _ in planned
                if o is not ev and o.channel == ev.channel and o.starts <= ev.ends and ev.starts <= o.ends
            ]
        ctx.shared["price_hold"] = hold  # the pricing agent leaves these prices alone
        if missing:
            out.append(Action(
                agent=self.name, kind="request_data", target="promotions/data",
                before={}, after={"title": f"Data the deal calendar is missing ({len(missing)})",
                                  "missing": sorted(missing.values())},
                reason="The promotions agent fills in what it can and lists the rest here.",
            ))
        return out

    # --- inputs -------------------------------------------------------------------------

    def _prices(self, ctx: Context, today: date) -> dict[str, dict]:
        """Average and lowest daily selling price per SKU over the last 30 days."""
        since = (today - timedelta(days=30)).isoformat()
        rows = ctx.wh.query(
            "SELECT sku, SUM(revenue) / SUM(units) AS avg, MIN(revenue / units) AS low, "
            "SUM(units) AS units FROM metrics_daily WHERE date >= ? AND units > 0 "
            "AND revenue > 0 GROUP BY sku", (since,))
        return {r["sku"]: r for r in rows}

    def _candidates(self, ctx, ev, today, prices, ratings, low, g, check_stock=True):
        days = (ev.ends - ev.starts).days + 1
        to_start = max(0, (ev.starts - today).days)
        lift = ev.lift or (EVENT_LIFT if days <= 7 else LONG_LIFT)
        rows = ctx.wh.query(
            "SELECT * FROM products WHERE daily_velocity > 0 ORDER BY daily_velocity DESC")
        if ev.skus:
            rows = [p for p in rows if p["sku"] in ev.skus]
        elif ev.pick == "overstock":
            over = ctx.shared.get("overstock", set())
            rows = [p for p in rows if p["sku"] in over]
        picks, excluded, short = [], [], []
        for p in rows:
            if len(picks) >= CANDIDATES:
                break
            v = p["daily_velocity"]
            if p["units_on_hand"] is None:
                continue  # no stock report row: can't promise deal stock
            need = v * to_start + v * lift * days
            have = (p["units_on_hand"] or 0) + (p["units_inbound"] or 0)
            stars = ratings.get(p["asin"])
            if stars is not None and stars < MIN_RATING:
                excluded.append({"sku": p["sku"], "why": f"rated {stars:.1f}, under {MIN_RATING:g}"})
                continue
            if check_stock and (p["sku"] in low or have < need):
                lands = p["lead_time_days"] is not None and today + timedelta(days=p["lead_time_days"]) <= ev.starts
                short.append({"sku": p["sku"], "title": p["title"], "have": have,
                              "need": round(need), "per_day": round(v, 1),
                              "order_in_time": lands})
                excluded.append({"sku": p["sku"], "why": (
                    f"runs out before a reorder can land ({days_of_cover(p):.0f} days of cover vs "
                    f"{p['lead_time_days']}-day lead time)" if p["sku"] in low and have >= need
                    else f"stock {have} vs about {round(need)} needed")})
                continue
            ref = (prices.get(p["sku"]) or {}).get("avg") or p["price"]
            deal = ref * (1 - ev.min_discount) if ev.min_discount else ref
            low30 = (prices.get(p["sku"]) or {}).get("low")
            if ev.below_30d_low and low30:
                deal = min(deal, low30 * (1 - ev.below_30d_low))
            deal = round(deal, 2)
            margin = None
            if not is_assumed(p, "landed_cost", "fees") and p["landed_cost"] is not None:
                floor = price_floor(g, p["landed_cost"], p["fees"] or 0)
                if deal < floor:
                    excluded.append({"sku": p["sku"], "why": f"deal price {deal:.2f} under floor {floor:.2f}"})
                    continue
                margin = round((deal - p["landed_cost"] - (p["fees"] or 0)) / deal, 3)
            picks.append({"sku": p["sku"], "title": p["title"], "price": p["price"],
                          "per_day": round(v, 2),
                          "reference": round(ref, 2), "deal_price": deal,
                          "discount": round(1 - deal / ref, 3) if ref else None,
                          "days_of_cover": round(days_of_cover(p), 1), "margin": margin,
                          "rating": round(stars, 1) if stars is not None else None})
        return picks, excluded, short

    # --- proposals ------------------------------------------------------------------------

    def _plan(self, ev: Event, today: date, picks, excluded, cost: dict) -> Action:
        due = ev.submit_by or ev.starts
        days_left = (due - today).days
        fee = ev.upfront_fee
        return Action(
            agent=self.name, kind="plan_deal", target=f"event/{ev.slug}",
            before={"candidates": len(picks)},
            after={"title": f"{ev.name}: {len(picks)} deal candidates, submit by {due:%b %-d}",
                   "event": ev.name, "channel": ev.channel, "deal_type": ev.deal_type,
                   "starts": ev.starts.isoformat(), "ends": ev.ends.isoformat(),
                   "submit_by": ev.submit_by.isoformat() if ev.submit_by else None,
                   "hold_from": ev.hold_from.isoformat() if ev.hold_days else None,
                   "confirmed": ev.confirmed, "source": ev.source,
                   "upfront_fee_per_deal": fee or None, "pct_fee": ev.pct_fee or None,
                   "fee_cap": ev.fee_cap, "fee_basis": ev.fee_basis, "deadline": ev.deadline,
                   "discount_proposed": ev.discount_proposed, "group": ev.group or ev.name,
                   "cost": cost,
                   "skus": picks, "excluded": excluded[:20],
                   "when": max(1, days_left) if days_left <= 14 else None},
            reason=(f"{ev.name} runs {ev.starts:%b %-d}–{ev.ends:%b %-d}; "
                    + (f"{'submissions close' if ev.deadline != 'ours' else 'set up by'} "
                       f"{ev.submit_by:%b %-d} ({days_left} days"
                       f"{'' if ev.deadline != 'ours' else ', our lead time'}). "
                       if ev.submit_by else "")
                    + (f"Estimated cost ${cost['cost_total']:,} (discount ${cost['discount_given']:,}"
                       + (f" + fees ${cost['fees_total']:,}" if cost['fees_total'] is not None else "")
                       + f") on about ${cost['deal_sales']:,} of deal sales. " if picks else "")
                    + (f"Deal prices aim {ev.min_discount:.0%} under the recent selling price. "
                       if ev.min_discount else "")
                    + ("Dates and rules not yet confirmed on the channel's own pages."
                       if not ev.confirmed else "")),
            notes=ev.notes,
        )

    def _hold(self, ev: Event, picks) -> Action:
        return Action(
            agent=self.name, kind="hold_price", target=f"hold/{ev.slug}",
            before={}, after={"title": f"Hold prices on {len(picks)} deal candidates until "
                                       f"{ev.name}",
                              "from": ev.hold_from.isoformat(), "until": ev.ends.isoformat(),
                              "skus": [p["sku"] for p in picks]},
            reason=("A price cut now lowers the 30-day low the deal has to beat; the pricing "
                    "agent won't change these prices until the event ends."),
        )

    def _stock(self, ev: Event, short) -> Action:
        late = [s for s in short if not s["order_in_time"]]
        return Action(
            agent=self.name, kind="event_stock", target=f"stock/{ev.slug}",
            before={"short_skus": len(short)},
            after={"title": f"{len(short)} best sellers won't have stock for {ev.name}",
                   "lines": short, "too_late_to_reorder": len(late), "team": "amazon_ops"},
            reason=("Event demand assumed above a normal day (2.5× for short events, 1.3× for "
                    "longer ones) until our own results replace it. "
                    + (f"{len(late)} can't be restocked before {ev.starts:%b %-d} at their "
                       "lead time: leave them out of the deal or expedite." if late else
                       "Reorders placed now land before the event.")),
        )

    def _walmart(self, ev: Event, picks, walmart, missing) -> list[Action]:
        if not walmart:
            missing["walmart"] = ("Walmart prices per SKU (Seller Center → Items → download, "
                                  "or the Walmart API): needed to match Walmart to Amazon deals "
                                  "and keep prices at parity.")
            return []
        lines = []
        for p in picks:
            w = walmart.get(p["sku"])
            if w and w["price"] and abs(w["price"] - p["deal_price"]) >= 0.01:
                lines.append({"sku": p["sku"], "walmart": w["price"], "match_to": p["deal_price"]})
        if not lines:
            return []
        return [Action(
            agent=self.name, kind="match_channel_price", target=f"walmart/{ev.slug}",
            before={"skus": len(lines)},
            after={"title": f"Match {len(lines)} Walmart prices for {ev.name}", "lines": lines,
                   "starts": ev.starts.isoformat(), "ends": ev.ends.isoformat()},
            reason=("Walmart expects prices at parity with other sites, and price changes can "
                    "take hours to show: schedule them ahead of the event."),
        )]

    def _results(self, ctx: Context, ev: Event, prices, missing) -> list[Action]:
        """Units and sales during the event vs the two weeks before, for the ad agency."""
        before_from = ev.starts - timedelta(days=BEFORE_DAYS)
        days = (ev.ends - ev.starts).days + 1
        q = ("SELECT SUM(units) AS units, SUM(revenue) AS revenue, SUM(ad_spend) AS spend, "
             "COUNT(DISTINCT date) AS days FROM metrics_daily WHERE date BETWEEN ? AND ?")
        during = ctx.wh.query(q, (ev.starts.isoformat(), ev.ends.isoformat()))[0]
        before = ctx.wh.query(q, (before_from.isoformat(),
                                  (ev.starts - timedelta(days=1)).isoformat()))[0]
        if not during["days"] or not before["days"]:
            missing[f"results:{ev.slug}"] = (f"{ev.name}: daily sales for {before_from:%b %-d}–"
                                             f"{ev.ends:%b %-d} aren't loaded, so results can't "
                                             "be measured.")
            return []
        per_day = lambda r: (r["revenue"] or 0) / r["days"]
        lift = per_day(during) / per_day(before) - 1 if per_day(before) else None
        return [Action(
            agent=self.name, kind="promotion_results", target=f"results/{ev.slug}",
            before={"revenue_per_day": round(per_day(before), 2)},
            after={"title": f"{ev.name} results"
                            + (f": sales {lift:+.0%} a day vs the 2 weeks before" if lift is not None else ""),
                   "revenue": round(during["revenue"] or 0, 2), "units": during["units"],
                   "ad_spend": round(during["spend"] or 0, 2), "days": days,
                   "lift": round(lift, 3) if lift is not None else None,
                   "send_to": "Quartile (channel and budget review) and the weekly report",
                   "team": "marketing"},
            reason="Every event gets a result, so the next calendar uses real lift, not a guess.",
        )]
