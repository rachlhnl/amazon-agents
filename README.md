# amz-agents

An agent suite that optimizes a brand's sales on Amazon (ads, listings, pricing,
inventory, reviews) and outside it (paid traffic from Meta/Google/TikTok, email,
its own store and other marketplaces, wholesale), with hard guardrails and a
human-approval workflow.

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
| Marketing | external campaigns, stock | `update_campaign_budget`, `pause_campaign`, `email_campaign` (+ LLM draft) | Meta Marketing API; human for Google/TikTok and email |
| Sales | other channels' prices, CRM leads | `update_channel_price`, `list_on_channel`, `send_outreach` (+ LLM draft) | Shopify Admin API; human otherwise |
| Data | everything, plus the other agents' proposals | `opportunity` (finding, recommendation, owner, impact, effort) | human |

Each agent declares a **role** (`agents/base.py`: `Role`): its mission, the team
that owns its work (Amazon operations, Marketing, Sales & wholesale, Data &
insights), the action types it proposes with the level of effort each takes a
person (XS–XL), and the warehouse tables it needs. The chief of staff reads the
roles to route, prioritize and check on the agents; adding an agent means
declaring its role, not editing the chief of staff.

The orchestrator runs them in that order. Inventory and reviews publish findings
to a shared context that later agents use: the ads agent won't raise bids and the
pricing agent won't cut prices on a SKU that is about to stock out; the marketing
agent pauses outside campaigns that would sell that stock; the sales agent warns
not to promise wholesale volume on it, and checks off-Amazon prices against the
Amazon price the pricing agent is about to set.

An action already awaiting approval, or carried out in the last 30 days, isn't
proposed again.

### Data agent

Runs last. It reviews the warehouse for opportunities the other agents' rules
don't cover, and for problems in the data they all depend on:

- **Data quality:** stale daily sales, products missing costs, Amazon-bound
  campaigns with no attributed sales (Attribution not loaded?), keywords not tied
  to a product.
- **Opportunities:** in-stock SKUs with no advertising, fulfillment fees eating
  margin, SKUs that depend on ads for most sales (weak organic rank), sales
  trending sharply up or down, price elasticity measured from past price changes
  vs. the assumption the estimates use, Amazon's share of total sales, and
  products rated under 4 stars.

It works with the other agents: each opportunity is routed to the agent whose
role covers it (so it lands with that agent's team), linked to the proposals
that already work on it ("in progress via [12], [15]"), and sized with a 30-day
impact and a level of effort. Opportunities are recommendations for a person
and are never executed. Once acknowledged, one stays quiet for 30 days.

### Priority, ROI and effort

The chief of staff scores every action and opportunity the same way:
**ROI** = estimated 30-day impact ÷ (effort hours × `HOURLY_COST`, default $75).
Effort comes from the proposing agent's role (or the data agent's estimate);
in `auto` mode, changes the system carries out count as XS.
**Priority:** `high` (≥ $500 and ROI ≥ 3), `medium` (ROI ≥ 1), `low` (below that,
or negative impact), `judgment` (no dollar estimate: outreach, defects, listings),
and `now` for escalations. The queue is ordered by priority, then impact.

### Marketing agent (outside Amazon)

Judges each external campaign by **profit per $1 of spend** over 30 days:
sales × unit margin on the destination channel, plus Amazon's Brand Referral
Bonus (~10%, set in `agents/marketing.py`) when the campaign sends shoppers to
Amazon via Amazon Attribution. At $1.50 or more it raises the budget; below $1 it
cuts it (both by at most `MAX_BUDGET_CHANGE_PCT`, never above `MAX_DAILY_BUDGET`).
It pauses campaigns with 150+ clicks and no orders, and campaigns that would
draw on FBA stock (Amazon traffic, or a channel fulfilled by Multi-Channel
Fulfillment) for a SKU about to stock out. For overstocked SKUs with no campaign
behind them, it drafts an email to the customer list for a person to send.

### Sales agent (outside Amazon)

- **Price parity.** Amazon's fair pricing policy can remove the Buy Box when a
  product is noticeably cheaper elsewhere, so off-Amazon prices more than 2% below
  Amazon are raised toward it, within the price guardrails.
- **Margin floor** on each channel after that channel's fees.
- **New channels.** SKUs selling 5+/day on Amazon (and not short on stock) that
  aren't listed on a channel the company already uses.
- **Wholesale follow-ups.** Leads quiet for longer than their stage allows (new 2
  days, negotiating 3, contacted 7), biggest first, with a drafted email and a
  warning if they want a SKU that's about to stock out.

## Chief of staff

The agents propose; the chief of staff turns their proposals into a plan a busy
person can act on in five minutes. It doesn't make changes itself. Each run it:

1. **Tracks goals** from `goals.toml` (unit or revenue growth per SKU, a TACoS
   ceiling), comparing the last 7 days with a baseline of days 15–28 ago.
2. **Estimates the 30-day profit impact** of every proposed action and ranks the
   approval queue by it. The assumptions (price elasticity, how much sales move
   with bids) are constants at the top of `agents/chief_of_staff.py`. Tune them
   once you have the company's own results.
3. **Tags each action** with the goal it serves or works against.
4. **Escalates** what needs a person: goals that can't be met (e.g. a growth goal
   on a SKU about to stock out) and anomalies (a sudden sales drop, with likely
   causes such as a lost Buy Box; an ad-spend spike).
5. **Checks past changes.** A week after an action executes, it compares the
   SKU's daily profit before and after, records a verdict, and says which action
   types have earned more autonomy and which need tighter guardrails.
6. **Checks on the agents:** whether each one ran, has the data its role needs
   (and whether daily sales are fresh), and only proposed the action types it
   declared. Problems go to the top of the brief; one failing agent no longer
   stops the run.
7. **Writes the brief**: needs your attention, goals, sales by channel, top
   priorities across teams, the data agent's opportunities, then a section per
   team (ranked queue, low priority, consider rejecting, judgment calls), whether
   past changes worked, and agent health. It also writes a brief per team to
   `reports/<team>/`. With SMTP settings in `.env`, `amz-agents run` emails the
   full brief to `BRIEF_TO` and each team's brief to `BRIEF_TO_<TEAM>`.

The before/after checks need daily per-SKU history in `metrics_daily`
(Sales & Traffic report plus Ads reports; see `ingest.py`).

Off-Amazon goals aren't tracked yet: `goals.toml` metrics are measured on Amazon
sales, and outside spend doesn't count toward TACoS. The brief shows units by
channel.

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
3. Load data with `amz-agents ingest --costs costs.csv` (nightly, before `run`).
   It runs every job whose credentials are set:

   | Job | Source | Fills |
   |---|---|---|
   | products | Listings Items, Product Fees, Product Pricing | `products` |
   | metrics | orders flat-file report + Ads `spAdvertisedProduct` (60-day backfill) | `metrics_daily` |
   | inventory | FBA Inventory; velocity from `metrics_daily` | `products` stock |
   | ads | Ads `spTargeting`, `spSearchTerm` (30 days) | `keywords`, `search_terms` |
   | costs | your CSV: `sku,landed_cost,lead_time_days` | `products` |
   | shopify | Shopify Admin GraphQL | `channel_listings` |
   | meta | Meta Marketing API ad sets + insights | `external_campaigns` |

   Everything else comes in as a CSV export with `amz-agents import TABLE FILE`
   (header = column names; existing rows update only the columns given):
   reviews from a licensed vendor, `leads` from the CRM, Google Ads/TikTok
   campaigns and Amazon Attribution results into `external_campaigns`, other
   marketplaces into `channel_listings`.

   **Meta naming convention:** put `sku=<SKU> dest=amazon` (or `dest=shopify`) in
   each ad set's name. That's how the agent knows what an ad set sells; ad sets
   without it are skipped.
4. `amz-agents run` in `AGENT_MODE=recommend`. Review `reports/` for a few weeks.
5. Switch to `approve`, then use `amz-agents actions --status pending_approval`,
   `amz-agents approve <id>`, `amz-agents reject <id>`.
6. Enable `auto` per action type only once the logs back it up.

Amazon's official Ads MCP server (open beta since Feb 2026, for Ads API partners)
can replace `AdsClient` as the action layer if you'd rather route through MCP.

Endpoint paths, report types and payloads in `data/clients.py` and `ingest.py`
follow the SP-API, Ads API v3, Shopify Admin GraphQL and Meta Marketing API docs,
and are tested against mocked responses, not live accounts. Verify them against
current docs and check a first ingest against Seller Central before going live:
- https://developer-docs.amazon.com/sp-api/
- https://advertising.amazon.com/API/docs/
- https://shopify.dev/docs/api/admin-graphql
- https://developers.facebook.com/docs/marketing-api/

## Layout

```
src/amz_agents/
  config.py          settings + guardrail limits from env
  guardrails.py      hard limits on bids, budgets, prices
  actions.py         action log, modes, approval flow
  executors.py       approved actions -> API calls
  orchestrator.py    runs agents, hands proposals to the chief of staff
  notify.py          emails the brief (optional)
  llm.py             optional Claude calls
  ingest.py          API -> warehouse jobs, CSV imports
  agents/            inventory, reviews, pricing, ads, listing, marketing, sales,
                     data, chief_of_staff; base.py holds roles, teams, effort scale
  data/              warehouse (SQLite), API clients, sample data
goals.toml           goals the chief of staff steers toward
tests/               guardrail, agent and chief-of-staff tests
```

## Compliance notes

- Don't solicit, incentivize, filter, or respond to reviews in ways that break
  Amazon's Community Guidelines. The reviews agent is read-only for this reason.
- Don't scrape Amazon pages; use the APIs or licensed data.
- Listing copy must follow Amazon's style guide for each category; drafts are
  never published without review.
- Marketing emails go only to people who opted in to the brand's list, and
  wholesale outreach must follow CAN-SPAM (or local equivalents). Both are
  drafted for a person to send; nothing is emailed automatically.
