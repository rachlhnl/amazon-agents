from datetime import date, timedelta

import pytest

from amz_agents.actions import Action, ActionLog
from amz_agents.agents.base import EFFORT_HOURS, TEAMS, Agent, Role
from amz_agents.agents.chief_of_staff import HOURLY_COST
from amz_agents.agents.data import DataAgent
from amz_agents.config import Mode, Settings
from amz_agents.orchestrator import DEFAULT_AGENTS, Orchestrator


def run(ctx, mode=Mode.RECOMMEND, agents=None, executors=None):
    extra = [a() for a in agents or []]
    orch = Orchestrator(ctx, ActionLog(ctx.wh, mode, executors),
                        agents=extra and [*[a() for a in DEFAULT_AGENTS], *extra])
    return orch, orch.run()


def find(submitted, kind, target):
    return next(a for _, a in submitted if a.kind == kind and a.target == target)


def opps(submitted):
    return {a.target: a for _, a in submitted if a.kind == "opportunity"}


# -------------------------------------------------------------------- roles


@pytest.mark.parametrize("agent", DEFAULT_AGENTS)
def test_every_agent_declares_a_role(agent):
    r = agent.role
    assert r.mission and r.team in TEAMS and r.proposes
    assert set(r.proposes.values()) <= set(EFFORT_HOURS)


def test_agents_only_propose_what_they_declare(ctx):
    orch, _ = run(ctx)
    for h in orch.chief.agent_health():
        assert h["problems"] == [], h


def test_chief_scores_effort_roi_priority_and_team(ctx):
    _, submitted = run(ctx)
    reorder = find(submitted, "reorder", "BTL-32-SGE")
    assert (reorder.team, reorder.effort, reorder.priority) == ("amazon_ops", "S", "high")
    assert reorder.roi == round(reorder.impact_usd / (EFFORT_HOURS["S"] * HOURLY_COST), 1)
    assert find(submitted, "pause_campaign", "m2").team == "marketing"
    lead = find(submitted, "send_outreach", "L1")
    assert (lead.team, lead.priority) == ("sales", "judgment")
    for _, a in submitted:
        assert a.priority and a.team, a


def test_priority_saved_with_action(ctx):
    run(ctx)
    row = ctx.wh.query("SELECT * FROM actions WHERE kind='reorder' AND target='BTL-32-SGE'")[0]
    assert (row["team"], row["effort"], row["priority"]) == ("amazon_ops", "S", "high")


def test_auto_mode_makes_executable_changes_xs(ctx):
    ctx.settings = Settings(mode=Mode.AUTO)
    _, submitted = run(ctx, Mode.AUTO, executors={})
    assert find(submitted, "reorder", "BTL-32-SGE").effort == "S"  # still a person's job
    assert all(a.effort == "XS" for _, a in submitted if a.kind == "update_bid")


class Broken(Agent):
    name = "broken"
    role = Role(mission="Always fails.", team="data", proposes={"x": "S"})

    def run(self, ctx):
        raise RuntimeError("boom")


class OutOfLane(Agent):
    name = "out_of_lane"
    role = Role(mission="Declares one thing, does another.", team="data",
                proposes={"x": "S"})

    def run(self, ctx):
        return [Action(agent=self.name, kind="update_price", target="LID-STRAW", sku="LID-STRAW",
                       before={"price": 12.99}, after={"price": 13.49, "margin": 0.5},
                       reason="because")]


def test_broken_agent_reported_not_fatal(ctx):
    orch, submitted = run(ctx, agents=[Broken])
    assert any(a.kind == "reorder" for _, a in submitted)  # everyone else still ran
    text = orch.chief.brief(submitted, orch.outcomes)
    assert "Agent **broken**: failed: RuntimeError('boom')" in text


def test_undeclared_action_type_flagged(ctx):
    orch, _ = run(ctx, agents=[OutOfLane])
    h = next(h for h in orch.chief.agent_health() if h["agent"] == "out_of_lane")
    assert "proposed undeclared types: update_price" in h["problems"]


def test_agent_without_its_data_flagged(ctx):
    ctx.wh.execute("DELETE FROM external_campaigns")
    orch, submitted = run(ctx)
    text = orch.chief.brief(submitted, orch.outcomes)
    assert "Agent **marketing**: no data in external_campaigns" in text


# -------------------------------------------------------------- team briefs


def test_team_brief_holds_only_that_team(ctx):
    orch, submitted = run(ctx)
    text = orch.chief.team_brief("marketing", submitted)
    assert text.startswith("# Marketing brief")
    assert "Your agents: **marketing**" in text
    assert "Pause TikTok campaign" in text
    assert "Reorder" not in text and "Follow up with" not in text


def test_team_brief_includes_its_opportunities(ctx):
    orch, submitted = run(ctx)
    text = orch.chief.team_brief("sales", submitted)
    assert "## Opportunities for your team" in text and "Amazon is 80% of unit sales" in text


def test_team_with_nothing_gets_no_brief(ctx):
    orch, submitted = run(ctx)
    assert orch.chief.team_brief("data", submitted) is None  # sample data is clean


def test_team_briefs_written_and_emailed(ctx, tmp_path, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def send_message(self, msg):
            sent.append((msg["To"], msg["Subject"]))

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    for k, v in {"SMTP_HOST": "smtp", "BRIEF_FROM": "bot@x", "BRIEF_TO": "ceo@x",
                 "BRIEF_TO_MARKETING": "mkt@x"}.items():
        monkeypatch.setenv(k, v)
    from amz_agents.notify import send_brief, team_recipients_var

    orch, submitted = run(ctx)
    paths = orch.team_briefs(submitted, out_dir=str(tmp_path))
    assert set(paths) == {"amazon_ops", "marketing", "sales"}
    for team, p in paths.items():
        send_brief(p.read_text(), f"{TEAMS[team]} brief", team_recipients_var(team))
    assert sent == [("mkt@x", "Marketing brief")]  # only marketing has recipients set


# --------------------------------------------------------------- data agent


def test_unadvertised_overstock_routed_to_ads_and_linked_to_email(ctx):
    _, submitted = run(ctx)
    o = opps(submitted)["ads/LID-STRAW"]
    assert o.after["owner"] == "ads" and o.team == "amazon_ops"
    assert o.impact_usd > 0 and o.effort == "M" and o.roi is not None
    assert ["email_campaign", "LID-STRAW"] in o.after["related"]


def test_measured_elasticity_challenges_assumption(ctx):
    _, submitted = run(ctx)
    o = opps(submitted)["elasticity/LID-STRAW"]
    assert "more price-sensitive" in o.after["title"]
    assert ["update_price", "LID-STRAW"] in o.after["related"]


def test_channel_concentration_goes_to_sales(ctx):
    _, submitted = run(ctx)
    o = opps(submitted)["channels/all"]
    assert o.team == "sales" and ["send_outreach", "L1"] in o.after["related"]


def test_low_rating_linked_to_defect_and_listing_work(ctx):
    orch, submitted = run(ctx)
    o = opps(submitted)["ratings/BTL-32-BLK"]
    assert ["flag_defect", "B0SAMPLE01"] in o.after["related"]
    assert ["update_listing_copy", "BTL-32-BLK"] in o.after["related"]
    text = orch.chief.brief(submitted, orch.outcomes)
    assert "In progress via other agents:" in text


def test_data_quality_checks(ctx):
    ctx.wh.execute("DELETE FROM metrics_daily WHERE date >= ?",
                   ((date.today() - timedelta(days=5)).isoformat(),))
    ctx.wh.execute("UPDATE products SET landed_cost=NULL WHERE sku='BTL-64-BLK'")
    ctx.wh.execute("UPDATE external_campaigns SET sales_30d=0, orders_30d=0 "
                   "WHERE campaign_id='m1'")
    orch, submitted = run(ctx)
    titles = {a.after["title"]: a for _, a in submitted if a.kind == "opportunity"
              and a.after["owner"] == "data"}
    assert {"Daily sales data is stale", "Products missing cost data",
            "Amazon-bound campaigns with no attributed sales"} <= set(titles)
    assert all(a.team == "data" for a in titles.values())
    assert orch.chief.team_brief("data", submitted).startswith("# Data & insights brief")
    # inventory and data both need fresh metrics_daily
    stale = [h["agent"] for h in orch.chief.agent_health()
             if any("stale" in p for p in h["problems"])]
    assert set(stale) == {"inventory", "data"}


def test_low_stock_sku_not_suggested_for_ads(ctx):
    ctx.wh.execute("DELETE FROM keywords WHERE sku='BTL-32-SGE'")
    ctx.wh.execute("UPDATE metrics_daily SET ad_spend=0 WHERE sku='BTL-32-SGE'")
    _, submitted = run(ctx)
    assert "ads/BTL-32-SGE" not in opps(submitted)


def test_opportunity_without_an_agent_flagged_for_assignment(ctx):
    ctx.shared["roster"] = {"data": DataAgent.role}
    ctx.shared["low_stock"], ctx.shared["overstock"] = set(), set()
    out = {a.target: a for a in DataAgent().run(ctx)}
    assert "No sales agent is running" in out["channels/all"].after["recommendation"]


def test_acknowledged_opportunity_not_resurfaced_when_numbers_move(ctx):
    _, submitted = run(ctx, Mode.APPROVE)
    i = next(i for i, a in submitted if a.target == "ads/LID-STRAW")
    log = ActionLog(ctx.wh, Mode.APPROVE)
    log.approve(i)  # human-only -> acknowledged
    ctx.wh.execute("UPDATE products SET daily_velocity=7.5 WHERE sku='LID-STRAW'")
    _, again = run(ctx, Mode.APPROVE)
    assert "ads/LID-STRAW" not in opps(again)
