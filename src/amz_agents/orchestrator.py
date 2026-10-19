"""Runs the agents in dependency order, hands their proposals to the chief of
staff for review and ranking, submits the plan, and writes the brief."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .actions import Action, ActionLog
from .agents import AdsAgent, InventoryAgent, ListingAgent, PricingAgent, ReviewsAgent
from .agents.base import Agent, Context
from .agents.chief_of_staff import ChiefOfStaff

# Order matters: inventory and reviews publish findings that later agents read.
DEFAULT_AGENTS: list[type[Agent]] = [
    InventoryAgent, ReviewsAgent, PricingAgent, AdsAgent, ListingAgent,
]


class Orchestrator:
    def __init__(
        self,
        ctx: Context,
        log: ActionLog,
        agents: list[Agent] | None = None,
        chief: ChiefOfStaff | None = None,
    ):
        self.ctx = ctx
        self.log = log
        self.agents = agents or [a() for a in DEFAULT_AGENTS]
        self.chief = chief or ChiefOfStaff(ctx)
        self.outcomes: list[dict] = []

    def run(self) -> list[tuple[int, Action]]:
        # Judge last week's changes first, so today's brief reflects them.
        self.outcomes = self.chief.evaluate_outcomes()

        proposals: list[Action] = []
        seen: set[tuple[str, str]] = set()
        for agent in self.agents:
            for action in agent.run(self.ctx):
                key = (action.kind, action.target + json.dumps(action.after, sort_keys=True))
                if key not in seen:
                    seen.add(key)
                    proposals.append(action)

        plan = self.chief.plan(self.chief.review(proposals))
        return [(self.log.submit(a), a) for a in plan]

    def brief(self, submitted: list[tuple[int, Action]], out_dir: str = "reports") -> Path:
        path = Path(out_dir) / f"brief-{date.today().isoformat()}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.chief.brief(submitted, self.outcomes))
        return path
