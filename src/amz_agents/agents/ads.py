"""Ads agent: bid tuning, search-term harvesting, and negation for Sponsored Products.

Rules are deterministic and conservative; every number passes through guardrails.
"""

from __future__ import annotations

from ..actions import Action
from ..guardrails import clamp_bid
from .base import Agent, Context, Role

MIN_CLICKS_FOR_BID_CHANGE = 15
MIN_CHANGE_PCT = 0.03  # ignore noise-level adjustments
NEGATE_AFTER_CLICKS = 20
HARVEST_MIN_ORDERS = 2


class AdsAgent(Agent):
    name = "ads"
    role = Role(
        mission="Run Sponsored Products efficiently: tune bids to target ACoS, harvest "
                "converting search terms, negate wasted ones.",
        team="amazon_ops",
        proposes={"update_bid": "XS", "add_keyword": "XS", "add_negative": "XS"},
        needs=("keywords", "search_terms"),
    )

    def run(self, ctx: Context) -> list[Action]:
        return self._bids(ctx) + self._harvest_and_negate(ctx)

    def _bids(self, ctx: Context) -> list[Action]:
        g = ctx.settings.guardrails
        low_stock = ctx.shared.get("low_stock", set())
        out: list[Action] = []
        for k in ctx.wh.query("SELECT * FROM keywords"):
            if k["clicks"] < MIN_CLICKS_FOR_BID_CHANGE:
                continue
            bid = k["bid"]
            if k["orders"] == 0:
                proposed, why = bid * (1 - g.max_bid_change_pct), f"{k['clicks']} clicks, 0 orders"
            else:
                acos = k["spend"] / k["sales"]
                proposed = bid * g.target_acos / acos
                why = f"ACoS {acos:.0%} vs target {g.target_acos:.0%}"

            if proposed > bid and k["sku"] in low_stock:
                continue  # don't buy more demand for a SKU about to stock out

            c = clamp_bid(g, bid, proposed)
            if abs(c.value - bid) / bid < MIN_CHANGE_PCT:
                continue
            out.append(
                Action(
                    agent=self.name,
                    kind="update_bid",
                    target=k["keyword_id"],
                    before={"bid": bid, "keyword": k["keyword"]},
                    after={"bid": c.value},
                    reason=why,
                    notes=c.note,
                )
            )
        return out

    def _harvest_and_negate(self, ctx: Context) -> list[Action]:
        g = ctx.settings.guardrails
        exact = {
            (r["sku"], r["keyword"].lower())
            for r in ctx.wh.query("SELECT sku, keyword FROM keywords WHERE match_type='exact'")
        }
        out: list[Action] = []
        for t in ctx.wh.query(
            "SELECT * FROM search_terms WHERE match_type IN ('broad','phrase','auto')"
        ):
            term = t["search_term"].lower()
            if t["orders"] == 0 and t["clicks"] >= NEGATE_AFTER_CLICKS:
                out.append(
                    Action(
                        agent=self.name,
                        kind="add_negative",
                        target=f"{t['campaign_id']}/{t['ad_group_id']}",
                        before=None,
                        after={"keyword": term, "match_type": "negativeExact"},
                        reason=f"'{term}': {t['clicks']} clicks, ${t['spend']:.2f} spend, 0 orders",
                    )
                )
            elif (
                t["orders"] >= HARVEST_MIN_ORDERS
                and t["spend"] / t["sales"] <= g.target_acos
                and (t["sku"], term) not in exact
            ):
                # Start the exact bid at what one click is worth at target ACoS.
                value_per_click = t["sales"] / t["clicks"]
                bid = round(min(max(value_per_click * g.target_acos, g.min_bid), g.max_bid), 2)
                out.append(
                    Action(
                        agent=self.name,
                        kind="add_keyword",
                        target=t["sku"],
                        before=None,
                        after={"keyword": term, "match_type": "exact", "bid": bid},
                        reason=(
                            f"'{term}': {t['orders']} orders at "
                            f"{t['spend'] / t['sales']:.0%} ACoS in {t['match_type']}"
                        ),
                    )
                )
        return out
