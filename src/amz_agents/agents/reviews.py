"""Reviews agent (read-only): finds recurring complaints in low-rated reviews.

Feeds product/ops teams (defects) and the listing agent (set expectations).
Never solicits, filters, or responds to reviews.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from .. import llm
from ..actions import Action
from .base import Agent, Context

# Starter themes; extend per catalog. When an API key is set the LLM pass
# also surfaces themes these patterns miss.
THEMES = {
    "leaking": r"\bleak",
    "size/fit": r"\b(fit|too (big|small)|cup holder|size)\b",
    "broken/defective": r"\b(broke|broken|crack|defect)",
    "temperature retention": r"\b(not cold|warm after|doesn'?t keep)",
    "smell/taste": r"\b(smell|taste|odor)",
    "color mismatch": r"\b(color|colour) (is|was) (off|different)",
}
MIN_MENTIONS = 2
LOW_RATING = 3


class ReviewsAgent(Agent):
    name = "reviews"

    def run(self, ctx: Context) -> list[Action]:
        hits: dict[tuple[str, str], list[str]] = defaultdict(list)
        low_by_asin: dict[str, list[str]] = defaultdict(list)
        for r in ctx.wh.query("SELECT * FROM reviews WHERE rating <= ?", (LOW_RATING + 1,)):
            if r["rating"] <= LOW_RATING:
                low_by_asin[r["asin"]].append(r["body"])
            for theme, pat in THEMES.items():
                if re.search(pat, r["body"], re.IGNORECASE):
                    hits[(r["asin"], theme)].append(r["body"])

        actions: list[Action] = []
        themes_by_asin: dict[str, list[str]] = defaultdict(list)
        for (asin, theme), bodies in hits.items():
            themes_by_asin[asin].append(theme)
            if len(bodies) >= MIN_MENTIONS:
                actions.append(
                    Action(
                        agent=self.name,
                        kind="flag_defect",
                        target=asin,
                        before=None,
                        after={"theme": theme, "mentions": len(bodies), "examples": bodies[:3]},
                        reason=f"{len(bodies)} recent reviews mention {theme}",
                    )
                )

        if llm.available():
            for asin, bodies in low_by_asin.items():
                summary = llm.complete(
                    "You analyze Amazon reviews for a brand's product team. Return a JSON list "
                    'of {"theme": str, "mentions": int} for recurring complaints. Only JSON.',
                    "\n".join(f"- {b}" for b in bodies),
                    ctx.settings.anthropic_model,
                    max_tokens=500,
                )
                try:
                    for t in json.loads(summary or "[]"):
                        themes_by_asin[asin].append(t["theme"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

        ctx.shared["review_themes"] = {a: sorted(set(t)) for a, t in themes_by_asin.items()}
        return actions
