"""Executors turn approved actions into API calls, then sync the warehouse.

Only registered when mode is approve/auto. Listing copy, reorders and defect
flags are carried out by people (see actions.HUMAN_ONLY).
"""

from __future__ import annotations

import os
from typing import Any

from .actions import Executor
from .data.clients import AdsClient, SPAPIClient
from .data.warehouse import Warehouse


def build_executors(wh: Warehouse, ads: AdsClient, sp: SPAPIClient) -> dict[str, Executor]:
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

    return {
        "update_bid": update_bid,
        "add_negative": add_negative,
        "add_keyword": add_keyword,
        "update_price": update_price,
    }
