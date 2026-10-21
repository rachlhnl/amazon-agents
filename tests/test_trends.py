from datetime import date, timedelta

from amz_agents.agents.base import Context
from amz_agents.agents.data import DataAgent
from amz_agents.config import Settings
from amz_agents.data.warehouse import Warehouse

TODAY = date(2026, 10, 21)


def _wh(daily):
    """daily(ago, size) -> units for two sizes of one style over 28 days."""
    wh = Warehouse(":memory:")
    wh.insert_many("products", [{"sku": f"KV_1_NVY_{s}", "asin": s, "title": "Dagacci Scrub Set, Navy",
                                 "price": 25.0, "landed_cost": 5.0, "fees": 12.0, "lead_time_days": 30,
                                 "units_on_hand": 500, "units_inbound": 0, "daily_velocity": 20}
                                for s in ("M", "L")])
    wh.insert_many("metrics_daily", [
        {"date": (TODAY - timedelta(days=ago)).isoformat(), "sku": f"KV_1_NVY_{s}",
         "units": daily(ago, s), "revenue": daily(ago, s) * 25.0, "ad_spend": 0.0, "ad_sales": 0.0}
        for ago in range(1, 29) for s in ("M", "L")])
    return wh


def _trends(wh):
    agent = DataAgent(today=TODAY)
    agent.ctx = Context(wh, Settings())
    agent.products = {p["sku"]: p for p in wh.query("SELECT * FROM products")}
    agent.proposals = []
    return agent._trends()


def test_a_promotion_week_doesnt_read_as_a_collapse():
    # a Prime Day week (days 8-14 ago) at 3x, otherwise flat
    os = _trends(_wh(lambda ago, s: 60 if 8 <= ago <= 14 else 20))
    assert not [o for o in os if "down" in o.title]


def test_drops_are_reported_once_per_style():
    os = _trends(_wh(lambda ago, s: 8 if ago <= 7 else 20))
    downs = [o for o in os if "down" in o.title]
    assert len(downs) == 1 and "2 variants down 60%" in downs[0].title and downs[0].key
