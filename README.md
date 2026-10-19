# amz-agents

An agent suite that optimizes Amazon product sales across ads, listings, pricing,
inventory, and reviews, with hard guardrails and a human-approval workflow.

## Design principles

1. **Agents read from a warehouse, never from live APIs.** Ingestion jobs pull
   from SP-API and the Ads API on a schedule; agents query a local store. That
   keeps you under rate limits and makes every decision reproducible.
2. **Decisions with money attached are deterministic and tested.** Bids, prices
   and reorders come from plain rules you can read and unit-test. The LLM drafts
   listing copy, extracts review themes, and writes the daily brief.
3. **Guardrails live in code, not prompts.** Every bid and price passes through
   `guardrails.py` (max change per day, absolute min/max, margin floor).
4. **Every change is an `Action`** with before/after values and a reason, logged
   before anything executes, so you can attribute lift and roll back.
5. **Autonomy is earned.** Start in `recommend`, move to `approve`, and enable
   `auto` once logs show the agents are right. Customer-facing and real-world
   actions (listing copy, purchase orders, defect flags) always need a human.

## Agents

| Agent | Reads | Proposes | Executes via |
|---|---|---|---|
| Inventory | stock, velocity, lead time | `reorder` | human |
| Reviews | low-rated reviews | `flag_defect` | human |
| Pricing | price, cost, fees, Buy Box | `update_price` | SP-API Listings Items |
| Ads | keywords, search terms | `update_bid`, `add_keyword`, `add_negative` | Ads API (SP v3) |
| Listing | search terms, review themes | `update_listing_copy` (+ LLM draft) | human |

The orchestrator runs them in that order. Inventory and reviews publish findings
to a shared context that later agents use: the ads agent won't raise bids and the
pricing agent won't cut prices on a SKU that is about to stock out.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # optional for the demo

amz-agents demo               # seeds sample data, runs every agent, writes reports/brief-*.md
pytest -q
```

Set `ANTHROPIC_API_KEY` to turn on LLM listing drafts, review-theme extraction,
and the narrative in the daily brief. Everything else runs without it.

## Running against real accounts

1. Register as an SP-API developer and an Amazon Ads API partner. For a single
   company's own account, a self-authorized private SP-API app is enough.
2. Fill in `.env` (LWA client ID/secret and refresh tokens for both APIs,
   Ads profile ID, `SP_API_SELLER_ID`).
3. Implement the stubs in `src/amz_agents/ingest.py`. Each one names the API or
   report to use. Schedule them hourly or nightly.
4. `amz-agents run` in `AGENT_MODE=recommend`. Review `reports/` for a few weeks.
5. Switch to `approve`, then use `amz-agents actions --status pending_approval`,
   `amz-agents approve <id>`, `amz-agents reject <id>`.
6. Enable `auto` per action type only once the logs back it up.

Amazon's official Ads MCP server (open beta since Feb 2026, for Ads API partners)
can replace `AdsClient` as the action layer if you'd rather route through MCP.

Endpoint paths and payloads in `data/clients.py` follow the SP-API Listings
Items API and Sponsored Products v3. Verify them against current docs before
going live:
- https://developer-docs.amazon.com/sp-api/
- https://advertising.amazon.com/API/docs/

## Layout

```
src/amz_agents/
  config.py          settings + guardrail limits from env
  guardrails.py      hard limits on bids, budgets, prices
  actions.py         action log, modes, approval flow
  executors.py       approved actions -> API calls
  orchestrator.py    runs agents, writes the daily brief
  llm.py             optional Claude calls
  ingest.py          API -> warehouse jobs (stubs to implement)
  agents/            inventory, reviews, pricing, ads, listing
  data/              warehouse (SQLite), API clients, sample data
tests/               guardrail and agent behavior tests
```

## Compliance notes

- Don't solicit, incentivize, filter, or respond to reviews in ways that break
  Amazon's Community Guidelines. The reviews agent is read-only for this reason.
- Don't scrape Amazon pages; use the APIs or licensed data.
- Listing copy must follow Amazon's style guide for each category; drafts are
  never published without review.
