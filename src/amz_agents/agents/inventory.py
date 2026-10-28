"""Inventory agent: flags stockout risk and overstock, proposes reorders.

Runs first: its low-stock list tells ads and pricing which SKUs not to push.
"""

from __future__ import annotations

import math
from collections import defaultdict

from ..actions import Action
from ..styles import name_of, styles
from .base import Agent, Context, Role, days_of_cover

SAFETY_DAYS = 14
TARGET_DAYS_AFTER_ARRIVAL = 60
OVERSTOCK_DAYS = 180
OVERSTOCK_MIN_VALUE = 1000.0  # $ of stock at list price before overstock is worth acting on


class InventoryAgent(Agent):
    name = "inventory"
    role = Role(
        mission="Keep every SKU in stock without tying up cash: one purchase plan per style, "
                "stockout and overstock warnings for the other agents.",
        team="amazon_ops", proposes={"reorder": "S"}, needs=("products", "metrics_daily"),
    )

    def run(self, ctx: Context) -> list[Action]:
        low, over = set(), set()
        lines: dict[str, list[dict]] = defaultdict(list)
        style = styles(ctx.wh)
        for p in ctx.wh.query("SELECT * FROM products"):
            v = p["daily_velocity"] or 0
            if v <= 0 or p["lead_time_days"] is None or p["units_on_hand"] is None:
                continue  # no stock report row (not at FBA, or not loaded): can't judge
            cover = days_of_cover(p)
            stock = p["units_on_hand"] + (p["units_inbound"] or 0)
            reorder_point = v * (p["lead_time_days"] + SAFETY_DAYS)

            if cover < p["lead_time_days"]:
                low.add(p["sku"])  # will stock out before a new PO can land
            if cover > OVERSTOCK_DAYS and stock * (p["price"] or 0) >= OVERSTOCK_MIN_VALUE:
                over.add(p["sku"])

            if stock < reorder_point:
                qty = math.ceil(v * (p["lead_time_days"] + SAFETY_DAYS + TARGET_DAYS_AFTER_ARRIVAL)
                                - stock)
                gap = max(0.0, p["lead_time_days"] + SAFETY_DAYS - cover)
                unit_profit = (p["price"] or 0) - (p["landed_cost"] or 0) - (p["fees"] or 0)
                lines[style.get(p["sku"], (p["sku"],))[0]].append({
                    "sku": p["sku"], "order_units": qty, "on_hand": p["units_on_hand"],
                    "inbound": p["units_inbound"] or 0, "per_day": round(v, 1),
                    "days_of_cover": round(cover, 1), "lead_time_days": p["lead_time_days"],
                    "at_risk": round(gap * v * max(unit_profit, 0), 2),
                    "assumed": bool((p["assumed"] or "").strip()),
                    "title": p["title"],
                })
        ctx.shared["low_stock"] = low
        ctx.shared["overstock"] = over
        return [self._plan(style, ls) for style, ls in lines.items()]

    def _plan(self, style: str, ls: list[dict]) -> Action:
        """One purchase plan per style: what a buyer actually sends a factory. A style
        with one variant stays a plain SKU reorder."""
        if len(ls) == 1:
            r = ls[0]
            return Action(
                agent=self.name, kind="reorder", target=r["sku"],
                before={"units_available": r["on_hand"] + r["inbound"],
                        "days_of_cover": r["days_of_cover"]},
                after={"order_units": r["order_units"],
                       "when": 1 if r["on_hand"] <= 0 and r["per_day"] >= 1 else None},
                reason=(f"{r['days_of_cover']:.0f} days of cover vs {r['lead_time_days']}d lead time "
                        f"+ {SAFETY_DAYS}d safety at {r['per_day']:g} units/day"),
            )
        ls.sort(key=lambda r: (r["on_hand"] > 0, -r["per_day"]))
        out = [r for r in ls if r["on_hand"] <= 0]
        units = sum(r["order_units"] for r in ls)
        risk = sum(r["at_risk"] for r in ls)
        assumed = any(r["assumed"] for r in ls)
        name = name_of(ls[0]["title"]) or style
        title = (f"Reorder {name}: {units:,} units across {len(ls)} sizes/colors"
                 + (f", {len(out)} out of stock" if out else ""))
        lead = ls[0]["lead_time_days"]
        return Action(
            agent=self.name, kind="reorder", target=f"style/{style}",
            sku=ls[0]["sku"] if len(ls) == 1 else None,
            before={"units_available": sum(r["on_hand"] + r["inbound"] for r in ls),
                    "out_of_stock": len(out)},
            after={"title": title, "order_units": units, "lines": ls[:60],
                   "impact_usd": round(risk, 2) or None,
                   "confidence": "low" if assumed else "high",
                   # out of stock on a real seller can't wait for the next review
                   "when": 1 if any(r["per_day"] >= 1 for r in out) else None},
            reason=(f"{len(ls)} variants will run out before a new order can land "
                    f"({lead}d lead time{' (assumed)' if assumed else ''} + {SAFETY_DAYS}d safety)"
                    + (f"; out of stock now: {', '.join(r['sku'] for r in out[:4])}"
                       f"{'…' if len(out) > 4 else ''}" if out else "")
                    + f". About ${risk:,.0f} of profit at risk."),
        )
