import pytest

from amz_agents.actions import ActionLog
from amz_agents.config import Mode
from amz_agents.orchestrator import Orchestrator


def run(ctx, mode=Mode.RECOMMEND, executors=None):
    log = ActionLog(ctx.wh, mode, executors)
    return Orchestrator(ctx, log).run(), log


def by_kind(submitted, kind):
    return [a for _, a in submitted if a.kind == kind]


def test_no_bid_raise_for_low_stock_sku(ctx):
    submitted, _ = run(ctx)
    assert "BTL-32-SGE" in ctx.shared["low_stock"]
    # kw4 (Sage) is well under target ACoS but should be held, not raised.
    assert not [a for a in by_kind(submitted, "update_bid") if a.target == "kw4"]


def test_bids_lowered_for_high_acos_and_zero_orders(ctx):
    bids = {a.target: a for a in by_kind(run(ctx)[0], "update_bid")}
    assert bids["kw5"].after["bid"] == 0.56  # 0 orders -> max cut
    assert bids["kw1"].after["bid"] < 1.40
    for a in bids.values():
        assert abs(a.after["bid"] - a.before["bid"]) / a.before["bid"] <= 0.2001


def test_harvest_and_negate(ctx):
    submitted, _ = run(ctx)
    harvested = {a.after["keyword"] for a in by_kind(submitted, "add_keyword")}
    negated = {a.after["keyword"] for a in by_kind(submitted, "add_negative")}
    assert harvested == {"insulated water bottle with straw lid"}
    assert negated == {"glass water bottle", "kids water bottle"}


def test_pricing_respects_floor_and_low_stock(ctx):
    prices = {a.target: a for a in by_kind(run(ctx)[0], "update_price")}
    assert prices["BTL-32-SGE"].after["price"] > 29.99  # slow sell-through
    assert prices["BTL-32-BLK"].after["price"] == 27.49  # match Buy Box
    for a in prices.values():
        assert a.after["margin"] >= 0.15


def test_reorder_and_defect_flags(ctx):
    submitted, _ = run(ctx)
    assert {a.target for a in by_kind(submitted, "reorder")} >= {"BTL-32-SGE"}
    defects = by_kind(submitted, "flag_defect")
    assert any(a.after["theme"] == "leaking" for a in defects)


def test_recommend_mode_never_executes(ctx):
    calls = []
    _submitted, log = run(ctx, Mode.RECOMMEND, {"update_bid": calls.append})
    assert not calls
    new = [a for a in log.list() if a["status"] != "executed"]  # seeded history is executed
    assert {a["status"] for a in new} == {"proposed"}


def test_auto_mode_executes_but_holds_human_only(ctx):
    calls = []
    executors = {k: calls.append for k in ("update_bid", "add_negative", "add_keyword", "update_price")}
    _, log = run(ctx, Mode.AUTO, executors)
    statuses = {(a["kind"], a["status"]) for a in log.list()}
    assert ("update_bid", "executed") in statuses
    assert ("update_listing_copy", "pending_approval") in statuses
    assert ("reorder", "pending_approval") in statuses
    assert calls


def test_approve_flow_and_failed_executor_is_logged(ctx):
    def boom(_):
        raise RuntimeError("api down")

    _, log = run(ctx, Mode.APPROVE, {"update_bid": boom})
    pending = log.list("pending_approval")
    bid = next(a for a in pending if a["kind"] == "update_bid")
    log.approve(bid["id"])
    row = ctx.wh.query("SELECT status, notes FROM actions WHERE id=?", (bid["id"],))[0]
    assert row["status"] == "failed" and "api down" in row["notes"]

    reorder = next(a for a in pending if a["kind"] == "reorder")
    log.approve(reorder["id"])
    assert ctx.wh.query("SELECT status FROM actions WHERE id=?", (reorder["id"],))[0]["status"] == "acknowledged"

    with pytest.raises(ValueError):
        log.approve(reorder["id"])  # can't approve twice
