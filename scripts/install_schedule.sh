#!/bin/bash
# Install (or reinstall) the daily Trading 212 sync as a launchd agent.
#
#   bash scripts/install_schedule.sh          # default: 18:30 daily
#   bash scripts/install_schedule.sh 7 45     # or pass HOUR MINUTE
#
# Uninstall with:
#   launchctl bootout gui/$(id -u)/com.ivan.t212sync
#   rm ~/Library/LaunchAgents/com.ivan.t212sync.plist
set -euo pipefail

HOUR="${1:-18}"
MINUTE="${2:-30}"
LABEL="com.ivan.t212sync"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

chmod +x "$PROJECT_DIR/scripts/run_sync.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"

cat >"$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$PROJECT_DIR/scripts/run_sync.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>$HOUR</integer>
        <key>Minute</key>
        <integer>$MINUTE</integer>
    </dict>
    <!-- Catch up if the Mac was asleep at the scheduled time. -->
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/logs/launchd.err.log</string>
</dict>
</plist>
PLIST_EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

printf 'Installed %s — runs daily at %02d:%02d\n' "$LABEL" "$HOUR" "$MINUTE"
printf 'Run it now with: launchctl kickstart gui/%s/%s\n' "$(id -u)" "$LABEL"
printf 'Logs: %s/logs/t212-sync.log\n' "$PROJECT_DIR"
