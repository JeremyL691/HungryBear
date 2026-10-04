#!/bin/bash
# Install (or reinstall) the launchd job that collects `runner: local` campuses on this Mac.
#
# macOS doesn't let launchd jobs read ~/Desktop, ~/Documents, etc., so the job runs from its own
# checkout in ~/.hungrybear/collector (pulled from origin/main before every run).
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$HOME/.hungrybear/collector"
DEST="$HOME/Library/LaunchAgents/com.hungrybear.collect.plist"
URL="$(git -C "$SRC" remote get-url origin)"
PYTHON="${PYTHON:-python3.11}"

mkdir -p "$(dirname "$RUN")" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
if [ -d "$RUN/.git" ]; then
  git -C "$RUN" fetch -q origin main && git -C "$RUN" reset -q --hard origin/main
else
  git clone -q --branch main "$URL" "$RUN"
fi
git -C "$RUN" config user.name "$(git -C "$SRC" config user.name)"
git -C "$RUN" config user.email "$(git -C "$SRC" config user.email)"
[ -x "$RUN/.venv/bin/python" ] || "$PYTHON" -m venv "$RUN/.venv"
"$RUN/.venv/bin/pip" install -q --upgrade pip
"$RUN/.venv/bin/pip" install -q -e "$RUN"

sed -e "s#__REPO__#$RUN#g" -e "s#__HOME__#$HOME#g" "$SRC/scripts/com.hungrybear.collect.plist" > "$DEST"
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"
echo "installed $DEST -> $RUN (log: ~/Library/Logs/hungrybear-collect.log)"
