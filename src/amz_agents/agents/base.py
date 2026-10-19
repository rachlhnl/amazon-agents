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


class Agent(ABC):
    name: str = "agent"

    @abstractmethod
    def run(self, ctx: Context) -> list[Action]:
        """Read from the warehouse, return proposed actions. Never call APIs directly."""


def days_of_cover(p: dict[str, Any]) -> float:
    v = p.get("daily_velocity") or 0
    stock = (p.get("units_on_hand") or 0) + (p.get("units_inbound") or 0)
    return float("inf") if v <= 0 else stock / v
