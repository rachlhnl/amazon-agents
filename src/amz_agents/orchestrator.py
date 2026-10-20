"""Runs the agents in dependency order, hands their proposals to the chief of
staff for review and ranking, submits the plan, and writes the brief."""

from __future__ import annotations

import json
from pathlib import Path

from .actions import Action, ActionLog
from .agents import (
    AdsAgent,
    CreativeAgent,
    DataAgent,
    InventoryAgent,
    ListingAgent,
    MarketingAgent,
    PricingAgent,
    ProductAgent,
    ReviewsAgent,
    SalesAgent,
    UXAgent,
)
from .agents.base import TEAMS, Agent, Context
from .agents.chief_of_staff import ChiefOfStaff

# Order matters: inventory and reviews publish findings that later agents read.
# Marketing and sales work outside Amazon and use the same findings. The data agent
# runs last so it can link its opportunities to what the others already proposed.
DEFAULT_AGENTS: list[type[Agent]] = [
    InventoryAgent, ReviewsAgent, PricingAgent, AdsAgent, ListingAgent,
    ProductAgent,  # fit, store stock and launches, from the market tracker's findings
    MarketingAgent, SalesAgent,
    CreativeAgent,  # reviews the drafts above against brand.toml
    UXAgent,  # annotates marketing's budget proposals when the landing page is the problem
    DataAgent,  # last: reviews everyone's proposals
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
        self.chief = chief or ChiefOfStaff(ctx, agents=self.agents)
        self.outcomes: list[dict] = []

    def run(self) -> list[tuple[int, Action]]:
        # Judge last week's changes first, so today's brief reflects them.
        self.outcomes = self.chief.evaluate_outcomes()

        proposals: list[Action] = []
        seen: set[tuple[str, str]] = set()
        self.skipped = 0
        self.ctx.shared["roster"] = {a.name: a.role for a in self.agents}
        self.ctx.shared["proposals"] = proposals  # later agents see earlier proposals
        health: dict[str, dict] = {}
        for agent in self.agents:
            h = health[agent.name] = {"proposed": 0, "error": None, "undeclared": set()}
            try:
                actions = agent.run(self.ctx)
            except Exception as e:  # noqa: BLE001 - one broken agent shouldn't stop the run
                h["error"] = repr(e)
                continue
            for action in actions:
                h["proposed"] += 1
                if action.kind not in agent.role.proposes:
                    h["undeclared"].add(action.kind)
                key = (action.kind, action.target + json.dumps(action.after, sort_keys=True))
                if key in seen:
                    continue
                seen.add(key)
                # Don't queue the same change twice across runs.
                if self.log.already_open_or_done(action):
                    self.skipped += 1
                    continue
                proposals.append(action)

        self.chief.health = health
        plan = self.chief.plan(self.chief.review(proposals))
        submitted = [(self.log.submit(a), a) for a in plan]
        self._record_run(len(submitted))
        return submitted

    def _record_run(self, submitted: int) -> None:
        """Keep each run and each agent's health for the tracker."""
        run_id = self.ctx.wh.execute(
            "INSERT INTO runs (finished, mode, submitted, skipped) "
            "VALUES (datetime('now'), ?, ?, ?)",
            (self.ctx.settings.mode.value, submitted, self.skipped),
        )
        self.ctx.wh.insert_many("agent_runs", [
            {"run_id": run_id, "agent": h["agent"], "team": h["role"].team,
             "proposed": h["proposed"], "problems": json.dumps(h["problems"])}
            for h in self.chief.agent_health()
        ])

    def brief(self, submitted: list[tuple[int, Action]], out_dir: str = "reports") -> Path:
        path = Path(out_dir) / f"brief-{self.chief.today.isoformat()}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.chief.brief(submitted, self.outcomes))
        return path

    def team_briefs(
        self, submitted: list[tuple[int, Action]], out_dir: str = "reports"
    ) -> dict[str, Path]:
        """Write each team's part to reports/<team>/brief-<date>.md."""
        out = {}
        for team in TEAMS:
            text = self.chief.team_brief(team, submitted)
            if text:
                path = Path(out_dir) / team / f"brief-{self.chief.today.isoformat()}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                out[team] = path
        return out
