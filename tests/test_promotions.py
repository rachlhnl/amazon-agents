from datetime import date, timedelta

from amz_agents.actions import ActionLog
from amz_agents.agents.inventory import InventoryAgent
from amz_agents.agents.pricing import PricingAgent
from amz_agents.agents.promotions import Event, PromotionsAgent, load_calendar
from amz_agents.config import Mode
from amz_agents.orchestrator import DEFAULT_AGENTS, Orchestrator

TODAY = date.today()


def event(days_out=20, length=4, **kw):
    starts = TODAY + timedelta(days=days_out)
    base = {"name": "Test Sale", "channel": "amazon", "starts": starts,
            "ends": starts + timedelta(days=length - 1), "min_discount": 0.15}
    return Event(**{**base, **kw})


def promotions(ctx, *events, today=TODAY):
    InventoryAgent().run(ctx)  # low-stock list, as in a real run
    return PromotionsAgent(list(events), today=today).run(ctx)


def kinds(actions, kind):
    return [a for a in actions if a.kind == kind]


def test_plan_prices_deals_from_recent_sales_and_checks_margin(ctx):
    plan = kinds(promotions(ctx, event(submit_by=TODAY + timedelta(days=5))), "plan_deal")[0]
    picks = {p["sku"]: p for p in plan.after["skus"]}
    big = picks["BTL-64-BLK"]
    assert big["reference"] == 39.99 and big["deal_price"] == 33.99  # 15% under
    assert big["margin"] is not None and big["margin"] > 0.15
    assert plan.after["when"] == 5  # submissions close in 5 days


def test_products_under_four_stars_stay_off_deals(ctx):
    plan = kinds(promotions(ctx, event()), "plan_deal")[0]
    assert "BTL-32-BLK" not in {p["sku"] for p in plan.after["skus"]}  # leaking-lid reviews
    assert {"sku": "BTL-32-BLK", "why": "rated 3.0, under 4"} in plan.after["excluded"]


def test_low_stock_is_left_out_and_sent_to_amazon_ops(ctx):
    actions = promotions(ctx, event())
    plan = kinds(actions, "plan_deal")[0]
    assert "BTL-32-SGE" not in {p["sku"] for p in plan.after["skus"]}
    stock = kinds(actions, "event_stock")[0]
    assert stock.after["team"] == "amazon_ops"
    assert "BTL-32-SGE" in {line["sku"] for line in stock.after["lines"]}


def test_price_hold_stops_the_pricing_agent(ctx):
    actions = promotions(ctx, event(days_out=10, hold_days=30))
    hold = kinds(actions, "hold_price")[0]
    assert "BTL-64-BLK" in hold.after["skus"]
    assert "BTL-64-BLK" in ctx.shared["price_hold"]
    changed = {a.target for a in PricingAgent().run(ctx)}
    assert "BTL-64-BLK" not in changed  # would otherwise match the 36.00 Buy Box


def test_no_hold_until_the_hold_window_is_near(ctx):
    actions = promotions(ctx, event(days_out=60, hold_days=30))
    assert not kinds(actions, "hold_price")
    assert ctx.shared["price_hold"] == {}


def test_missing_data_is_named(ctx):
    ctx.wh.execute("UPDATE products SET assumed = 'landed_cost fees' WHERE sku = 'BTL-64-BLK'")
    ctx.wh.execute("DELETE FROM channel_listings WHERE channel = 'walmart'")
    actions = promotions(ctx, event(), event(channel="walmart"))
    plan = kinds(actions, "plan_deal")[0]
    assert {p["sku"]: p for p in plan.after["skus"]}["BTL-64-BLK"]["margin"] is None
    missing = " ".join(kinds(actions, "request_data")[0].after["missing"])
    assert "COSTS_CSV" in missing and "Walmart" in missing and "Confirm" in missing


def test_walmart_prices_are_matched_when_known(ctx):
    ctx.wh.upsert("channel_listings", [{"channel": "walmart", "sku": "BTL-64-BLK",
                                        "price": 39.99, "listed": 1}], key=["channel", "sku"])
    actions = promotions(ctx, event(channel="walmart", confirmed=True))
    match = kinds(actions, "match_channel_price")[0]
    assert match.after["lines"][0] == {"sku": "BTL-64-BLK", "walmart": 39.99, "match_to": 33.99}


def test_results_after_an_event_for_the_ad_agency(ctx):
    ended = event(days_out=-8, length=5, confirmed=True)  # ran 8 to 4 days ago
    res = kinds(promotions(ctx, ended), "promotion_results")[0]
    assert res.after["team"] == "marketing" and res.after["days"] == 5
    assert res.after["lift"] is not None


def test_far_off_events_wait(ctx):
    assert not promotions(ctx, event(days_out=200, confirmed=True))


def test_clearance_picks_overstock_only(ctx):
    actions = promotions(ctx, event(pick="overstock", confirmed=True))
    picked = {p["sku"] for p in kinds(actions, "plan_deal")[0].after["skus"]}
    assert picked <= ctx.shared["overstock"]


def test_runs_before_pricing_and_routes_work_to_teams(ctx):
    names = [a.name for a in DEFAULT_AGENTS]
    assert names.index("promotions") < names.index("pricing")
    orch = Orchestrator(ctx, ActionLog(ctx.wh, Mode.RECOMMEND))
    orch.agents[names.index("promotions")] = PromotionsAgent([event()], today=TODAY)
    submitted = orch.run()
    teams = {a.kind: a.team for _, a in submitted if a.agent == "promotions"}
    assert teams["plan_deal"] == "promotions" and teams["event_stock"] == "amazon_ops"


def test_shipped_calendar_parses():
    events = load_calendar("promotions.toml")
    assert events and all(e.ends >= e.starts for e in events)
    bf = next(e for e in events if e.name == "Black Friday & Cyber Monday · Amazon")
    assert bf.starts == date(2026, 11, 27) and bf.hold_from == date(2026, 10, 28)


def test_stale_sales_and_stock_are_flagged(ctx):
    later = TODAY + timedelta(days=12)  # sample history ends yesterday
    actions = promotions(ctx, event(days_out=30), today=later)
    missing = " ".join(kinds(actions, "request_data")[0].after["missing"])
    assert "Sales and stock are as of" in missing


def test_cost_counts_discount_and_fees_by_basis(ctx):
    per_deal = event(upfront_fee=100.0, pct_fee=0.015, fee_cap=5000.0, fee_basis="per_deal")
    plan = kinds(promotions(ctx, per_deal), "plan_deal")[0]
    cost, picks = plan.after["cost"], plan.after["skus"]
    assert cost["deals"] == len(picks)  # sample SKUs are one style each
    assert cost["upfront_fees"] == 100 * len(picks)
    assert cost["cost_total"] == cost["discount_given"] + cost["fees_total"] > 0
    campaign = event(upfront_fee=100.0, pct_fee=0.015, fee_basis="per_campaign")
    assert kinds(promotions(ctx, campaign), "plan_deal")[0].after["cost"]["upfront_fees"] == 100
    unknown = kinds(promotions(ctx, event(fee_basis="unknown")), "plan_deal")[0].after["cost"]
    assert unknown["fees_total"] is None and unknown["cost_total"] == unknown["discount_given"]


def test_overlapping_promotions_on_one_platform_are_flagged(ctx):
    a, b = event(name="Deal A"), event(name="Coupon B", days_out=22)
    c = event(name="Walmart C", channel="walmart")
    plans = {p.after["event"]: p for p in kinds(promotions(ctx, a, b, c), "plan_deal")}
    assert [o["event"] for o in plans["Deal A"].after["overlaps"]] == ["Coupon B"]
    assert plans["Deal A"].after["overlaps"][0]["shared_skus"] > 0
    assert plans["Walmart C"].after["overlaps"] == []
