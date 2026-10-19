"""Pricing agent: protects margin, competes for the Buy Box, and uses price
to manage sell-through (slow down before a stockout, speed up overstock)."""

from __future__ import annotations

from ..actions import Action
from ..guardrails import clamp_price, price_floor
from .base import Agent, Context

LOW_STOCK_RAISE = 0.05
OVERSTOCK_CUT = 0.05


class PricingAgent(Agent):
    name = "pricing"

    def run(self, ctx: Context) -> list[Action]:
        g = ctx.settings.guardrails
        low = ctx.shared.get("low_stock", set())
        over = ctx.shared.get("overstock", set())
        out: list[Action] = []
        proposed_prices: dict[str, float] = {}
        for p in ctx.wh.query("SELECT * FROM products"):
            price, cost, fees = p["price"], p["landed_cost"], p["fees"]
            if None in (price, cost, fees):
                continue  # can't price without costs; the brief lists the gap
            floor = price_floor(g, cost, fees)

            if p["sku"] in low:
                proposed, why = price * (1 + LOW_STOCK_RAISE), "low stock: slow sell-through to avoid stockout"
            elif p["buybox_price"] and p["buybox_price"] < price:
                proposed, why = p["buybox_price"], f"Buy Box at {p['buybox_price']:.2f}"
            elif p["sku"] in over:
                proposed, why = price * (1 - OVERSTOCK_CUT), "overstock: speed up sell-through"
            elif price < floor:
                proposed, why = floor, f"below margin floor {floor:.2f}"
            else:
                continue

            c = clamp_price(g, price, proposed, cost, fees)
            if c.value == price:
                continue
            proposed_prices[p["sku"]] = c.value
            margin = (c.value - cost - fees) / c.value
            out.append(
                Action(
                    agent=self.name,
                    kind="update_price",
                    target=p["sku"],
                    before={"price": price},
                    after={"price": c.value, "margin": round(margin, 3)},
                    reason=why,
                    notes=c.note,
                )
            )
        # The sales agent keeps off-Amazon prices in line with these.
        ctx.shared["amazon_price_proposals"] = proposed_prices
        return out
