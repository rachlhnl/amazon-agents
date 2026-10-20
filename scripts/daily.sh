#!/bin/bash
# Daily agent run: refresh data, run the agents, rebuild the briefs and tracker.
#
# Runs on this Mac (not in GitHub Actions) because approvals live in the local
# warehouse the tracker reads. The market tracker itself refreshes in GitHub
# Actions; this pulls its latest snapshot first.
#
# Install the daily schedule:   scripts/install-schedule.sh
# Run it now:                   scripts/daily.sh
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p reports
exec >>reports/daily.log 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M') daily run"

# Suggestions queue for approval, so each one has a decision and an owner. .env
# can't override this here; set AGENT_MODE in the schedule to change it.
export AGENT_MODE="${AGENT_MODE:-approve}"

GIT_TERMINAL_PROMPT=0 git pull --ff-only --quiet || echo "git pull skipped (offline, local changes or no push yet)"
.venv/bin/amz-agents ingest || echo "ingest failed; running on the data already loaded"
.venv/bin/amz-agents run
echo "=== done"
