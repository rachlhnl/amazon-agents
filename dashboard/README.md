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

## Schedules

Two launchd jobs, installed by `zsh install.sh --schedule` on **one machine only** (two
machines would both publish and both write reports):

| Job | When | What it does | File |
|---|---|---|---|
| Weekly refresh | Tuesdays 07:00 (or at wake if the Mac was asleep) | Runs `refresh.sh`, below | `com.dagacci.kpi-refresh.plist` |
| Export filing | Whenever `~/Downloads` changes | Runs `file_exports.sh`: moves Seller Central and Quartile exports out of Downloads into the data folder (Brand Analytics CSVs and the Listing Summary to `seller_central/`, orders, inventory, listings and business reports to `data/seller-<date>/`, the Quartile export to `data/`) | `com.dagacci.file-exports.plist` |

What `refresh.sh` does, in order (it stops at the first failure, so a half-built page is never published):

1. Files any exports still in Downloads.
2. **With SP-API keys** in `.env`: pulls orders, stock, traffic and Brand Analytics, and builds the dashboard from them. **Without them**: builds from the CSVs in `seller_central/` and loads the newest `data/seller-<date>/` reports into the agents' warehouse.
3. Runs the agents (`amz-agents run`, approve mode), so the briefs and the promotions plan are current.
4. Republishes the dashboard to the same link with headless Claude Code.
5. Writes the weekly report (`report_prompt.md`), and the monthly one in the first week of a month.
6. If `~/Documents/dagacci/hub/update.sh` exists, runs it: it refreshes the company hub page outside this repo.

On the page itself, the To-do list adds each week's routine tasks every Monday (and the monthly ones in a month's first week), from `ROUTINE` and `MONTHLY` in `dashboard_template.html`.

**macOS permission (once per machine):** scheduled jobs can't read Documents or Downloads until you add `/bin/zsh` and `/bin/bash` under System Settings → Privacy & Security → Full Disk Access. The refresh log shows "Operation not permitted" when this is missing.

Until SP-API keys are in, a person downloads the weekly reports into Downloads (they're filed automatically):
- **Weekly:** Search Query Performance (Brand view, Comprehensive), Repeat Purchase Behavior (ASIN view), Market Basket Analysis, Customer Loyalty → Trends → Export; Reports → All Orders and FBA Inventory.
- **Monthly:** Voice of the Customer → Download data (Listing Summary); the Quartile ASIN Analysis export.
- **Anytime:** an Editor can press "Write report" on the Weekly reports tab when the automatic run didn't happen.

Logs: `dashboard/logs/refresh-YYYY-MM-DD.log`, `dashboard/logs/file-exports.log`. To stop both jobs:
`launchctl unload ~/Library/LaunchAgents/com.dagacci.kpi-refresh.plist ~/Library/LaunchAgents/com.dagacci.file-exports.plist`

## Turning on SP-API

1. Put `SP_API_CLIENT_ID`, `SP_API_CLIENT_SECRET` and `SP_API_REFRESH_TOKEN` in `~/Documents/amazon-agents/.env`. Never paste them into chat or commit them.
2. Validate the first live pull against the CSVs you already have:
   `python3 -I build_dashboard.py --source spapi --week-end 2026-10-10 --out-dir /tmp/check`
   Raw API responses are saved to `seller_central/api_raw/`. The JSON-to-table mappings in sources.py were written from Amazon's published schemas and haven't been run against live data yet.
3. Run `zsh refresh.sh` once by hand and check the log. From then on the Tuesday job uses the API automatically.

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

