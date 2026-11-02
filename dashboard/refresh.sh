#!/bin/zsh
# Weekly unattended refresh of the Dagacci KPI dashboard (run by launchd, see
# com.dagacci.kpi-refresh.plist). Stops at the first failure so a half-built
# dashboard is never published.
#   1. Seller Central exports waiting in ~/Downloads are filed (file_exports.sh)
#   2. With SP-API keys: SP-API -> amazon-agents warehouse and Brand Analytics.
#      Without them: the dashboard is built from the CSVs in seller_central/.
#   3. The agents run, so this week's briefs and the promotions plan are current
#   4. Headless Claude Code republishes the page to the same artifact link
#   5. ...writes the weekly report (report_prompt.md) into the page's shared database
#   6. ...and, when the hub folder exists, rebuilds and republishes the Dagacci HQ page
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
HERE="$(cd "$(dirname "$0")" && pwd -P)"              # amazon-agents/dashboard
AGENTS="$(dirname "$HERE")"                           # the amazon-agents repo
ROOT="${DAGACCI_DATA:-$HOME/Documents/dagacci}"       # exports, built page, KPI history
ARTIFACT="${DASHBOARD_URL:-https://claude.ai/artifact/EQVvremS6huZYvNAY1BswG}"
mkdir -p "$HERE/logs"
exec >>"$HERE/logs/refresh-$(date +%F).log" 2>&1
echo "== $(date) =="

zsh "$HERE/file_exports.sh" || echo "filing exports failed; building from what's already filed"

cd "$AGENTS"
# An SP-API refresh token in .env means the live pull works; otherwise build from the CSV drops.
if grep -Eq '^SP_API_REFRESH_TOKEN=[^#[:space:]]+' .env 2>/dev/null; then
  .venv/bin/amz-agents ingest --only products,metrics,inventory,asin_traffic,shopify
  python3 -I "$HERE/build_dashboard.py" --source spapi
else
  echo "No SP-API keys: building from the CSVs in $ROOT/seller_central"
  .venv/bin/amz-agents ingest --only shopify || echo "shopify ingest skipped"
  # Orders, inventory and listings downloaded by hand: load the newest set into the warehouse.
  latest=("$ROOT"/data/seller-*(N/om[1]))
  if (( ${#latest} )); then
    .venv/bin/amz-agents seller "$latest[1]"/*(N.) || echo "seller report import failed"
  fi
  python3 -I "$HERE/build_dashboard.py" --source csv
fi
AGENT_MODE=approve .venv/bin/amz-agents run || echo "agent run failed; the dashboard still publishes"

cd "$ROOT"
claude -p "Republish $ROOT/dashboard/Dagacci_KPI_Dashboard.html to the existing artifact $ARTIFACT \
(read it first, then publish the local file over it; keep its icon). Reply with the publish result only." \
  --allowedTools "Artifact"
# The weekly (and, at the start of a month, monthly) report from the new data and the team's actions
claude -p "$(sed "s#~/Documents/dagacci#$ROOT#g; s#https://claude.ai/artifact/EQVvremS6huZYvNAY1BswG#$ARTIFACT#g" "$HERE/report_prompt.md")" \
  --allowedTools "ArtifactData,Read"
# Company pages that live outside this repo (they hold names and business data)
if [[ -f "$ROOT/hub/update.sh" ]]; then
  zsh "$ROOT/hub/update.sh" || echo "hub update failed"
fi
echo "== done $(date) =="
