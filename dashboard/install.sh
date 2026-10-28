#!/bin/zsh
# Set up the Dagacci dashboard on this Mac, from a fresh clone of amazon-agents.
#   zsh dashboard/install.sh              # folders, Python env, .env, checks
#   zsh dashboard/install.sh --schedule   # also install the Tuesday 07:00 refresh (one machine only)
# Safe to re-run: it never overwrites .env, data or an existing schedule without saying so.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd -P)"
REPO="$(dirname "$HERE")"
DATA="${DAGACCI_DATA:-$HOME/Documents/dagacci}"
ok() { print -P "%F{green}✓%f $1"; }
todo() { print -P "%F{yellow}→%f $1"; }

# 1. Business data lives outside the repo (never committed).
mkdir -p "$DATA/seller_central" "$DATA/dashboard" "$DATA/data" "$HERE/logs"
ok "Data folder: $DATA (seller_central/, dashboard/, data/)"
[[ -e "$DATA/pipeline" ]] || { ln -s "$HERE" "$DATA/pipeline"; ok "Linked $DATA/pipeline -> $HERE"; }

# 2. Python: the agents need 3.11+ (the dashboard build itself runs on the system python3).
#    uv (https://docs.astral.sh/uv/) is preferred: it also fetches Python 3.12 if the Mac lacks it.
if command -v uv >/dev/null; then
  [[ -d "$REPO/.venv" ]] || uv venv -q --python 3.12 "$REPO/.venv"
  uv pip install -q --python "$REPO/.venv/bin/python" -e "${REPO}[dev,xlsx]"
  ok "Python environment: $REPO/.venv ($("$REPO/.venv/bin/python" --version), via uv)"
elif PY="$(command -v python3.13 || command -v python3.12 || command -v python3.11)"; then
  [[ -d "$REPO/.venv" ]] || "$PY" -m venv "$REPO/.venv"
  "$REPO/.venv/bin/python" -m pip install -q -e "${REPO}[dev,xlsx]"
  ok "Python environment: $REPO/.venv ($("$REPO/.venv/bin/python" --version))"
else
  todo "Install uv (curl -LsSf https://astral.sh/uv/install.sh | sh) or Python 3.11+, then re-run this script."
fi

# 3. Credentials: copied from .env.example once; fill in by hand (see SETUP.md).
if [[ ! -f "$REPO/.env" ]]; then
  cp "$REPO/.env.example" "$REPO/.env"; chmod 600 "$REPO/.env"
  todo "Created $REPO/.env from .env.example: fill in the keys (SETUP.md, step 3)."
fi
missing=()
for k in SP_API_CLIENT_ID SP_API_CLIENT_SECRET SP_API_REFRESH_TOKEN SP_API_SELLER_ID; do
  grep -qE "^$k=[^[:space:]#]+" "$REPO/.env" || missing+=$k
done
(( ${#missing} )) && todo "Still empty in .env: ${missing[*]}" || ok "SP-API credentials present in .env"

# 4. Claude Code: republishes the page and writes reports, signed in as the dashboard's owner or an Editor.
if command -v claude >/dev/null; then ok "Claude Code installed ($(claude --version 2>/dev/null | head -1))"
else todo "Install Claude Code (https://claude.com/claude-code) and sign in with the account that owns the dashboard."; fi

# 5. Data that isn't in git.
[[ -f "$DATA/dashboard/history.json" ]] || todo "Copy dashboard/history.json from the old machine into $DATA/dashboard/ (KPI history for reports)."
ls "$DATA"/seller_central/*.csv >/dev/null 2>&1 || todo "Copy the Seller Central CSV exports into $DATA/seller_central/ (or download fresh ones)."
[[ -f "$REPO/data/warehouse.db" ]] || todo "No agents warehouse yet: copy $REPO/data/warehouse.db from the old machine, or run 'amz-agents ingest' once keys are in."

# 6. Optional: the weekly schedule. Only one machine should run it, or reports and publishes double up.
if [[ "${1:-}" == "--schedule" ]]; then
  dest="$HOME/Library/LaunchAgents/com.dagacci.kpi-refresh.plist"
  [[ -f "$dest" ]] && launchctl unload "$dest" 2>/dev/null || true
  sed "s#__HERE__#$HERE#g" "$HERE/com.dagacci.kpi-refresh.plist" > "$dest"
  launchctl load "$dest"
  ok "Scheduled: Tuesdays 07:00 ($dest). Logs: $HERE/logs/"
else
  todo "When this is the machine that should run the weekly refresh: zsh $HERE/install.sh --schedule"
fi
