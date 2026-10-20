from pathlib import Path

import pytest

from amz_agents.agents.base import Context
from amz_agents.config import Settings
from amz_agents.data.sample import seed
from amz_agents.data.warehouse import Warehouse

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GOALS_PATH", str(ROOT / "goals.toml"))
    monkeypatch.setenv("PLAN_PATH", str(ROOT / "tests" / "no-plan.toml"))  # tests opt in
    wh = Warehouse()
    seed(wh)
    return Context(wh, Settings())
