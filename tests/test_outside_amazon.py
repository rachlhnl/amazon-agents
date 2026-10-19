from amz_agents.actions import ActionLog
from amz_agents.agents.marketing import BRAND_REFERRAL_BONUS
from amz_agents.config import Mode
from amz_agents.executors import build_executors
from amz_agents.orchestrator import Orchestrator


def run(ctx, mode=Mode.RECOMMEND, executors=None):
    log = ActionLog(ctx.wh, mode, executors)
    orch = Orchestrator(ctx, log)
    return orch.run(), log, orch


def by_kind(submitted, kind):
    return {a.target: a for _, a in submitted if a.kind == kind}


# ---------------------------------------------------------------- marketing


def test_campaign_for_low_stock_sku_paused(ctx):
    paused = by_kind(run(ctx)[0], "pause_campaign")
    assert "low stock" in paused["m2"].reason  # Sage sends shoppers to Amazon
    assert paused["m2"].impact_usd == 450.0  # 30 days of spend saved


def test_zero_order_campaign_paused(ctx):
    paused = by_kind(run(ctx)[0], "pause_campaign")
    assert "0 orders" in paused["t1"].reason


def test_budgets_follow_profit_per_dollar(ctx):
    budgets = by_kind(run(ctx)[0], "update_campaign_budget")
    # m1: $2,099 Amazon sales x (45% margin + referral bonus) / $1,200 spend < $1 -> cut
    assert budgets["m1"].after["daily_budget"] == 32.0
    assert budgets["m1"].before["profit_per_dollar"] < 1
    # g1: Shopify traffic at > $1.50 per $1 -> scale, capped at +20%
    assert budgets["g1"].after["daily_budget"] == 30.0


def test_referral_bonus_counts_only_for_amazon_traffic(ctx):
    ctx.wh.execute("UPDATE external_campaigns SET destination='shopify' WHERE campaign_id='m1'")
    ctx.wh.execute("UPDATE channel_listings SET price=29.99, fees=9.40 "
                   "WHERE channel='shopify' AND sku='BTL-32-BLK'")
    m1 = by_kind(run(ctx)[0], "update_campaign_budget")["m1"]
    assert abs(m1.before["profit_per_dollar"]
               - (0.96 - 2099.3 * BRAND_REFERRAL_BONUS / 1200)) < 0.02


def test_budget_never_exceeds_cap(ctx):
    ctx.wh.execute("UPDATE external_campaigns SET daily_budget=450 WHERE campaign_id='g1'")
    g1 = by_kind(run(ctx)[0], "update_campaign_budget")["g1"]
    assert g1.after["daily_budget"] == ctx.settings.guardrails.max_daily_budget


def test_email_for_overstock_without_campaign(ctx):
    emails = by_kind(run(ctx)[0], "email_campaign")
    assert set(emails) == {"LID-STRAW"}


# -------------------------------------------------------------------- sales


def test_channel_price_parity_uses_proposed_amazon_price(ctx):
    prices = by_kind(run(ctx)[0], "update_channel_price")
    # Amazon BLK is being cut to 27.49 (Buy Box), so Shopify at 27.99 is fine.
    assert "shopify/BTL-32-BLK" not in prices
    lid = prices["shopify/LID-STRAW"]
    assert lid.after["price"] == 10.99  # toward Amazon, capped at +10% per change
    assert "fair pricing" in lid.reason


def test_channel_price_raised_to_margin_floor(ctx):
    ctx.wh.execute("UPDATE channel_listings SET price=14.00 WHERE channel='walmart'")
    a = by_kind(run(ctx)[0], "update_channel_price")["walmart/BTL-32-BLK"]
    assert "margin floor" in a.reason
    assert a.after["price"] == 16.00  # (7.10 cost + 6.50 Walmart fees) / (1 - 15%)


def test_suggests_new_channel_for_fast_sellers_not_low_stock(ctx):
    listings = by_kind(run(ctx)[0], "list_on_channel")
    assert "walmart/LID-STRAW" in listings
    assert not any("BTL-32-SGE" in t for t in listings)  # low stock


def test_quiet_leads_followed_up_with_stock_warning(ctx):
    outreach = by_kind(run(ctx)[0], "send_outreach")
    assert set(outreach) == {"L1", "L3"}  # L2 contacted 3 days ago; L4 won
    assert "Don't promise volume on BTL-32-SGE" in outreach["L1"].reason


def test_outside_amazon_human_only_in_auto(ctx):
    _, log, _ = run(ctx, Mode.AUTO, {})
    statuses = {(a["kind"], a["status"]) for a in log.list()}
    for kind in ("send_outreach", "list_on_channel", "email_campaign"):
        assert (kind, "pending_approval") in statuses


def test_brief_shows_channel_mix_and_outside_actions(ctx):
    submitted, _, orch = run(ctx)
    text = orch.chief.brief(submitted, orch.outcomes)
    assert "## Sales by channel" in text and "| shopify |" in text
    assert "Pause TikTok campaign" in text
    assert "Follow up with Peak Outfitters" in text


def test_external_actions_dont_count_toward_tacos_goal(ctx):
    submitted = run(ctx)[0]
    for a in by_kind(submitted, "update_campaign_budget").values():
        assert "TACoS" not in a.goal


# ------------------------------------------------------- across runs + executors


def test_pending_actions_not_queued_again(ctx):
    first, _, _ = run(ctx, Mode.APPROVE)
    second, _, orch = run(ctx, Mode.APPROVE)
    proposed_first = [a for _, a in first if a.kind != "escalation"]
    proposed_second = [a for _, a in second if a.kind != "escalation"]
    assert proposed_first and not proposed_second
    assert orch.skipped == len(proposed_first)


def test_executed_negative_not_proposed_again(ctx):
    calls = []
    ex = {"add_negative": calls.append}
    run(ctx, Mode.AUTO, ex)
    n = len(calls)
    run(ctx, Mode.AUTO, ex)
    assert n == 2 and len(calls) == n  # search-term report still shows the terms


class FakeMeta:
    def __init__(self):
        self.calls = []

    def update_daily_budget(self, oid, budget):
        self.calls.append(("budget", oid, budget))

    def pause(self, oid):
        self.calls.append(("pause", oid))


class FakeShopify:
    def __init__(self):
        self.calls = []

    def update_variant_price(self, vid, price):
        self.calls.append((vid, price))


def test_external_executors(ctx):
    meta, shop = FakeMeta(), FakeShopify()
    ex = build_executors(ctx.wh, shopify=shop, meta=meta)
    _, log, _ = run(ctx, Mode.AUTO, ex)
    assert ("budget", "m1", 32.0) in meta.calls and ("pause", "m2") in meta.calls
    assert shop.calls == [("gid://shopify/ProductVariant/1003", 10.99)]
    assert ctx.wh.query("SELECT status FROM external_campaigns WHERE campaign_id='m2'")[0][
        "status"] == "paused"
    # Google and TikTok have no API executor: logged as failed with the reason.
    failed = {a["target"]: a["notes"] for a in log.list("failed")}
    assert "no API executor for google" in failed["g1"]
    assert "no API executor for tiktok" in failed["t1"]
