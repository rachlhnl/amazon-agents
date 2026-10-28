"""Marketing agent: paid traffic and email outside Amazon.

Judges each external campaign (Meta, Google, TikTok...) by the profit it brings
back per dollar spent, counting Amazon's Brand Referral Bonus for campaigns
that send shoppers to Amazon. Scales winners, cuts losers, pauses campaigns
that would sell stock the inventory agent says is running out, and drafts an
email campaign for overstocked SKUs.

Budgets pass through guardrails; emails are customer-facing and need a human.
"""

from __future__ import annotations

from .. import llm
from ..actions import Action
from ..guardrails import clamp_budget_change
from ..styles import styles
from .base import Agent, Context, Role

# Amazon credits brand-registered sellers a share of sales from external traffic
# tagged with Amazon Attribution. ~10% on average; check the current rate per
# category in Seller Central.
BRAND_REFERRAL_BONUS = 0.10

MIN_CLICKS = 50  # don't judge a campaign on less
SCALE_AT = 1.5  # profit per $1 of spend at or above this -> raise budget
CUT_BELOW = 1.0  # below this the campaign loses money -> cut budget
PAUSE_AFTER_CLICKS = 150  # this many clicks and no orders -> pause
MIN_CHANGE_PCT = 0.03


def profit_per_dollar(c: dict, unit_margin_pct: float) -> float | None:
    """Contribution (sales x margin, + referral bonus for Amazon traffic) per $1 spent."""
    if not c["spend_30d"]:
        return None
    bonus = BRAND_REFERRAL_BONUS if c["destination"] == "amazon" else 0.0
    return c["sales_30d"] * (unit_margin_pct + bonus) / c["spend_30d"]


def unit_margin_pct(ctx: Context, sku: str, destination: str) -> float | None:
    """Margin per unit on the channel the campaign sends shoppers to."""
    p = ctx.wh.query("SELECT * FROM products WHERE sku=?", (sku,))
    if not p:
        return None
    p = p[0]
    price, fees = p["price"], p["fees"]
    if p["landed_cost"] is None:
        return None
    if destination != "amazon":
        ch = ctx.wh.query(
            "SELECT price, fees FROM channel_listings WHERE channel=? AND sku=?", (destination, sku)
        )
        if not ch:
            return None
        price, fees = ch[0]["price"], ch[0]["fees"]
    if not price or fees is None:
        return None
    return (price - p["landed_cost"] - fees) / price


def draws_on_fba_stock(ctx: Context, c: dict) -> bool:
    if c["destination"] == "amazon":
        return True
    ch = ctx.wh.query(
        "SELECT fulfillment FROM channel_listings WHERE channel=? AND sku=?",
        (c["destination"], c["sku"]),
    )
    return bool(ch) and ch[0]["fulfillment"] == "mcf"


class MarketingAgent(Agent):
    name = "marketing"
    role = Role(
        mission="Spend outside Amazon (Meta, Google, TikTok, email) where it returns "
                "profit, and stop spend that sells stock we're short of.",
        team="marketing",
        proposes={"update_campaign_budget": "XS", "pause_campaign": "XS",
                  "email_campaign": "M"},
        needs=("external_campaigns",),
    )

    def run(self, ctx: Context) -> list[Action]:
        return self._campaigns(ctx) + self._email(ctx)

    def _campaigns(self, ctx: Context) -> list[Action]:
        g = ctx.settings.guardrails
        low = ctx.shared.get("low_stock", set())
        out: list[Action] = []
        for c in ctx.wh.query("SELECT * FROM external_campaigns WHERE status='enabled'"):
            margin = unit_margin_pct(ctx, c["sku"], c["destination"])
            if margin is None:
                continue
            ppd = profit_per_dollar(c, margin)
            before = {
                "daily_budget": c["daily_budget"], "name": c["name"],
                "platform": c["platform"], "destination": c["destination"],
                "profit_per_dollar": round(ppd, 2) if ppd is not None else None,
            }

            if c["sku"] in low and draws_on_fba_stock(ctx, c):
                out.append(self._pause(c, before, "low stock: don't buy demand for a SKU about "
                                       "to stock out; organic sales will use the stock"))
                continue
            if c["clicks_30d"] < MIN_CLICKS or ppd is None or not c["daily_budget"]:
                continue
            if c["orders_30d"] == 0 and c["clicks_30d"] >= PAUSE_AFTER_CLICKS:
                out.append(self._pause(
                    c, before, f"{c['clicks_30d']} clicks, ${c['spend_30d']:.2f} spend, 0 orders"
                ))
                continue

            if ppd >= SCALE_AT:
                proposed = c["daily_budget"] * (1 + g.max_budget_change_pct)
            elif ppd < CUT_BELOW:
                proposed = c["daily_budget"] * (1 - g.max_budget_change_pct)
            else:
                continue
            cl = clamp_budget_change(g, c["daily_budget"], proposed)
            if abs(cl.value - c["daily_budget"]) / c["daily_budget"] < MIN_CHANGE_PCT:
                continue
            out.append(Action(
                agent=self.name, kind="update_campaign_budget", target=c["campaign_id"],
                sku=c["sku"], before=before,
                after={"daily_budget": cl.value},
                reason=(f"${ppd:.2f} profit per $1 spent over 30 days "
                        f"({'scale' if ppd >= SCALE_AT else 'losing money'})"),
                notes=cl.note,
            ))
        return out

    def _pause(self, c: dict, before: dict, why: str) -> Action:
        return Action(
            agent=self.name, kind="pause_campaign", target=c["campaign_id"], sku=c["sku"],
            before=before, after={"status": "paused"}, reason=why,
        )

    def _email(self, ctx: Context) -> list[Action]:
        """Overstocked styles with no campaign behind them: draft one email per style to
        the customer list (an email sells a style, not one size of it)."""
        over = ctx.shared.get("overstock", set())
        promoted = {
            r["sku"] for r in ctx.wh.query("SELECT sku FROM external_campaigns WHERE status='enabled'")
        }
        style = styles(ctx.wh)
        groups: dict[str, list[dict]] = {}
        for p in ctx.wh.query("SELECT * FROM products ORDER BY sku"):
            if p["sku"] in over and p["sku"] not in promoted:
                groups.setdefault(style.get(p["sku"], (p["sku"],))[0], []).append(p)
        out: list[Action] = []
        for key, ps in groups.items():
            name = style.get(ps[0]["sku"], (key, key))[1]
            ps.sort(key=lambda p: -(p["units_on_hand"] or 0))
            units = sum(p["units_on_hand"] or 0 for p in ps)
            draft = llm.complete(
                "You write short, plain marketing emails for a consumer brand's own customer "
                "list. No false urgency, no invented discounts or claims. Return a subject line "
                "and a body under 120 words.",
                f"Product: {ps[0]['title']}\nWe have plenty in stock. Suggest it to existing "
                "customers who might need one (e.g. a replacement or a gift).",
                ctx.settings.anthropic_model, max_tokens=400,
            )
            one = len(ps) == 1
            out.append(Action(
                agent=self.name, kind="email_campaign", target=ps[0]["sku"] if one else f"style/{key}",
                sku=ps[0]["sku"] if one else None, before=None,
                after={"audience": "customer list", "draft": draft,
                       **({} if one else {"title": f"Email the list about {name}: {units:,} units "
                                                   f"overstocked across {len(ps)} variants",
                                          "skus": [p["sku"] for p in ps]})},
                reason=f"overstock ({units} on hand) and no paid campaign behind it",
            ))
        return out
