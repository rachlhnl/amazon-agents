from datetime import date, timedelta

from amz_agents.actions import ActionLog
from amz_agents.agents.chief_of_staff import ChiefOfStaff, Goal, load_goals
from amz_agents.config import Mode, Settings
from amz_agents.orchestrator import Orchestrator


def run(ctx, mode=Mode.RECOMMEND):
    orch = Orchestrator(ctx, ActionLog(ctx.wh, mode))
    submitted = orch.run()
    return orch, submitted


def test_goals_load_from_toml(ctx):
    goals = load_goals()
    assert {g.metric for g in goals} == {"units", "tacos", "revenue"}


def test_escalations_come_first(ctx):
    _, submitted = run(ctx)
    kinds = [a.kind for _, a in submitted]
    first_non_esc = next(i for i, k in enumerate(kinds) if k != "escalation")
    assert "escalation" not in kinds[first_non_esc:]


def test_goal_conflict_escalated_for_low_stock_growth_goal(ctx):
    _, submitted = run(ctx)
    esc = [a for _, a in submitted if a.kind == "escalation" and a.sku == "BTL-32-SGE"]
    assert esc and "can't be met on current stock" in esc[0].reason
    assert "Price BTL-32-SGE" in esc[0].reason  # names the action working against it


def test_sales_drop_escalated_with_likely_cause(ctx):
    _, submitted = run(ctx)
    esc = [a for _, a in submitted if a.kind == "escalation" and a.sku == "BTL-64-BLK"]
    assert esc and "Buy Box" in esc[0].reason and "not a stockout" in esc[0].reason.lower()


def test_actions_ranked_by_priority_then_impact(ctx):
    from amz_agents.agents.chief_of_staff import TIER

    _, submitted = run(ctx)
    ranked = [a for _, a in submitted if a.kind != "escalation"]
    tiers = [TIER[a.priority] for a in ranked]
    assert tiers == sorted(tiers)
    for tier in ("high", "medium", "low"):
        impacts = [a.impact_usd for a in ranked if a.priority == tier]
        assert impacts == sorted(impacts, reverse=True)


def test_every_action_has_sku_and_goal_tags_make_sense(ctx):
    _, submitted = run(ctx)
    for _, a in submitted:
        assert a.sku or (a.kind == "opportunity" and a.target.endswith("/all")), a
    tags = {(a.kind, a.target): a.goal for _, a in submitted}
    assert tags[("add_negative", "c3/ag3")] == "serves: Keep TACoS under 12%"
    assert tags[("update_price", "BTL-32-SGE")] == "works against: Grow Sage bottle sales"


def test_negative_impact_flagged_for_rejection(ctx):
    orch, submitted = run(ctx)
    text = orch.chief.brief(submitted, orch.outcomes)
    assert "## Consider rejecting" in text
    assert any(a.impact_usd is not None and a.impact_usd < 0 for _, a in submitted)


def test_outcomes_evaluated_once(ctx):
    orch, _ = run(ctx)
    assert {o["verdict"] for o in orch.outcomes} == {"helped"}
    assert len(orch.outcomes) == 2
    again = ChiefOfStaff(ctx).evaluate_outcomes()
    assert again == []  # already judged


def test_recent_actions_not_judged_yet(ctx):
    ctx.wh.execute("DELETE FROM actions")
    yesterday = (date.today() - timedelta(days=2)).isoformat()
    ctx.wh.execute(
        "INSERT INTO actions (agent, kind, target, before, after, reason, status, sku, executed_at)"
        " VALUES ('ads','update_bid','kw1','{}','{}','x','executed','BTL-32-BLK',?)",
        (yesterday,),
    )
    assert ChiefOfStaff(ctx).evaluate_outcomes() == []


def test_lessons_flag_action_types_that_keep_hurting(ctx):
    for i in range(3):
        aid = ctx.wh.execute(
            "INSERT INTO actions (agent, kind, target, before, after, reason, status) "
            "VALUES ('pricing','update_price','X','{}','{}','r','executed')"
        )
        ctx.wh.execute(
            "INSERT INTO outcomes (action_id, metric, before, after, verdict) "
            "VALUES (?, 'daily_profit', 10, 8, 'hurt')", (aid,)
        )
    lessons = ChiefOfStaff(ctx).lessons()
    assert any("update_price" in x and "tighten" in x for x in lessons)


def test_tacos_goal_progress(ctx):
    cos = ChiefOfStaff(ctx, goals=[Goal("t", "tacos", "all", target=0.01)])
    gp = cos.goal_progress()[0]
    assert gp["status"] == "behind" and gp["pct"] > 0.01


def test_brief_has_all_sections(ctx):
    ctx.settings = Settings(mode=Mode.APPROVE)
    orch, submitted = run(ctx, Mode.APPROVE)
    text = orch.chief.brief(submitted, orch.outcomes)
    for section in ("Needs your attention", "Goals", "Top priorities across teams",
                    "Opportunities from the data agent", "Did past changes work?",
                    "Agent health"):
        assert f"## {section}" in text
    assert "amz-agents approve" in text
    assert "(see action [" in text  # anomaly links to the pricing action
