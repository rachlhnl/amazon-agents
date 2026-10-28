#!/bin/zsh
# Weekly unattended refresh of the Dagacci KPI dashboard (run by launchd, see
# com.dagacci.kpi-refresh.plist). Stops at the first failure so a half-built
# dashboard is never published.
#   1. SP-API -> amazon-agents warehouse (section 5: orders, stock, traffic)
#   2. SP-API Brand Analytics + CSV drops -> Dagacci_KPI_Dashboard.html
#   3. Headless Claude Code republishes the page to the same artifact link
#   4. ...and writes the weekly report (report_prompt.md) into the page's shared database
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"

ROOT="$HOME/Documents/dagacci"
AGENTS="$HOME/Documents/amazon-agents"
ARTIFACT="https://claude.ai/artifact/EQVvremS6huZYvNAY1BswG"
mkdir -p "$ROOT/pipeline/logs"
exec >>"$ROOT/pipeline/logs/refresh-$(date +%F).log" 2>&1
echo "== $(date) =="

cd "$AGENTS"
.venv/bin/amz-agents ingest --only products,metrics,inventory,asin_traffic,shopify

python3 -I "$ROOT/pipeline/build_dashboard.py" --source spapi

cd "$ROOT"
claude -p "Republish $ROOT/dashboard/Dagacci_KPI_Dashboard.html to the existing artifact $ARTIFACT \
(read it first, then publish the local file over it; keep its icon). Reply with the publish result only." \
  --allowedTools "Artifact"

# 4. The weekly (and, at the start of a month, monthly) report from the new data and the team's actions
claude -p "$(cat "$ROOT/pipeline/report_prompt.md")" --allowedTools "ArtifactData,Read"
echo "== done $(date) =="
