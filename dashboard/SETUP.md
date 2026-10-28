# Setting up the Dagacci dashboard on a new machine

The dashboard has three parts, and only one of them lives on a computer:

| Part | Where it lives | Moves with the computer? |
|---|---|---|
| The page, action tracker, reports, review checklist and targets | claude.ai (artifact + its shared database) | No: it's in the cloud, untouched by a move |
| Code (pipeline, agents) | GitHub: rachlhnl/amazon-agents | Cloned fresh |
| Credentials, Seller Central exports, KPI history, agents' warehouse | Your Mac only (never in git) | Copied by hand, securely |

Teammates need no setup. They open the dashboard link while signed in to claude.ai.

## Part 1: on the old machine

1. **Make sure all code is on GitHub:** `git -C ~/Documents/amazon-agents status` should show nothing to commit and nothing to push.
2. **Stop the weekly job** if it's installed, so two machines don't both publish and write reports:
   `launchctl unload ~/Library/LaunchAgents/com.dagacci.kpi-refresh.plist`
3. **Copy these files to the new machine**, using AirDrop, an encrypted drive or a password manager's secure notes. Never email, Slack or git:

   | File | Why |
   |---|---|
   | `~/Documents/amazon-agents/.env` | All API keys |
   | `~/Documents/amazon-agents/plan.local.toml`, `goals.local.toml` | Real figures for the agents' CEO plan (gitignored) |
   | `~/Documents/amazon-agents/data/warehouse.db` | Sales, stock and traffic history for the agents and the Sales & stock tab |
   | `~/Documents/dagacci/dashboard/history.json` | Weekly KPI and recommendation history. Reports compare against it; it can't be rebuilt for past weeks |
   | `~/Documents/dagacci/seller_central/` | Customer Trends, Search Query, Repeat Purchase, Market Basket and Listing Summary CSVs |
   | `~/Documents/dagacci/data/` | Quartile export, other one-off exports |

## Part 2: on the new machine (macOS)

1. **Install the tools** (in Terminal):
   ```
   xcode-select --install                              # git
   curl -LsSf https://astral.sh/uv/install.sh | sh     # Python manager; fetches Python 3.12 itself
   ```
   Install Claude Code (https://claude.com/claude-code), run `claude`, and sign in with **the claude.ai account that owns the dashboard** (or one invited to it as an Editor). That account republishes the page and writes the reports.
2. **Get the code:**
   ```
   git clone https://github.com/rachlhnl/amazon-agents.git ~/Documents/amazon-agents
   ```
3. **Put the copied files back** in the same places as in Part 1, step 3.
4. **Run the installer** and work through anything it marks with →:
   ```
   zsh ~/Documents/amazon-agents/dashboard/install.sh
   ```
   It creates the data folders, the Python environment and `.env` (if missing), and checks keys, Claude Code and data.
5. **Test a build:** `python3 -I ~/Documents/amazon-agents/dashboard/build_dashboard.py`, then open `~/Documents/dagacci/dashboard/Dagacci_KPI_Dashboard.html`.
6. **Test the full refresh once.** This republishes the live page and writes the week's report if it's missing:
   `zsh ~/Documents/amazon-agents/dashboard/refresh.sh`, then read `dashboard/logs/refresh-<today>.log`.
7. **Turn on the Tuesday 07:00 schedule** on this machine only:
   `zsh ~/Documents/amazon-agents/dashboard/install.sh --schedule`

Other locations: set `DAGACCI_DATA=/path/to/data` for the data folder and `DASHBOARD_URL=<artifact link>` for a different dashboard, in your shell profile and for launchd.
Not on a Mac: the scripts need zsh. On Linux, replace step 7 with the cron line `0 7 * * 2 zsh ~/Documents/amazon-agents/dashboard/refresh.sh`. On Windows, use WSL.

## Part 3: the APIs and where to get them

Put every value in `~/Documents/amazon-agents/.env`. Don't paste them into chat, documents or git.

### Needed for the dashboard's automatic updates

| `.env` keys | What | Where to get it |
|---|---|---|
| `SP_API_CLIENT_ID`, `SP_API_CLIENT_SECRET` | Amazon Selling Partner API app (Login with Amazon credentials) | Seller Central → Apps and Services → **Develop Apps**. Register as a **private developer** the first time. **Add new app client**, API type *SP API*, roles: Brand Analytics, Selling Partner Insights, Inventory and Order Tracking, Product Listing, Pricing. Then **View** under LWA credentials. Brand Analytics needs the account to be the brand owner in Brand Registry |
| `SP_API_REFRESH_TOKEN` | Lets the app read your account without a login | Same page: the arrow next to **Edit App** → **Authorize** → **Authorize app**. It's shown once, so copy it immediately |
| `SP_API_SELLER_ID` | Your merchant token | Seller Central → Settings → **Account Info** → Your Merchant Token |
| `SP_API_REGION`, `SP_API_MARKETPLACE_ID` | Already set: `na`, `ATVPDKIKX0DER` (US) | No action needed |
| *(not a key)* | Claude account | claude.ai, signed in to Claude Code (Part 2, step 1) |

### Optional: more data on the dashboard

| `.env` keys | What it adds | Where to get it |
|---|---|---|
| `ADS_API_CLIENT_ID`, `ADS_API_CLIENT_SECRET`, `ADS_API_REFRESH_TOKEN`, `ADS_API_PROFILE_ID` | Ad spend, ACoS and TACoS without the manual Quartile export | Apply at advertising.amazon.com → **API** (Amazon Ads API access). It can reuse the LWA app above. If Quartile runs the account, ask them for API access or a weekly export instead |
| `SHOPIFY_SHOP`, `SHOPIFY_ACCESS_TOKEN` (or `SHOPIFY_CLIENT_ID`/`SECRET`) | dagacci.com units | **Already set.** To regenerate: Shopify admin → Settings → Apps and sales channels → **Develop apps** → app → Admin API access token, read scopes for products, orders and inventory |

### Used by the agents, not by the dashboard

| `.env` keys | What | Where to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | Agents' drafted copy and briefs | console.anthropic.com → **API Keys** |
| `META_AD_ACCOUNT_ID`, `META_ACCESS_TOKEN` | Meta ads results (already set) | Meta Business Settings → **System users** → Generate token with `ads_read` (and `ads_management` to act). Ad account ID under Accounts → Ad accounts |
| `PAGESPEED_API_KEY` | Store page speed (already set) | Google Cloud console → enable **PageSpeed Insights API** → Credentials → **API key** |
| `GA4_PROPERTY_ID`, `GA4_CREDENTIALS` | Store funnel | Google Analytics → Admin → Property details (ID). Service account JSON from Google Cloud, added as a Viewer on the property |
| `SMTP_*`, `BRIEF_*` | Emailing the agents' brief | Your mail provider: an app password on a company mailbox |

Data with no API (manual, see README "Data sources"): **Customer Trends** CSV (weekly) and **Listing Summary** CSV (monthly), both from Seller Central → Brand Analytics / Voice of the Customer, dropped into `~/Documents/dagacci/seller_central/`.
