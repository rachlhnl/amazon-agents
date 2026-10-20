"""Product agent: fit, sizing and returns, keeping best sellers in stock on our own
store, and what to launch next.

It works from the market tracker's findings (market_recs, refreshed by `ingest`),
which come from dagacci.com's public catalog and stock, its Judge.me reviews and
the other scrub brands. Each finding in its areas becomes a piece of work for the
Product & merchandising team, with the steps to take and the KPI that shows it
worked. Everything here is for a person: size charts, patterns, factory orders and
launches aren't things an API should change.
"""

from __future__ import annotations

import json
import re

from ..actions import Action
from .base import Agent, Context, Role

# Market tracker area -> what this agent proposes.
KINDS = {"Stock": "restock_style", "Fit & returns": "fix_fit", "Product": "improve_product",
         "Reviews": "improve_product"}
LAUNCH = re.compile(r"\blaunch|\bnew (styles?|colou?rs?)\b|\bline has room\b|\bwiden colou?rs\b",
                    re.IGNORECASE)


def owned(area: str) -> bool:
    """Market tracker areas this agent works; the data agent routes the rest."""
    return area in KINDS


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


class ProductAgent(Agent):
    name = "product"
    role = Role(
        mission="Fix what buyers say is wrong (fit and sizing first), keep best sellers in "
                "stock on dagacci.com, and keep the range fresh with new styles and colors.",
        team="product",
        proposes={"fix_fit": "M", "restock_style": "S", "improve_product": "M",
                  "plan_launch": "L"},
        needs=("market_recs",),
    )

    def run(self, ctx: Context) -> list[Action]:
        out = []
        for r in ctx.wh.query("SELECT * FROM market_recs ORDER BY rank"):
            if not owned(r["area"]):
                continue
            kind = KINDS[r["area"]]
            if kind == "improve_product" and LAUNCH.search(r["title"]):
                kind = "plan_launch"
            # The title names the product or the gap, so it stays the same from day to
            # day while the numbers in `see` move.
            out.append(Action(
                agent=self.name, kind=kind, target=_slug(r["title"]), before=None,
                after={"title": r["title"], "when": r["rank"], "area": r["area"],
                       "steps": json.loads(r["actions"] or "[]"), "kpi": r["kpi"],
                       "kpi_target": r["kpi_target"], "url": r["url"], "source": "market"},
                reason=f"{r['see']} {r['why']}".strip(),
                # Kept out of `after` so a moving number doesn't re-queue finished work.
                notes=f"{r['kpi']} now {r['kpi_now']}" if r["kpi"] and r["kpi_now"] else "",
            ))
        return out
