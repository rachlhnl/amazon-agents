import json

from amz_agents import dashboard, plan
from amz_agents.actions import Action, ActionLog
from amz_agents.config import Mode

PLAN = """
[[item]]
id = "sizing"
rank = 1
added = "2026-10-20"
title = "One clear sizing step"
team = "product"
effort = "S"
due_days = 7
gain_low = 1000
gain_high = 2000
why = "Shoppers size down twice."
kpi = "Fit share"
baseline = "70%"
target = "\\u2264 35%"
measure = "manual"
steps = ["Pick one size system."]
covers = ["Straw Lid: buyers complain about leaks"]
drafter = "product"
draft = "A sizing memo."
"""


def test_plan_becomes_approved_work_and_merges_suggestions(ctx, tmp_path, monkeypatch):
    path = tmp_path / "plan.toml"
    path.write_text(PLAN)
    monkeypatch.setenv("PLAN_PATH", str(path))
    log = ActionLog(ctx.wh, Mode.APPROVE)
    sid = log.submit(Action(agent="product", kind="fix_fit", target="straw-lid", before=None,
                            after={"title": "Straw Lid: buyers complain about leaks"}, reason=""))
    assert plan.sync(ctx.wh) == 1
    assert plan.sync(ctx.wh) == 0  # idempotent
    rows = ctx.wh.query("SELECT * FROM actions WHERE agent='ceo'")
    assert len(rows) == 1 and rows[0]["status"] == "acknowledged" and rows[0]["priority"] == "now"
    after = json.loads(rows[0]["after"])
    assert after["draft_text"]["voice"] == "outline" and "Pick one size system." in after["draft_text"]["text"]
    assert ctx.wh.query("SELECT status FROM actions WHERE id=?", (sid,))[0]["status"] == "superseded"
    # the merged suggestion isn't proposed again
    assert log.already_open_or_done(Action(agent="product", kind="fix_fit", target="straw-lid",
                                           before=None, after={"title": "Straw Lid: buyers complain about leaks"},
                                           reason=""))
    log.complete(rows[0]["id"])
    data = dashboard.collect(ctx.wh)
    item = data["plan"][0]
    assert item["status"] == "done" and item["merged"] == 1 and item["gain_high"] == 2000
    assert not any(a["status"] == "superseded" for a in data["actions"])


def test_targets_read_both_ways():
    assert plan._met(0.3, "≤ 35%") and not plan._met(0.7, "≤ 35%")
    assert plan._met(1200, "< 1,500 KB") and plan._met(49, "≤ $49") and not plan._met(50, "≤ $49")


def test_local_plan_wins_over_the_example(tmp_path, monkeypatch):
    monkeypatch.delenv("PLAN_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.toml").write_text(PLAN)
    assert plan.plan_path().name == "plan.toml"
    (tmp_path / "plan.local.toml").write_text(PLAN.replace("One clear sizing step", "Real plan"))
    assert plan.plan_path().name == "plan.local.toml"
    assert plan.load_plan()[0]["title"] == "Real plan"


def test_ad_kpis_come_from_the_quartile_export(ctx):
    ctx.wh.insert_many("ad_products", [
        {"period_start": "2026-08-20", "period_end": "2026-10-20", "asin": a, "parent_asin": p,
         "name": f"Dagacci Set {a}, Navy, M", "total_sales": t, "ad_sales": ad, "ad_spend": sp,
         "ad_types": "Sponsored Products", "source": "quartile"}
        for a, p, t, ad, sp in [("A1", "P1", 50000, 10000, 1000), ("B1", "P2", 4000, 3000, 900)]])
    share = plan.measure({"measure": "ads:share:P2", "target": "≤ 40%"}, wh=ctx.wh)
    assert share["now"] == "75%" and share["met"] is False
    costly = plan.measure({"measure": "ads:costly", "target": "≤ 3"}, wh=ctx.wh)
    assert costly["now"] == "1" and costly["met"] is True  # B1: 30% ACoS vs 14.8%


def test_outline_drafts_are_rewritten_once_claude_is_available(ctx, tmp_path, monkeypatch):
    path = tmp_path / "plan.toml"
    path.write_text(PLAN)
    monkeypatch.setenv("PLAN_PATH", str(path))
    plan.sync(ctx.wh)
    after = lambda: json.loads(ctx.wh.query("SELECT after FROM actions WHERE agent='ceo'")[0]["after"])
    assert after()["draft_text"]["voice"] == "outline"
    monkeypatch.setattr(plan.llm, "available", lambda: True)
    monkeypatch.setattr(plan.llm, "complete", lambda *a, **k: "A written memo.")
    plan.sync(ctx.wh, model="claude-test")
    assert after()["draft_text"] == {"by": "product", "voice": "claude", "text": "A written memo."}


def test_items_taken_out_of_the_plan_leave_the_tracker(ctx, tmp_path, monkeypatch):
    path = tmp_path / "plan.toml"
    path.write_text(PLAN + PLAN.replace('"sizing"', '"other"').replace("rank = 1", "rank = 2"))
    plan.sync(ctx.wh, path=path)
    assert ctx.wh.query("SELECT COUNT(*) AS n FROM actions WHERE agent='ceo' AND "
                        "status='acknowledged'")[0]["n"] == 2
    path.write_text(PLAN)
    plan.sync(ctx.wh, path=path)
    rows = {r["target"]: r["status"] for r in ctx.wh.query("SELECT * FROM actions WHERE agent='ceo'")}
    assert rows == {"plan/sizing": "acknowledged", "plan/other": "superseded"}


def test_stock_kpi_counts_out_of_stock_best_sellers(ctx):
    ctx.wh.execute("UPDATE products SET units_on_hand=0 WHERE sku='BTL-32-BLK'")
    m = plan.measure({"measure": "stock:core_out", "target": "0"}, wh=ctx.wh)
    assert m["now"] == "1" and m["met"] is False


def test_units_kpi_reads_the_last_seven_days(ctx):
    m = plan.measure({"measure": "units7:BTL-32-BLK", "target": "≥ 5/day"}, wh=ctx.wh)
    assert m["now"].endswith("/day") and m["met"] is not None
