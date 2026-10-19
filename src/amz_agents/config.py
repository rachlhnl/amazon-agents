"""Settings loaded from environment variables (and .env if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional at runtime
    pass


class Mode(str, Enum):
    RECOMMEND = "recommend"  # propose only
    APPROVE = "approve"  # queue for human approval
    AUTO = "auto"  # execute within guardrails


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Guardrails:
    target_acos: float = 0.25
    max_bid_change_pct: float = 0.20
    min_bid: float = 0.20
    max_bid: float = 5.00
    max_daily_budget: float = 500.00
    min_margin_pct: float = 0.15
    max_price_change_pct: float = 0.10
    max_budget_change_pct: float = 0.20

    @classmethod
    def from_env(cls) -> Guardrails:
        return cls(
            target_acos=_f("TARGET_ACOS", cls.target_acos),
            max_bid_change_pct=_f("MAX_BID_CHANGE_PCT", cls.max_bid_change_pct),
            min_bid=_f("MIN_BID", cls.min_bid),
            max_bid=_f("MAX_BID", cls.max_bid),
            max_daily_budget=_f("MAX_DAILY_BUDGET", cls.max_daily_budget),
            min_margin_pct=_f("MIN_MARGIN_PCT", cls.min_margin_pct),
            max_price_change_pct=_f("MAX_PRICE_CHANGE_PCT", cls.max_price_change_pct),
            max_budget_change_pct=_f("MAX_BUDGET_CHANGE_PCT", cls.max_budget_change_pct),
        )


@dataclass(frozen=True)
class Settings:
    mode: Mode = Mode.RECOMMEND
    warehouse_path: str = "data/warehouse.db"
    anthropic_model: str = "claude-sonnet-5-5"
    guardrails: Guardrails = field(default_factory=Guardrails)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            mode=Mode(os.getenv("AGENT_MODE", Mode.RECOMMEND.value)),
            warehouse_path=os.getenv("WAREHOUSE_PATH", "data/warehouse.db"),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            guardrails=Guardrails.from_env(),
        )
