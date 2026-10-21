"""Sales agent: channels outside Amazon (own store, other marketplaces, wholesale).

  - Price parity: Amazon's fair pricing policy can take the Buy Box away when the
    same product is noticeably cheaper elsewhere, so off-Amazon prices below the
    Amazon price are raised to match (within guardrails).
  - Margin floor: off-Amazon prices that don't clear the margin floor after that
    channel's fees are raised.
  - New channels: SKUs selling well on Amazon but not listed on a channel the
    company already sells through.
  - Wholesale: leads that have gone quiet get a follow-up, drafted for a person
    to send. Interest in a SKU that is about to stock out is called out so nobody
    promises volume that isn't there.

Prices pass through guardrails. Listings and outreach need a human.
"""

from __future__ import annotations

from datetime import date

from .. import llm
from ..actions import Action
from ..guardrails import clamp_price
from .base import Agent, Context, Role

PARITY_TOLERANCE = 0.02  # off-Amazon price may sit this far below Amazon's
MIN_VELOCITY_TO_EXPAND = 5.0  # Amazon units/day before suggesting another channel
SKU_MATCH_MIN = 0.25  # share of Amazon's top 20 SKUs a channel must list to compare
FOLLOW_UP_DAYS = {"new": 2, "contacted": 7, "negotiating": 3}


class SalesAgent(Agent):
    name = "sales"
    role = Role(
        mission="Grow sales outside Amazon (own store, other marketplaces, wholesale) "
                "without undercutting the Amazon Buy Box.",
        team="sales",
        proposes={"update_channel_price": "XS", "review_channel_pricing": "S",
                  "list_on_channel": "L", "send_outreach": "S"},
        needs=("channel_listings", "leads"),
    )

    def __init__(self, today: date | None = None):
        self.today = today or date.today()

    def run(self, ctx: Context) -> list[Action]:
        return self._prices(ctx) + self._expand(ctx) + self._leads(ctx)

    def _prices(self, ctx: Context) -> list[Action]:
        g = ctx.settings.guardrails
        pending = ctx.shared.get("amazon_price_proposals", {})
        out: list[Action] = []
        gaps: list[dict] = []  # parity gaps on products whose costs are assumed
        for r in ctx.wh.query(
            "SELECT c.*, p.price AS amazon_price, p.landed_cost, p.assumed, p.daily_velocity "
            "FROM channel_listings c "
            "JOIN products p ON p.sku = c.sku WHERE c.listed = 1 AND p.landed_cost IS NOT NULL "
            "AND c.fees IS NOT NULL ORDER BY c.channel, c.sku"
        ):
            # Parity is judged against the lower of today's Amazon price and the one the
            # pricing agent just proposed, so the two agents don't pull in opposite directions.
            price = r["price"]
            amazon = min(r["amazon_price"], pending.get(r["sku"], r["amazon_price"]))
            if "landed_cost" in (r["assumed"] or ""):
                # Margin floors on an assumed cost aren't safe to act on per SKU: report
                # the parity gaps once, for a person to decide.
                if price < amazon * (1 - PARITY_TOLERANCE):
                    gaps.append(r)
                continue
            c = clamp_price(g, price, price, r["landed_cost"], r["fees"])
            if c.value != price:  # below the margin floor on this channel
                proposed, why = c.value, f"below margin floor after {r['channel']} fees"
            elif price < amazon * (1 - PARITY_TOLERANCE):
                proposed = clamp_price(g, price, amazon, r["landed_cost"], r["fees"]).value
                vs = "proposed Amazon price" if amazon < r["amazon_price"] else "Amazon"
                why = (f"{price:.2f} on {r['channel']} vs {amazon:.2f} {vs}: a lower price "
                       "elsewhere can cost the Amazon Buy Box (fair pricing policy)")
            else:
                continue
            if proposed == price:
                continue
            out.append(Action(
                agent=self.name, kind="update_channel_price",
                target=f"{r['channel']}/{r['sku']}", sku=r["sku"],
                before={"price": price, "channel": r["channel"], "amazon_price": r["amazon_price"]},
                after={"price": proposed,
                       "margin": round((proposed - r["landed_cost"] - r["fees"]) / proposed, 3)},
                reason=why,
            ))
        if gaps:
            gaps.sort(key=lambda r: -((r["daily_velocity"] or 0) + (r["units_30d"] or 0) / 30))
            ex = ", ".join(f"{r['sku']} {r['price']:.2f} vs {r['amazon_price']:.2f}" for r in gaps[:3])
            out.append(Action(
                agent=self.name, kind="review_channel_pricing",
                target=f"{gaps[0]['channel']}/parity", before={"skus": len(gaps)},
                after={"title": f"{len(gaps)} SKUs cost less on {gaps[0]['channel']} than on Amazon",
                       "skus": [r["sku"] for r in gaps[:50]]},
                reason=(f"{len(gaps)} SKUs are priced more than {PARITY_TOLERANCE:.0%} below "
                        f"Amazon on {gaps[0]['channel']} (e.g. {ex}). Amazon can drop the Buy Box "
                        "when the same item is cheaper elsewhere. Decide one rule: match Amazon, "
                        "or keep the site cheaper on purpose."),
            ))
        return out

    def _expand(self, ctx: Context) -> list[Action]:
        low = ctx.shared.get("low_stock", set())
        channels = [r["channel"] for r in
                    ctx.wh.query("SELECT DISTINCT channel FROM channel_listings ORDER BY channel")]
        live = {(r["channel"], r["sku"]) for r in
                ctx.wh.query("SELECT channel, sku FROM channel_listings WHERE listed = 1")}
        # A channel that has none of Amazon's best sellers under the same SKU most likely
        # names SKUs differently; "not listed" would be wrong for all of them.
        top = [p["sku"] for p in ctx.wh.query(
            "SELECT sku FROM products ORDER BY daily_velocity DESC LIMIT 20")]
        channels = [ch for ch in channels
                    if not top or sum((ch, s) in live for s in top) >= len(top) * SKU_MATCH_MIN]
        out: list[Action] = []
        for p in ctx.wh.query("SELECT * FROM products ORDER BY daily_velocity DESC"):
            if (p["daily_velocity"] or 0) < MIN_VELOCITY_TO_EXPAND or p["sku"] in low:
                continue
            for ch in channels:
                if (ch, p["sku"]) in live:
                    continue
                out.append(Action(
                    agent=self.name, kind="list_on_channel", target=f"{ch}/{p['sku']}",
                    sku=p["sku"], before=None,
                    after={"channel": ch, "suggested_price": p["price"]},
                    reason=(f"sells {p['daily_velocity']:g}/day on Amazon, not listed on {ch}; "
                            "list at the Amazon price to keep parity"),
                ))
        return out

    def _leads(self, ctx: Context) -> list[Action]:
        low = ctx.shared.get("low_stock", set())
        out: list[Action] = []
        for lead in ctx.wh.query(
            "SELECT * FROM leads WHERE stage IN ('new','contacted','negotiating') "
            "ORDER BY est_annual_value DESC"
        ):
            last = date.fromisoformat(lead["last_contact"]) if lead["last_contact"] else None
            quiet = (self.today - last).days if last else None
            due = FOLLOW_UP_DAYS[lead["stage"]]
            if quiet is not None and quiet < due:
                continue
            skus = [s.strip() for s in (lead["sku_interest"] or "").split(",") if s.strip()]
            short = [s for s in skus if s in low]
            caution = (f"Don't promise volume on {', '.join(short)}: low stock until the "
                       "reorder lands." if short else "")
            draft = llm.complete(
                "You write short, specific B2B follow-up emails for a consumer products brand "
                "selling wholesale. No pressure tactics, no invented terms or discounts. "
                "Under 120 words.",
                f"Company: {lead['company']}\nContact: {lead['contact']}\nStage: {lead['stage']}\n"
                f"Interested in: {skus}\nNotes: {lead['notes']}\n"
                f"Days since last contact: {quiet}\n{caution}",
                ctx.settings.anthropic_model, max_tokens=400,
            )
            out.append(Action(
                agent=self.name, kind="send_outreach", target=lead["lead_id"],
                sku=skus[0] if skus else None, before={"stage": lead["stage"],
                                                        "last_contact": lead["last_contact"]},
                after={"company": lead["company"], "contact": lead["contact"],
                       "email": lead["email"], "est_annual_value": lead["est_annual_value"],
                       "draft": draft},
                reason=(f"{lead['company']} ({lead['stage']}, est. "
                        f"${lead['est_annual_value']:,.0f}/yr): "
                        + (f"no contact in {quiet} days" if quiet is not None else "never contacted")
                        + (f". {caution}" if caution else "")),
            ))
        return out
