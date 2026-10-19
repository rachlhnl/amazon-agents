"""Ingestion jobs: pull from Amazon APIs into the warehouse on a schedule.

Each function is a stub with the source it should use. Implement these first;
agents are only as good as the data they read. Run hourly/nightly via cron,
Airflow, or a scheduled job.
"""

from __future__ import annotations

from .data.clients import AdsClient, SPAPIClient
from .data.warehouse import Warehouse


def ingest_products(wh: Warehouse, sp: SPAPIClient) -> None:
    """products: price, product_type -> Listings Items API (searchListingsItems);
    landed_cost -> your ERP/COGS sheet; fees -> Product Fees API (getMyFeesEstimates);
    buybox_price -> Product Pricing API (competitive summary)."""
    raise NotImplementedError


def ingest_inventory(wh: Warehouse, sp: SPAPIClient) -> None:
    """units_on_hand/inbound -> FBA Inventory API (getInventorySummaries);
    daily_velocity -> Orders API v2026-01-01 or Sales & Traffic report, trailing 30d;
    lead_time_days -> supplier master data."""
    raise NotImplementedError


def ingest_ads(wh: Warehouse, ads: AdsClient) -> None:
    """keywords + search_terms -> Ads Reporting v3 (async: create report, poll, download
    gzipped JSON). Report types: spKeywords / spTargeting and spSearchTerm, last 30 days."""
    raise NotImplementedError


def ingest_reviews(wh: Warehouse) -> None:
    """reviews -> no general review API exists for sellers. Options: Customer Feedback API
    (brand-registered, topic-level), Brand Analytics, or a licensed data vendor. Do not
    scrape Amazon in violation of its terms."""
    raise NotImplementedError
