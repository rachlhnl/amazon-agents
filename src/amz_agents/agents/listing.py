"""Listing agent: finds converting search terms missing from listing copy and,
when an API key is set, drafts revised title/bullets for human review.

Listing copy is customer-facing, so these actions always need approval.
"""

from __future__ import annotations

import re

from .. import llm
from ..actions import Action
from .base import Agent, Context, Role

STOPWORDS = {"a", "an", "and", "for", "of", "the", "with", "to", "in", "oz"}
MIN_ORDERS = 2


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOPWORDS}


class ListingAgent(Agent):
    name = "listing"
    role = Role(
        mission="Keep listing copy matched to what shoppers search for and honest about "
                "known complaints.",
        team="amazon_ops", proposes={"update_listing_copy": "M"},
        needs=("products", "search_terms"),
    )

    def run(self, ctx: Context) -> list[Action]:
        themes = ctx.shared.get("review_themes", {})
        out: list[Action] = []
        for p in ctx.wh.query("SELECT * FROM products"):
            terms = ctx.wh.query(
                "SELECT search_term, SUM(orders) AS orders FROM search_terms "
                "WHERE sku=? GROUP BY search_term HAVING SUM(orders) >= ? ORDER BY orders DESC",
                (p["sku"], MIN_ORDERS),
            )
            title_words = _words(p["title"] or "")
            gaps = sorted(
                {w for t in terms for w in _words(t["search_term"])} - title_words
            )
            asin_themes = themes.get(p["asin"], [])
            if not gaps and not asin_themes:
                continue

            draft = None
            if llm.available():
                draft = llm.complete(
                    "You write Amazon product listings that comply with Amazon's style guide: "
                    "no promotional claims, no competitor names, title under 200 characters. "
                    "Return a title and 5 bullets.",
                    f"Current title: {p['title']}\n"
                    f"Converting search terms: {[t['search_term'] for t in terms]}\n"
                    f"Missing words to work in naturally: {gaps}\n"
                    f"Recurring review complaints to set expectations on honestly: {asin_themes}",
                    ctx.settings.anthropic_model,
                )

            out.append(
                Action(
                    agent=self.name,
                    kind="update_listing_copy",
                    target=p["sku"],
                    before={"title": p["title"]},
                    after={"missing_terms": gaps, "review_themes": asin_themes, "draft": draft},
                    reason=(
                        f"{len(gaps)} converting search-term words absent from title"
                        + (f"; review themes: {', '.join(asin_themes)}" if asin_themes else "")
                    ),
                )
            )
        return out
