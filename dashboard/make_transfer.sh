#!/bin/zsh
# Copies everything a second computer needs that isn't on GitHub into one folder.
#   zsh dashboard/make_transfer.sh [folder]     # default: ~/Desktop/dagacci-transfer
# Safe to re-run: it updates the folder. It holds customer and business data but no keys:
# .env is left out on purpose, so move that separately (see SETUP.md).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd -P)"
REPO="$(dirname "$HERE")"
DATA="${DAGACCI_DATA:-$HOME/Documents/dagacci}"
T="${1:-$HOME/Desktop/dagacci-transfer}"

mkdir -p "$T/amazon-agents/data"
chmod 700 "$T"
rsync -a --exclude .DS_Store --exclude '.env' --exclude '.env.*' "$DATA/" "$T/dagacci/"

[[ -f "$REPO/data/warehouse.db" ]] && cp "$REPO/data/warehouse.db" "$T/amazon-agents/data/"
for f in "$REPO"/*.local.toml(N); do cp "$f" "$T/amazon-agents/"; done
[[ -d "$REPO/reports" ]] && rsync -a "$REPO/reports/" "$T/amazon-agents/reports/"
[[ -d "$HOME/webvolve/data" ]] && mkdir -p "$T/webvolve" && rsync -a "$HOME/webvolve/data/" "$T/webvolve/data/"
memory="$HOME/.claude/projects/-Users-$(whoami)/memory"
[[ -d "$memory" ]] && rsync -a "$memory/" "$T/claude-memory/"

if found="$(find "$T" -name '.env' -print -quit)" && [[ -n "$found" ]]; then
  print "Stopped: a keys file ended up in the folder: $found"
  exit 1
fi
du -sh "$T"
print "No keys in this folder. Move $REPO/.env separately."
