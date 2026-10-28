from amz_agents import assumptions
from amz_agents.agents.base import Context
from amz_agents.agents.inventory import InventoryAgent
from amz_agents.config import Settings
from amz_agents.data.warehouse import Warehouse
from amz_agents.styles import key_of


def _wh():
    wh = Warehouse(":memory:")
    wh.insert_many("products", [
        {"sku": f"KV_100_NVY_{s}", "asin": f"B{s}", "title": f"Dagacci Scrub Set, Navy, {s}",
         "price": 25.0, "units_on_hand": oh, "units_inbound": 0, "daily_velocity": v}
        for s, oh, v in [("M", 0, 10.0), ("L", 40, 5.0), ("S", 5000, 1.0)]])
    return wh


def test_blanks_get_flagged_averages_and_real_costs_clear_them(tmp_path, monkeypatch):
    wh = _wh()
    assert assumptions.apply(wh) == 3
    p = wh.query("SELECT * FROM products WHERE sku='KV_100_NVY_M'")[0]
    assert p["fees"] == round(25 * 0.487, 2) and p["landed_cost"] == 5.0
    assert p["lead_time_days"] == 45 and assumptions.is_assumed(p, "fees")
    costs = tmp_path / "costs.csv"
    costs.write_text("sku,landed_cost,lead_time_days\nKV_100_NVY_M,6.5,30\n")
    monkeypatch.setenv("COSTS_CSV", str(costs))
    from amz_agents.ingest import ingest_costs_csv

    ingest_costs_csv(wh, costs)
    p = wh.query("SELECT * FROM products WHERE sku='KV_100_NVY_M'")[0]
    assert p["landed_cost"] == 6.5 and p["assumed"] == "fees"
    assumptions.apply(wh)  # a later run doesn't overwrite real values
    assert wh.query("SELECT landed_cost FROM products WHERE sku='KV_100_NVY_M'")[0]["landed_cost"] == 6.5


def test_reorders_come_as_one_plan_per_style():
    wh = _wh()
    assumptions.apply(wh)
    acts = InventoryAgent().run(Context(wh, Settings()))
    assert len(acts) == 1
    a = acts[0]
    assert a.target == "style/KV_100" and a.after["when"] == 1  # a seller is out of stock
    assert [r["sku"] for r in a.after["lines"]] == ["KV_100_NVY_M", "KV_100_NVY_L"]  # out first
    assert "1 out of stock" in a.after["title"] and a.after["confidence"] == "low"
    assert a.after["impact_usd"] > 0


def test_style_keys():
    assert key_of({"sku": "KV_10005000_NVY_M", "asin": "B1"}, {}) == "KV_10005000"
    assert key_of({"sku": "KV_10005000_NVY_M", "asin": "B1"}, {"B1": "P9"}) == "P9"
    assert key_of({"sku": "FS100500BLKS", "title": "Dagacci Jogger Set, Black, S"}, {}) == "Jogger Set"
