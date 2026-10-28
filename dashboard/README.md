# Dagacci KPI dashboard

Live page: https://claude.ai/artifact/EQVvremS6huZYvNAY1BswG (shared with the team as email-invited Editors)

**New machine or first install: see [SETUP.md](SETUP.md)** (install script, files to copy, every API key and where to get it).

Data sources -> normalized tables (sources.py) -> KPIs, recommendations, scorecard (kpis.py)
-> KPI history (dashboard/history.json) -> page (dashboard_template.html -> dashboard/Dagacci_KPI_Dashboard.html)
-> republished to the same link. Owners, status, the change log, reports and decisions live in the
page's shared database, not in these files, so a rebuild never overwrites them.

## Data sources and how they update

| Dashboard data | Source | How it gets here | Status |
|---|---|---|---|
| Search queries, repeat purchase, market basket | Brand Analytics via SP-API | `build_dashboard.py --source spapi` | Code ready; needs SP-API keys |
| Daily sales, stock, traffic and Buy Box (Sales & stock tab) | SP-API -> amazon-agents warehouse | `amz-agents ingest` in refresh.sh | Code ready; needs SP-API keys |
| Weekly sales, new-to-brand, repeat rate (headline + scorecard) | Customer Trends CSV | Manual drop in `seller_central/` | No API: weekly CSV |
| Fit, returns, listing health | Voice of the Customer Listing Summary CSV | Manual drop in `seller_central/` | No API: monthly CSV is enough |
| Ad spend, ACoS, TACoS | Quartile ASIN Analysis export | `amz-agents quartile FILE` | Manual, unless Amazon Ads API access is added |
| dagacci.com units | Shopify | `amz-agents ingest --only shopify` in refresh.sh | Credentials set |

## Cadence (once SP-API keys are in)

- **Tuesday 07:00**: launchd runs `refresh.sh`. It pulls SP-API data, rebuilds with the week that ended the previous Saturday, republishes the page, and writes the weekly report (`report_prompt.md`). In the first week of a month it also writes the monthly report. If the Mac is asleep at 07:00, the job runs when it wakes.
- **Weekly, before Tuesday**: drop the Customer Trends CSV into `seller_central/`.
- **Monthly**: drop a fresh Listing Summary CSV and the Quartile export.
- **Anytime**: an Editor can press "Write report" on the Reports tab when the automatic run didn't happen.
- Logs: `dashboard/logs/refresh-YYYY-MM-DD.log`.

## Turning on automation

1. Put `SP_API_CLIENT_ID`, `SP_API_CLIENT_SECRET` and `SP_API_REFRESH_TOKEN` in `~/Documents/amazon-agents/.env`. Never paste them into chat or commit them.
2. Validate the first live pull against the CSVs you already have:
   `python3 -I build_dashboard.py --source spapi --week-end 2026-10-10 --out-dir /tmp/check`
   Raw API responses are saved to `seller_central/api_raw/`. The JSON-to-table mappings in sources.py were written from Amazon's published schemas and haven't been run against live data yet.
3. Run `zsh refresh.sh` once by hand and check the log.
4. `zsh install.sh --schedule` (on one machine only)

## Manual refresh (works today)

1. Drop new Seller Central exports into `~/Documents/dagacci/seller_central/` (keep Amazon's file names).
2. `python3 -I ~/Documents/amazon-agents/dashboard/build_dashboard.py`
3. Republish `dashboard/Dagacci_KPI_Dashboard.html` to the link above (ask Claude Code, or run step 3 of refresh.sh).

## What to edit

- KPI targets: the dashboard's **Review** tab (saved for everyone, overrides) or `targets.json` (the default)
- Recommendation rules and wording: `insights()` / `ops_insights()` in `kpis.py`
- Dollar impact estimates and their assumptions: `impact()` in `kpis.py`
- Owning team per recommendation: `KPI_IDS` in `kpis.py`. The id also keeps tracker rows attached week to week, so don't rename ids.
- Automatic report instructions: `report_prompt.md`
- Layout: `dashboard_template.html` (never the built HTML, which is overwritten on every build)

## Shared database (on the artifact)

- `actions/<id>`: the current team, status, due date, note and done note for each recommendation or team to-do (`todo-…`).
- `log/<auto>`: one row per change with the full state after it. Reports replay this log.
- `reports/<week-YYYY-MM-DD | month-YYYY-MM>`: summary bullets and, for weekly reports, the team-activity window.
- `reports/<id>/decisions/<auto>`: decisions recorded during the meeting.
- `review/<part>`: the Review tab's verdict on each part of the page (`keep`, `change` or `hide`), with a comment; change requests carry `status` (`requested` or `done`) and `resolution`.
- `settings/targets`: scorecard targets set on the page.

## Applying the review

The CEO marks each part of the page on the **Review** tab:
- **Not useful** hides the part for everyone immediately. Clicking it again restores the part.
- **Change it** queues a request.

To apply the requests, open Claude Code in this repo and ask it to **"apply the dashboard review"**. It should:
1. Read `review` (ArtifactData on the live page) for `verdict: change`, `status: requested`.
2. Make each change in `dashboard_template.html` or `kpis.py`. Parts marked "Not useful" can also be removed for good here.
3. Rebuild and republish to the same link, and commit the change.
4. Set each request's `status` to `done`, with a one-line `resolution` saying what changed. The Review tab then shows it as Done.

Parts are identified by their tab and heading. If a card's heading is renamed, its old review entry no longer applies to it.

