#!/bin/bash
# Install (or reinstall) the launchd job that runs scripts/collect_local.sh on this Mac.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/Library/LaunchAgents/com.hungrybear.collect.plist"
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" "$REPO/scripts/com.hungrybear.collect.plist" > "$DEST"
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"
echo "installed $DEST (log: ~/Library/Logs/hungrybear-collect.log)"
