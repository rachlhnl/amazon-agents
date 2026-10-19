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

import gzip
import json
import os
import time
from collections.abc import Callable, Iterator
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


class ReportFailed(RuntimeError):
    pass


class _Base:
    # Report polling; tests swap in a no-op sleep.
    poll_interval: float = 30.0
    poll_timeout: float = 3600.0
    sleep: Callable[[float], None] = staticmethod(time.sleep)

    def __init__(self, token: LWAToken, host: str, http: httpx.Client | None = None):
        self.token = token
        self.host = host
        self.http = http or httpx.Client(timeout=30)

    def _poll(self, check: Callable[[], str | None], what: str) -> str:
        """Call check() until it returns a value (the download handle) or times out."""
        waited = 0.0
        while waited <= self.poll_timeout:
            result = check()
            if result:
                return result
            self.sleep(self.poll_interval)
            waited += self.poll_interval
        raise ReportFailed(f"{what} not ready after {self.poll_timeout:.0f}s")

    def _download(self, url: str, gzipped: bool) -> bytes:
        """Report files live at pre-signed URLs: no auth headers."""
        r = self.http.get(url)
        r.raise_for_status()
        return gzip.decompress(r.content) if gzipped else r.content

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

    def post(self, path: str, body: dict[str, Any], **params: Any) -> dict[str, Any]:
        return self._request("POST", path, params=params, json=body).json()

    def report(
        self, report_type: str, start: str, end: str, options: dict[str, str] | None = None
    ) -> bytes:
        """Reports API 2021-06-30: create, poll until DONE, download (decompressed) bytes."""
        body: dict[str, Any] = {
            "reportType": report_type, "marketplaceIds": [self.marketplace_id],
            "dataStartTime": start, "dataEndTime": end,
        }
        if options:
            body["reportOptions"] = options
        report_id = self.post("/reports/2021-06-30/reports", body)["reportId"]

        def check() -> str | None:
            r = self.get(f"/reports/2021-06-30/reports/{report_id}")
            if r["processingStatus"] in ("CANCELLED", "FATAL"):
                raise ReportFailed(f"{report_type} {report_id}: {r['processingStatus']}")
            return r.get("reportDocumentId") if r["processingStatus"] == "DONE" else None

        doc = self.get(f"/reports/2021-06-30/documents/{self._poll(check, report_type)}")
        return self._download(doc["url"], doc.get("compressionAlgorithm") == "GZIP")

    def paginate(self, path: str, key: Callable[[dict], list], **params: Any) -> Iterator[dict]:
        """Follow nextToken/pageToken pagination (location varies by API)."""
        token_param = params.pop("_token_param", "nextToken")
        while True:
            body = self.get(path, **params)
            yield from key(body)
            nxt = (body.get("pagination") or {}).get("nextToken") or body.get("nextToken")
            if not nxt:
                return
            params[token_param] = nxt

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

    def report(
        self, report_type: str, start: str, end: str, group_by: list[str], columns: list[str],
        time_unit: str = "SUMMARY",
    ) -> list[dict[str, Any]]:
        """Ads Reporting v3 for Sponsored Products: create, poll, download gzipped JSON."""
        ct = "application/vnd.createasyncreportrequest.v3+json"
        body = {
            "name": f"{report_type} {start}..{end}", "startDate": start, "endDate": end,
            "configuration": {
                "adProduct": "SPONSORED_PRODUCTS", "reportTypeId": report_type,
                "groupBy": group_by, "columns": columns, "timeUnit": time_unit,
                "format": "GZIP_JSON",
            },
        }
        r = self._request("POST", "/reporting/reports", json=body, headers={"Content-Type": ct})
        report_id = r.json()["reportId"]

        def check() -> str | None:
            s = self._request("GET", f"/reporting/reports/{report_id}").json()
            if s["status"] == "FAILED":
                raise ReportFailed(f"{report_type} {report_id}: {s.get('failureReason')}")
            return s.get("url") if s["status"] == "COMPLETED" else None

        return json.loads(self._download(self._poll(check, report_type), gzipped=True))

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


# ---------------------------------------------------------------- outside Amazon


class ShopifyClient:
    """Shopify Admin GraphQL API with a custom-app access token.

    Verify against https://shopify.dev/docs/api/admin-graphql before going live.
    """

    def __init__(self, shop: str, token: str, api_version: str = "2025-10",
                 http: httpx.Client | None = None):
        self.url = f"https://{shop}/admin/api/{api_version}/graphql.json"
        self.token = token
        self.http = http or httpx.Client(timeout=30)

    @classmethod
    def from_env(cls) -> ShopifyClient:
        return cls(os.environ["SHOPIFY_SHOP"], os.environ["SHOPIFY_ACCESS_TOKEN"],
                   os.getenv("SHOPIFY_API_VERSION", "2025-10"))

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(5):
            r = self.http.post(self.url, json={"query": query, "variables": variables or {}},
                               headers={"X-Shopify-Access-Token": self.token})
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(min(2**attempt, 30))
                continue
            r.raise_for_status()
            body = r.json()
            throttled = any((e.get("extensions") or {}).get("code") == "THROTTLED"
                            for e in body.get("errors") or [])
            if throttled and attempt < 4:
                time.sleep(min(2**attempt, 30))
                continue
            if body.get("errors"):
                raise RuntimeError(f"Shopify GraphQL errors: {body['errors']}")
            return body["data"]
        raise RuntimeError("Shopify throttled after retries")

    def nodes(self, query: str, path: str, variables: dict[str, Any] | None = None
              ) -> Iterator[dict[str, Any]]:
        """Cursor pagination: `query` takes $after and returns `path { nodes pageInfo }`."""
        after = None
        while True:
            conn = self.graphql(query, {**(variables or {}), "after": after})[path]
            yield from conn["nodes"]
            if not conn["pageInfo"]["hasNextPage"]:
                return
            after = conn["pageInfo"]["endCursor"]

    def update_variant_price(self, variant_id: str, price: float) -> None:
        product = self.graphql(
            "query($id: ID!) { productVariant(id: $id) { product { id } } }", {"id": variant_id}
        )["productVariant"]["product"]["id"]
        res = self.graphql(
            "mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {"
            " productVariantsBulkUpdate(productId: $productId, variants: $variants) {"
            " userErrors { field message } } }",
            {"productId": product, "variants": [{"id": variant_id, "price": f"{price:.2f}"}]},
        )["productVariantsBulkUpdate"]
        if res["userErrors"]:
            raise RuntimeError(f"Shopify: {res['userErrors']}")


class MetaAdsClient:
    """Meta Marketing API (Graph API) with a system-user access token.

    Budgets live on ad sets (or on campaigns using campaign budget optimization);
    `external_campaigns.campaign_id` holds whichever object carries the budget.
    Verify against https://developers.facebook.com/docs/marketing-api/ before going live.
    """

    def __init__(self, account_id: str, token: str, api_version: str = "v23.0",
                 http: httpx.Client | None = None):
        self.base = f"https://graph.facebook.com/{api_version}"
        self.account = account_id if account_id.startswith("act_") else f"act_{account_id}"
        self.token = token
        self.http = http or httpx.Client(timeout=30)

    @classmethod
    def from_env(cls) -> MetaAdsClient:
        return cls(os.environ["META_AD_ACCOUNT_ID"], os.environ["META_ACCESS_TOKEN"],
                   os.getenv("META_API_VERSION", "v23.0"))

    def _req(self, method: str, path: str, **params: Any) -> dict[str, Any]:
        for attempt in range(5):
            r = self.http.request(method, f"{self.base}/{path}",
                                  params={**params, "access_token": self.token})
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(min(2**attempt, 30))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("unreachable")

    def list(self, edge: str, **params: Any) -> Iterator[dict[str, Any]]:
        body = self._req("GET", f"{self.account}/{edge}", **params)
        while True:
            yield from body.get("data", [])
            nxt = (body.get("paging") or {}).get("next")
            if not nxt:
                return
            r = self.http.get(nxt)  # the next URL already carries the token and cursor
            r.raise_for_status()
            body = r.json()

    def update_daily_budget(self, object_id: str, budget: float) -> None:
        # Meta budgets are in the account currency's minor unit (cents).
        self._req("POST", object_id, daily_budget=str(round(budget * 100)))

    def pause(self, object_id: str) -> None:
        self._req("POST", object_id, status="PAUSED")


class PageSpeedClient:
    """Google PageSpeed Insights v5 (Lighthouse). An API key raises the quota."""

    URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

    def __init__(self, api_key: str | None = None, http: httpx.Client | None = None):
        self.api_key = api_key
        self.http = http or httpx.Client(timeout=120)  # a Lighthouse run takes a while

    @classmethod
    def from_env(cls) -> PageSpeedClient:
        return cls(os.getenv("PAGESPEED_API_KEY"))

    def run(self, url: str, strategy: str) -> dict[str, Any]:
        params: list[tuple[str, str]] = [("url", url), ("strategy", strategy),
                                         ("category", "performance"),
                                         ("category", "accessibility")]
        if self.api_key:
            params.append(("key", self.api_key))
        for attempt in range(4):
            r = self.http.get(self.URL, params=params)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(min(2**attempt, 30))
                continue
            r.raise_for_status()
            return r.json()["lighthouseResult"]
        raise RuntimeError("unreachable")


class GA4Client:
    """Google Analytics 4 Data API (runReport).

    Auth: GA4_ACCESS_TOKEN, or a service account JSON in GA4_CREDENTIALS (needs the
    optional google-auth package: pip install -e ".[ga4]"). Add the service account's
    email as a Viewer on the GA4 property.
    """

    def __init__(self, property_id: str, token: Callable[[], str],
                 http: httpx.Client | None = None):
        self.url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
        self.token = token
        self.http = http or httpx.Client(timeout=60)

    @classmethod
    def from_env(cls) -> GA4Client:
        if os.getenv("GA4_ACCESS_TOKEN"):
            static = os.environ["GA4_ACCESS_TOKEN"]
            return cls(os.environ["GA4_PROPERTY_ID"], lambda: static)
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            os.environ["GA4_CREDENTIALS"],
            scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        )

        def token() -> str:
            if not creds.valid:
                creds.refresh(Request())
            return creds.token

        return cls(os.environ["GA4_PROPERTY_ID"], token)

    def report(self, dimensions: list[str], metrics: list[str], start: str, end: str
               ) -> list[dict[str, str]]:
        """Rows as {dimension or metric name: value}, all pages."""
        out, offset = [], 0
        while True:
            body = {"dimensions": [{"name": d} for d in dimensions],
                    "metrics": [{"name": m} for m in metrics],
                    "dateRanges": [{"startDate": start, "endDate": end}],
                    "limit": 10000, "offset": offset}
            r = self.http.post(self.url, json=body,
                               headers={"Authorization": f"Bearer {self.token()}"})
            r.raise_for_status()
            data = r.json()
            for row in data.get("rows", []):
                vals = [v["value"] for v in row.get("dimensionValues", [])]
                vals += [v["value"] for v in row.get("metricValues", [])]
                out.append(dict(zip(dimensions + metrics, vals)))
            offset += 10000
            if offset >= int(data.get("rowCount", 0)):
                return out
