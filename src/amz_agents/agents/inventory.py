"""Inventory agent: flags stockout risk and overstock, proposes reorders.

Runs first: its low-stock list tells ads and pricing which SKUs not to push.
"""

from __future__ import annotations

import math

from ..actions import Action
from .base import Agent, Context, Role, days_of_cover

SAFETY_DAYS = 14
TARGET_DAYS_AFTER_ARRIVAL = 60
OVERSTOCK_DAYS = 180


class InventoryAgent(Agent):
    name = "inventory"
    role = Role(
        mission="Keep every SKU in stock without tying up cash: reorders, stockout and "
                "overstock warnings for the other agents.",
        team="amazon_ops", proposes={"reorder": "S"}, needs=("products", "metrics_daily"),
    )

    def run(self, ctx: Context) -> list[Action]:
        actions: list[Action] = []
        low, over = set(), set()
        for p in ctx.wh.query("SELECT * FROM products"):
            v = p["daily_velocity"] or 0
            if v <= 0 or p["lead_time_days"] is None:
                continue
            cover = days_of_cover(p)
            stock = p["units_on_hand"] + p["units_inbound"]
            reorder_point = v * (p["lead_time_days"] + SAFETY_DAYS)

            if cover < p["lead_time_days"]:
                low.add(p["sku"])  # will stock out before a new PO can land
            if cover > OVERSTOCK_DAYS:
                over.add(p["sku"])

            if stock < reorder_point:
                qty = math.ceil(
                    v * (p["lead_time_days"] + SAFETY_DAYS + TARGET_DAYS_AFTER_ARRIVAL) - stock
                )
                actions.append(
                    Action(
                        agent=self.name,
                        kind="reorder",
                        target=p["sku"],
                        before={"units_available": stock, "days_of_cover": round(cover, 1)},
                        after={"order_units": qty},
                        reason=(
                            f"{cover:.0f} days of cover vs {p['lead_time_days']}d lead time "
                            f"+ {SAFETY_DAYS}d safety at {v:g} units/day"
                        ),
                    )
                )
        ctx.shared["low_stock"] = low
        ctx.shared["overstock"] = over
        return actions
