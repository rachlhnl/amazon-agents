#!/bin/bash
# Run scripts/daily.sh every morning at 7:00 (or at wake, if the Mac was asleep).
# Remove it with: scripts/install-schedule.sh --remove
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
label="com.dagacci.amz-agents.daily"
plist="$HOME/Library/LaunchAgents/$label.plist"

if [[ "${1:-}" == "--remove" ]]; then
  launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
  rm -f "$plist"
  echo "Removed the daily agent run."
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat >"$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>$repo/scripts/daily.sh</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
  <key>EnvironmentVariables</key><dict><key>AGENT_MODE</key><string>approve</string></dict>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/amz-agents-daily.log</string>
</dict>
</plist>
PLIST
launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "Agents will run daily at 7:00. Log: $repo/reports/daily.log"
case "$repo" in
  "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*)
    echo
    echo "Note: macOS doesn't let scheduled jobs read Documents, Desktop or Downloads."
    echo "Either move this folder (e.g. to ~/code) and run this script again, or open"
    echo "System Settings > Privacy & Security > Full Disk Access and add /bin/bash."
    echo "If the job is blocked, the reason is in ~/Library/Logs/amz-agents-daily.log."
    ;;
esac
