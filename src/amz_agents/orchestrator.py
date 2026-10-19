"""Runs agents in dependency order, submits their actions, writes a daily brief."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path

from . import llm
from .actions import Action, ActionLog
from .agents import AdsAgent, InventoryAgent, ListingAgent, PricingAgent, ReviewsAgent
from .agents.base import Agent, Context

# Order matters: inventory and reviews publish findings that later agents read.
DEFAULT_AGENTS: list[type[Agent]] = [
    InventoryAgent, ReviewsAgent, PricingAgent, AdsAgent, ListingAgent,
]


class Orchestrator:
    def __init__(self, ctx: Context, log: ActionLog, agents: list[Agent] | None = None):
        self.ctx = ctx
        self.log = log
        self.agents = agents or [a() for a in DEFAULT_AGENTS]

    def run(self) -> list[tuple[int, Action]]:
        submitted: list[tuple[int, Action]] = []
        seen: set[tuple[str, str]] = set()
        for agent in self.agents:
            for action in agent.run(self.ctx):
                key = (action.kind, action.target + json.dumps(action.after, sort_keys=True))
                if key in seen:
                    continue
                seen.add(key)
                submitted.append((self.log.submit(action), action))
        return submitted

    def brief(self, submitted: list[tuple[int, Action]], out_dir: str = "reports") -> Path:
        counts = Counter(a.kind for _, a in submitted)
        lines = [
            f"# Daily brief: {date.today().isoformat()}",
            "",
            f"Mode: **{self.ctx.settings.mode.value}** · {len(submitted)} actions",
            "",
            "| # | Agent | Action | Target | Change | Why |",
            "|---|---|---|---|---|---|",
        ]
        for action_id, a in submitted:
            change = f"{_fmt(a.before)} → {_fmt(a.after)}" if a.before else _fmt(a.after)
            lines.append(
                f"| {action_id} | {a.agent} | {a.kind} | {a.target} | {change} | {a.reason} |"
            )
        lines += ["", "Summary: " + ", ".join(f"{k} ×{v}" for k, v in counts.items())]

        narrative = llm.complete(
            "You are the head of e-commerce writing a 5-sentence morning note for the team. "
            "Lead with the most important risk or opportunity. Plain language, no hype.",
            "\n".join(lines),
            self.ctx.settings.anthropic_model,
            max_tokens=400,
        )
        if narrative:
            lines[1:1] = ["", narrative.strip()]

        path = Path(out_dir) / f"brief-{date.today().isoformat()}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
        return path


def _fmt(v: object) -> str:
    if isinstance(v, dict):
        return ", ".join(
            f"{k}={x}" for k, x in v.items() if k not in ("examples", "draft") and x not in (None, [])
        )
    return str(v)
