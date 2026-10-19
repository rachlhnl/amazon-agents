import gzip
import json
from datetime import date

import httpx
import pytest

from amz_agents.actions import ActionLog
from amz_agents.agents.creative import CreativeAgent, load_brand, review_copy
from amz_agents.config import Mode
from amz_agents.data.clients import GA4Client, LWAToken, MetaAdsClient, PageSpeedClient, SPAPIClient
from amz_agents.data.warehouse import Warehouse
from amz_agents.executors import build_executors
from amz_agents.ingest import (
    ingest_asin_traffic,
    ingest_ga4,
    ingest_listing_assets,
    ingest_meta_creatives,
    ingest_pagespeed,
)
from amz_agents.orchestrator import Orchestrator

from .conftest import ROOT

TODAY = date(2026, 10, 19)


def run(ctx, mode=Mode.RECOMMEND, executors=None):
    orch = Orchestrator(ctx, ActionLog(ctx.wh, mode, executors))
    return orch, orch.run()


def by_kind(submitted, kind):
    return {a.target: a for _, a in submitted if a.kind == kind}


@pytest.fixture
def brand():
    return load_brand(ROOT / "brand.toml")


# ------------------------------------------------------------------ creative


def test_fatigued_ad_flagged_for_refresh(ctx):
    a = by_kind(run(ctx)[1], "refresh_creative")["m1-a1"]
    assert "-42%" in a.reason and "3.4×" in a.reason
    assert a.team == "creative" and a.impact_usd > 0 and a.effort == "M"


def test_ad_spending_without_selling_paused_and_executes_on_meta(ctx):
    class FakeMeta:
        def __init__(self):
            self.paused = []

        def pause(self, oid):
            self.paused.append(oid)

    meta = FakeMeta()
    _, submitted = run(ctx, Mode.AUTO, build_executors(ctx.wh, meta=meta))
    assert "m1-a3" in by_kind(submitted, "pause_creative")
    assert "m1-a3" in meta.paused
    assert ctx.wh.query("SELECT status FROM creatives WHERE ad_id='m1-a3'")[0]["status"] == "paused"


def test_winning_ad_gets_variants(ctx):
    a = by_kind(run(ctx)[1], "new_creative_variants")["m1-a2"]
    assert a.priority == "judgment" and "9 purchases" in a.reason


def test_single_ad_campaigns_not_judged_against_siblings(ctx):
    _, submitted = run(ctx)
    for kind in ("pause_creative", "new_creative_variants"):
        assert not {"g1-a1", "t1-a1"} & set(by_kind(submitted, kind))


def test_listing_asset_gaps(ctx):
    assets = by_kind(run(ctx)[1], "improve_listing_assets")
    assert set(assets) == {"BTL-32-SGE", "LID-STRAW"}
    assert "no A+ content" in assets["LID-STRAW"].reason


def test_listing_title_breaking_rules(ctx):
    a = by_kind(run(ctx)[1], "fix_listing_title")["BTL-64-BLK"]
    assert "BEST" in a.reason


def test_review_copy_rules(brand):
    issues = review_copy("Subject: The BEST bottle EVER, guaranteed to beat Yeti!!\n"
                         "It's amazing.", "email_campaign", brand)
    text = " ".join(issues)
    for expected in ("amazing", "'BEST'", "guaranteed", "Yeti", "exclamation", "all-caps"):
        assert expected in text, expected
    assert review_copy("Subject: Spare lids are back\nKeeps your bottle going.",
                       "email_campaign", brand) == []


def test_drafts_from_other_agents_reviewed_before_the_brief(ctx, monkeypatch, brand):
    drafts = {"email": "Subject: Our AMAZING straw lid\nThe best lid you'll ever buy!!",
              "outreach": "Hi Dana, following up on the 600 units per quarter."}

    def fake_complete(system, prompt, model, max_tokens=1500):
        if "marketing emails" in system:
            return drafts["email"]
        if "B2B follow-up" in system:
            return drafts["outreach"]
        return None  # listing drafts, review themes, voice review, narrative

    monkeypatch.setattr("amz_agents.llm.complete", fake_complete)
    monkeypatch.setattr("amz_agents.llm.available", lambda: True)
    orch, submitted = run(ctx)
    email = by_kind(submitted, "email_campaign")["LID-STRAW"]
    assert email.after["creative_review"]["status"] == "needs changes"
    assert "creative review: needs changes" in email.notes
    outreach = by_kind(submitted, "send_outreach")["L1"]
    assert outreach.after["creative_review"]["status"] == "ok"
    assert ctx.shared["creative_reviews"] >= 2
    assert "creative review: needs changes" in orch.chief.brief(submitted, orch.outcomes)


def test_voice_review_uses_llm_when_available(ctx, monkeypatch, brand):
    monkeypatch.setattr("amz_agents.llm.complete",
                        lambda *a, **k: '```json\n{"issues": ["too salesy"]}\n```')
    assert CreativeAgent(brand)._llm_review(ctx, "x", "email_campaign") == ["voice: too salesy"]


# ------------------------------------------------------------------------ UX


def test_mobile_gap_and_checkout_break_found(ctx):
    funnel = by_kind(run(ctx)[1], "fix_funnel_step")
    assert "funnel/mobile" in funnel and funnel["funnel/mobile"].team == "ux"
    drop = funnel["funnel/purchase-drop"]
    assert "almost all of it on mobile" in drop.reason and drop.priority == "high"


def test_slow_and_inaccessible_pages(ctx):
    _, submitted = run(ctx)
    speed = by_kind(submitted, "improve_page_speed")
    assert "https://yourstore.example" in speed
    assert "https://yourstore.example/products/insulated-bottle-32oz" not in speed
    assert "a11y/https://yourstore.example" in by_kind(submitted, "fix_accessibility")


def test_bad_landing_page_flagged_on_marketing_proposal(ctx):
    _, submitted = run(ctx)
    page = by_kind(submitted, "fix_landing_page")["https://yourstore.example/pages/creators"]
    assert ["pause_campaign", "t1"] in page.after["related"]
    pause = by_kind(submitted, "pause_campaign")["t1"]
    assert "UX: its landing page converts" in pause.notes


def test_amazon_conversion_blames_buy_box_when_lost(ctx):
    a = by_kind(run(ctx)[1], "improve_listing_conversion")["BTL-64-BLK"]
    assert "Buy Box only 55%" in a.reason
    assert ["update_price", "BTL-64-BLK"] in a.after["related"]


def test_amazon_conversion_blames_page_when_buy_box_fine(ctx):
    ctx.wh.execute("UPDATE asin_traffic SET buy_box_pct=0.97 WHERE sku='BTL-64-BLK'")
    a = by_kind(run(ctx)[1], "improve_listing_conversion")["BTL-64-BLK"]
    assert "the page itself" in a.reason
    assert ["fix_listing_title", "BTL-64-BLK"] in a.after["related"]


def test_creative_and_ux_work_needs_a_person_in_auto(ctx):
    run(ctx, Mode.AUTO, {})
    log = ActionLog(ctx.wh, Mode.AUTO)
    statuses = {(a["kind"], a["status"]) for a in log.list()}
    for kind in ("refresh_creative", "fix_funnel_step", "improve_listing_assets"):
        assert (kind, "pending_approval") in statuses


def test_creative_and_ux_team_briefs(ctx):
    orch, submitted = run(ctx)
    ux = orch.chief.team_brief("ux", submitted)
    assert ux.startswith("# UX & web brief") and "Mobile converts far below desktop" in ux
    creative = orch.chief.team_brief("creative", submitted)
    assert "Refresh fatigued ad" in creative and "Mobile converts" not in creative


# ------------------------------------------------------------------ ingests


def test_meta_creatives_two_windows_and_landing_url():
    wh = Warehouse()
    wh.insert_many("external_campaigns", [{"campaign_id": "as1", "platform": "meta",
                                           "sku": "SKU1"}])

    def handler(req):
        if req.url.path.endswith("/insights"):
            since = json.loads(req.url.params["time_range"])["since"]
            recent = since == "2026-10-12"
            return httpx.Response(200, json={"data": [{
                "ad_id": "ad1", "impressions": "1000" if recent else "3000",
                "clicks": "10" if recent else "60", "spend": "5", "frequency": "2.7",
                "actions": [{"action_type": "purchase", "value": "2"}]}]})
        return httpx.Response(200, json={"data": [
            {"id": "ad1", "name": "Video", "status": "ACTIVE", "adset_id": "as1",
             "creative": {"object_story_spec": {"link_data": {"link": "https://s.example/p"}}}},
            {"id": "ad2", "name": "No sku", "status": "ACTIVE", "adset_id": "other"},
        ]})

    meta = MetaAdsClient("1", "t", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert ingest_meta_creatives(wh, meta, today=TODAY) == 1
    c = wh.query("SELECT * FROM creatives")[0]
    assert (c["sku"], c["landing_url"], c["impressions_7d"], c["impressions_prior"]) == \
        ("SKU1", "https://s.example/p", 1000, 3000)
    assert c["purchases_7d"] == 2


def test_pagespeed_scores_site_and_creative_landing_pages():
    wh = Warehouse()
    wh.insert_many("creatives", [
        {"ad_id": "a", "sku": "S", "landing_url": "https://s.example/pages/promo?utm=x"},
        {"ad_id": "b", "sku": "S", "landing_url": "https://www.amazon.com/dp/B0X"},
    ])
    seen = []

    def handler(req):
        seen.append((req.url.params["url"], req.url.params["strategy"]))
        assert req.url.params.get_list("category") == ["performance", "accessibility"]
        mobile = req.url.params["strategy"] == "mobile"
        return httpx.Response(200, json={"lighthouseResult": {
            "categories": {"performance": {"score": 0.42 if mobile else 0.9},
                           "accessibility": {"score": 0.77}},
            "audits": {"largest-contentful-paint": {"numericValue": 4800.0},
                       "cumulative-layout-shift": {"numericValue": 0.12}}}})

    ps = PageSpeedClient("key", http=httpx.Client(transport=httpx.MockTransport(handler)))
    n = ingest_pagespeed(wh, ps, pages=[{"url": "https://s.example/", "type": "home"}],
                         today=TODAY)
    assert n == 2  # amazon.com landing pages aren't ours to test
    p = {r["url"]: r for r in wh.query("SELECT * FROM web_pages")}
    assert p["https://s.example"]["perf_mobile"] == 42
    assert p["https://s.example"]["perf_desktop"] == 90
    assert p["https://s.example/pages/promo"]["page_type"] == "landing"
    assert p["https://s.example/pages/promo"]["lcp_ms_mobile"] == 4800.0


def test_ga4_funnel_and_landing_pages():
    wh = Warehouse()
    wh.insert_many("web_pages", [{"url": "https://s.example/pages/promo", "perf_mobile": 50}])

    def handler(req):
        body = json.loads(req.content)
        assert req.headers["authorization"] == "Bearer tok"
        dims = [d["name"] for d in body["dimensions"]]
        if dims == ["date", "deviceCategory"]:
            rows = [{"dimensionValues": [{"value": "20261018"}, {"value": "mobile"}],
                     "metricValues": [{"value": v} for v in ("600", "27", "11", "4", "124.0")]}]
        else:
            rows = [{"dimensionValues": [{"value": "/pages/promo"}],
                     "metricValues": [{"value": "400"}, {"value": "3"}]},
                    {"dimensionValues": [{"value": "(not set)"}],
                     "metricValues": [{"value": "9"}, {"value": "0"}]}]
        return httpx.Response(200, json={"rows": rows, "rowCount": len(rows)})

    ga = GA4Client("123", lambda: "tok", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert ingest_ga4(wh, ga, "https://s.example/", today=TODAY) == (1, 1)
    f = wh.query("SELECT * FROM web_funnel")[0]
    assert (f["date"], f["device"], f["purchases"]) == ("2026-10-18", "mobile", 4)
    page = wh.query("SELECT * FROM web_pages")[0]
    assert (page["sessions_30d"], page["perf_mobile"]) == (400, 50)  # speed data kept


def _sp(handler):
    sp = SPAPIClient(LWAToken("i", "s", "r"),
                     http=httpx.Client(transport=httpx.MockTransport(handler)))
    sp.sleep = lambda s: None
    return sp


def test_listing_assets_from_catalog_and_aplus():
    wh = Warehouse()
    wh.insert_many("products", [{"sku": "S1", "asin": "A1"}])

    def handler(req):
        p = req.url.path
        if req.url.host == "api.amazon.com":
            return httpx.Response(200, json={"access_token": "t"})
        if p.startswith("/catalog/"):
            imgs = [{"variant": v} for v in ("MAIN", "PT01", "PT02", "PT02")]
            return httpx.Response(200, json={
                "images": [{"marketplaceId": "ATVPDKIKX0DER", "images": imgs}],
                "attributes": {"item_name": [{"value": "Bottle 32oz"}],
                               "bullet_point": [{"value": "a"}, {"value": "b"}]}})
        if p.startswith("/aplus/"):
            return httpx.Response(200, json={"publishRecordList": []})
        return httpx.Response(404)

    assert ingest_listing_assets(wh, _sp(handler)) == 1
    a = wh.query("SELECT * FROM listing_assets")[0]
    assert (a["image_count"], a["bullet_count"], a["has_aplus"], a["title_length"]) == \
        (3, 2, 0, 11)


def test_asin_traffic_from_sales_and_traffic_report():
    wh = Warehouse()
    report = {"salesAndTrafficByAsin": [
        {"sku": "S1", "trafficByAsin": {"sessions": 2000, "pageViews": 2600,
                                        "buyBoxPercentage": 55.0},
         "salesByAsin": {"unitsOrdered": 120}}]}

    def handler(req):
        p = req.url.path
        if req.url.host == "api.amazon.com":
            return httpx.Response(200, json={"access_token": "t"})
        if req.url.host == "files.example":
            return httpx.Response(200, content=gzip.compress(json.dumps(report).encode()))
        if p == "/reports/2021-06-30/reports":
            opts = json.loads(req.content)["reportOptions"]
            assert opts == {"dateGranularity": "DAY", "asinGranularity": "SKU"}
            return httpx.Response(202, json={"reportId": "r1"})
        if p == "/reports/2021-06-30/reports/r1":
            return httpx.Response(200, json={"processingStatus": "DONE",
                                             "reportDocumentId": "d1"})
        if p == "/reports/2021-06-30/documents/d1":
            return httpx.Response(200, json={"url": "https://files.example/d1",
                                             "compressionAlgorithm": "GZIP"})
        return httpx.Response(404)

    assert ingest_asin_traffic(wh, _sp(handler), today=TODAY) == 1
    t = wh.query("SELECT * FROM asin_traffic")[0]
    assert (t["sessions_30d"], t["units_30d"], t["buy_box_pct"]) == (2000, 120, 0.55)
