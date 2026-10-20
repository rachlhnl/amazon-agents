from amz_agents.actions import HUMAN_ONLY
from amz_agents.agents.data import DataAgent
from amz_agents.agents.product import ProductAgent
from amz_agents.agents.reviews import ReviewsAgent


def test_product_agent_works_product_findings(ctx):
    acts = {a.kind: a for a in ProductAgent().run(ctx)}
    assert set(acts) == {"fix_fit", "plan_launch"}
    fit = acts["fix_fit"]
    assert fit.after["when"] == 1 and fit.after["steps"] and fit.after["source"] == "market"
    assert "71%" in fit.notes and "kpi_now" not in fit.after  # moving number kept out
    assert set(acts) <= HUMAN_ONLY


def test_data_agent_routes_the_rest_of_the_market(ctx):
    ctx.shared["roster"] = {}
    opps = [a for a in DataAgent().run(ctx) if a.after.get("source") == "market"]
    assert [o.after["owner"] for o in opps] == ["marketing"]
    assert opps[0].after["when"] == 2


def test_store_reviews_left_to_product_agent(ctx):
    ctx.wh.insert_many("reviews", [{"asin": "store:1", "rating": 1, "body": "size is too big",
                                    "created": "2026-10-01"}] * 3)
    assert not [a for a in ReviewsAgent().run(ctx) if a.target.startswith("store:")]


def test_urgent_market_work_ranks_first(ctx):
    from tests.test_roles_and_data import run

    _, submitted = run(ctx)
    fit = next(a for _, a in submitted if a.kind == "fix_fit")
    assert fit.priority == "now" and fit.team == "product"
