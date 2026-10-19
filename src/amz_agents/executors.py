"""Executors turn approved actions into API calls, then sync the warehouse.

Only registered when mode is approve/auto, and only for platforms whose
credentials are set. Listing copy, reorders, defect flags, new channel listings,
outreach and emails are carried out by people (see actions.HUMAN_ONLY). An
approved action with no executor is logged as failed with the reason.
"""

from __future__ import annotations

import os
from typing import Any

from .actions import Executor
from .data.clients import AdsClient, MetaAdsClient, ShopifyClient, SPAPIClient
from .data.warehouse import Warehouse


def build_executors(
    wh: Warehouse,
    ads: AdsClient | None = None,
    sp: SPAPIClient | None = None,
    shopify: ShopifyClient | None = None,
    meta: MetaAdsClient | None = None,
) -> dict[str, Executor]:
    out: dict[str, Executor] = {}
    if ads and sp:
        out |= _amazon(wh, ads, sp)
    out |= _external(wh, shopify, meta)
    return out


def from_env(wh: Warehouse) -> dict[str, Executor]:
    """Executors for every platform with credentials in the environment."""
    has = os.getenv
    amazon = has("SP_API_REFRESH_TOKEN") and has("ADS_API_REFRESH_TOKEN")
    return build_executors(
        wh,
        ads=AdsClient.from_env() if amazon else None,
        sp=SPAPIClient.from_env() if amazon else None,
        shopify=ShopifyClient.from_env() if has("SHOPIFY_ACCESS_TOKEN") else None,
        meta=MetaAdsClient.from_env() if has("META_ACCESS_TOKEN") else None,
    )


def _amazon(wh: Warehouse, ads: AdsClient, sp: SPAPIClient) -> dict[str, Executor]:
    seller_id = os.getenv("SP_API_SELLER_ID", "")

    def update_bid(a: dict[str, Any]) -> None:
        ads.update_keyword_bids([{"keywordId": a["target"], "bid": a["after"]["bid"]}])
        wh.execute("UPDATE keywords SET bid=? WHERE keyword_id=?", (a["after"]["bid"], a["target"]))

    def add_negative(a: dict[str, Any]) -> None:
        campaign_id, ad_group_id = a["target"].split("/")
        ads.create_negative_keywords([{
            "campaignId": campaign_id, "adGroupId": ad_group_id,
            "keywordText": a["after"]["keyword"], "matchType": "NEGATIVE_EXACT", "state": "ENABLED",
        }])

    def add_keyword(a: dict[str, Any]) -> None:
        # Harvest into the ad group that already holds this SKU's exact keywords.
        home = wh.query(
            "SELECT campaign_id, ad_group_id FROM keywords WHERE sku=? AND match_type='exact' LIMIT 1",
            (a["target"],),
        )
        if not home:
            raise RuntimeError(f"no exact-match ad group for {a['target']}; create one first")
        ads.create_keywords([{
            "campaignId": home[0]["campaign_id"], "adGroupId": home[0]["ad_group_id"],
            "keywordText": a["after"]["keyword"], "matchType": "EXACT",
            "bid": a["after"]["bid"], "state": "ENABLED",
        }])

    def update_price(a: dict[str, Any]) -> None:
        if not seller_id:
            raise RuntimeError("SP_API_SELLER_ID not set")
        p = wh.query("SELECT product_type FROM products WHERE sku=?", (a["target"],))[0]
        sp.patch_listing_price(seller_id, a["target"], p["product_type"], a["after"]["price"])
        wh.execute("UPDATE products SET price=? WHERE sku=?", (a["after"]["price"], a["target"]))

    return {  # Amazon
        "update_bid": update_bid,
        "add_negative": add_negative,
        "add_keyword": add_keyword,
        "update_price": update_price,
    }


def _external(
    wh: Warehouse, shopify: ShopifyClient | None, meta: MetaAdsClient | None
) -> dict[str, Executor]:
    def campaign(a: dict[str, Any]) -> dict[str, Any]:
        c = wh.query("SELECT * FROM external_campaigns WHERE campaign_id=?", (a["target"],))
        if not c:
            raise RuntimeError(f"campaign {a['target']} not in warehouse")
        if c[0]["platform"] != "meta" or meta is None:
            raise RuntimeError(f"no API executor for {c[0]['platform']}: change it in that "
                               "platform's ads manager by hand")
        return c[0]

    def update_campaign_budget(a: dict[str, Any]) -> None:
        campaign(a)
        meta.update_daily_budget(a["target"], a["after"]["daily_budget"])
        wh.execute("UPDATE external_campaigns SET daily_budget=? WHERE campaign_id=?",
                   (a["after"]["daily_budget"], a["target"]))

    def pause_campaign(a: dict[str, Any]) -> None:
        campaign(a)
        meta.pause(a["target"])
        wh.execute("UPDATE external_campaigns SET status='paused' WHERE campaign_id=?",
                   (a["target"],))

    def update_channel_price(a: dict[str, Any]) -> None:
        channel, sku = a["target"].split("/", 1)
        if channel != "shopify" or shopify is None:
            raise RuntimeError(f"no API executor for {channel}: change the price there")
        row = wh.query("SELECT external_id FROM channel_listings WHERE channel=? AND sku=?",
                       (channel, sku))
        if not row or not row[0]["external_id"]:
            raise RuntimeError(f"no Shopify variant ID for {sku}")
        shopify.update_variant_price(row[0]["external_id"], a["after"]["price"])
        wh.execute("UPDATE channel_listings SET price=? WHERE channel=? AND sku=?",
                   (a["after"]["price"], channel, sku))

    def pause_creative(a: dict[str, Any]) -> None:
        ad = wh.query("SELECT platform FROM creatives WHERE ad_id=?", (a["target"],))
        if not ad or ad[0]["platform"] != "meta" or meta is None:
            platform = ad[0]["platform"] if ad else "unknown platform"
            raise RuntimeError(f"no API executor for {platform}: pause the ad there by hand")
        meta.pause(a["target"])
        wh.execute("UPDATE creatives SET status='paused' WHERE ad_id=?", (a["target"],))

    return {
        "pause_creative": pause_creative,
        "update_campaign_budget": update_campaign_budget,
        "pause_campaign": pause_campaign,
        "update_channel_price": update_channel_price,
    }
