from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..actions import Action
from ..config import Settings
from ..data.warehouse import Warehouse


@dataclass
class Context:
    wh: Warehouse
    settings: Settings
    # Shared scratchpad: earlier agents leave findings for later ones
    # (e.g. inventory flags low-stock SKUs so ads/pricing don't push them).
    shared: dict[str, Any] = field(default_factory=dict)


# Teams that own agents' work. The chief of staff groups the brief by team and,
# when BRIEF_TO_<TEAM> is set (e.g. BRIEF_TO_MARKETING), emails each team its part.
TEAMS = {
    "amazon_ops": "Amazon operations",
    "marketing": "Marketing",
    "sales": "Sales & wholesale",
    "data": "Data & insights",
}

# Level of effort -> hours of a person's time, for ROI. XS is a click to approve
# a change the system then makes; L is days of work.
EFFORT_HOURS = {"XS": 0.1, "S": 0.5, "M": 2.0, "L": 8.0, "XL": 24.0}


@dataclass(frozen=True)
class Role:
    """What an agent is for. The chief of staff reads this to route, prioritize and
    check on each agent's work."""

    mission: str
    team: str  # key of TEAMS
    proposes: dict[str, str]  # action kind -> level of effort to carry it out
    needs: tuple[str, ...] = ()  # warehouse tables it can't work without


class Agent(ABC):
    name: str = "agent"
    role: Role

    @abstractmethod
    def run(self, ctx: Context) -> list[Action]:
        """Read from the warehouse, return proposed actions. Never call APIs directly."""


def days_of_cover(p: dict[str, Any]) -> float:
    v = p.get("daily_velocity") or 0
    stock = (p.get("units_on_hand") or 0) + (p.get("units_inbound") or 0)
    return float("inf") if v <= 0 else stock / v
