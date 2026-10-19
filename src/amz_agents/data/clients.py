"""Thin clients for Amazon's Selling Partner API and Ads API.

Both use Login with Amazon (LWA) refresh-token auth. These are deliberately
minimal: ingestion jobs call them and write to the warehouse; agents never do.

Before production use, check endpoint paths and payloads against current docs:
  SP-API:  https://developer-docs.amazon.com/sp-api/
  Ads API: https://advertising.amazon.com/API/docs/
Amazon also offers an official Ads MCP server (open beta, Feb 2026) that can
replace AdsClient as the action layer if you're an Ads API partner.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

SP_API_HOSTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}

ADS_API_HOSTS = {
    "na": "https://advertising-api.amazon.com",
    "eu": "https://advertising-api-eu.amazon.com",
    "fe": "https://advertising-api-fe.amazon.com",
}


@dataclass
class LWAToken:
    client_id: str
    client_secret: str
    refresh_token: str
    _access: str | None = field(default=None, repr=False)
    _expires: float = 0.0

    def get(self, http: httpx.Client) -> str:
        if self._access and time.time() < self._expires - 60:
            return self._access
        r = http.post(
            LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        r.raise_for_status()
        body = r.json()
        self._access = body["access_token"]
        self._expires = time.time() + int(body.get("expires_in", 3600))
        return self._access


class _Base:
    def __init__(self, token: LWAToken, host: str, http: httpx.Client | None = None):
        self.token = token
        self.host = host
        self.http = http or httpx.Client(timeout=30)

    def _request(self, method: str, path: str, *, retries: int = 4, **kw: Any) -> httpx.Response:
        """Request with exponential backoff on 429/5xx (both APIs throttle hard)."""
        extra_headers = kw.pop("headers", {})
        for attempt in range(retries + 1):
            headers = {**self._headers(), **extra_headers}
            r = self.http.request(method, self.host + path, headers=headers, **kw)
            if r.status_code not in (429, 500, 502, 503, 504) or attempt == retries:
                r.raise_for_status()
                return r
            time.sleep(min(2**attempt, 30))
        raise RuntimeError("unreachable")

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError


class SPAPIClient(_Base):
    def __init__(self, token: LWAToken, region: str = "na", marketplace_id: str = "ATVPDKIKX0DER", **kw):
        super().__init__(token, SP_API_HOSTS[region], **kw)
        self.marketplace_id = marketplace_id

    @classmethod
    def from_env(cls) -> SPAPIClient:
        return cls(
            LWAToken(
                os.environ["SP_API_CLIENT_ID"],
                os.environ["SP_API_CLIENT_SECRET"],
                os.environ["SP_API_REFRESH_TOKEN"],
            ),
            region=os.getenv("SP_API_REGION", "na"),
            marketplace_id=os.getenv("SP_API_MARKETPLACE_ID", "ATVPDKIKX0DER"),
        )

    def _headers(self) -> dict[str, str]:
        return {"x-amz-access-token": self.token.get(self.http)}

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        return self._request("GET", path, params=params).json()

    def patch_listing_price(self, seller_id: str, sku: str, product_type: str, price: float) -> dict:
        """Update a listing's price via the Listings Items API (verify payload vs. current docs)."""
        body = {
            "productType": product_type,
            "patches": [
                {
                    "op": "replace",
                    "path": "/attributes/purchasable_offer",
                    "value": [
                        {
                            "marketplace_id": self.marketplace_id,
                            "currency": "USD",
                            "our_price": [{"schedule": [{"value_with_tax": price}]}],
                        }
                    ],
                }
            ],
        }
        r = self._request(
            "PATCH",
            f"/listings/2021-08-01/items/{seller_id}/{sku}",
            params={"marketplaceIds": self.marketplace_id},
            json=body,
        )
        return r.json()


class AdsClient(_Base):
    def __init__(self, token: LWAToken, profile_id: str, region: str = "na", **kw):
        super().__init__(token, ADS_API_HOSTS[region], **kw)
        self.profile_id = profile_id

    @classmethod
    def from_env(cls) -> AdsClient:
        return cls(
            LWAToken(
                os.environ["ADS_API_CLIENT_ID"],
                os.environ["ADS_API_CLIENT_SECRET"],
                os.environ["ADS_API_REFRESH_TOKEN"],
            ),
            profile_id=os.environ["ADS_API_PROFILE_ID"],
            region=os.getenv("ADS_API_REGION", "na"),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token.get(self.http)}",
            "Amazon-Advertising-API-ClientId": self.token.client_id,
            "Amazon-Advertising-API-Scope": self.profile_id,
        }

    def update_keyword_bids(self, updates: list[dict[str, Any]]) -> dict:
        """Sponsored Products v3: [{'keywordId': ..., 'bid': ...}]"""
        ct = "application/vnd.spKeyword.v3+json"
        r = self._request(
            "PUT", "/sp/keywords", json={"keywords": updates},
            headers={"Content-Type": ct, "Accept": ct},
        )
        return r.json()

    def create_negative_keywords(self, negatives: list[dict[str, Any]]) -> dict:
        """Sponsored Products v3: [{'campaignId','adGroupId','keywordText','matchType','state'}]"""
        ct = "application/vnd.spNegativeKeyword.v3+json"
        r = self._request(
            "POST", "/sp/negativeKeywords", json={"negativeKeywords": negatives},
            headers={"Content-Type": ct, "Accept": ct},
        )
        return r.json()

    def create_keywords(self, keywords: list[dict[str, Any]]) -> dict:
        """Sponsored Products v3: [{'campaignId','adGroupId','keywordText','matchType','bid','state'}]"""
        ct = "application/vnd.spKeyword.v3+json"
        r = self._request(
            "POST", "/sp/keywords", json={"keywords": keywords},
            headers={"Content-Type": ct, "Accept": ct},
        )
        return r.json()
