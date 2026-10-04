#!/bin/bash
# Install (or reinstall) the always-on Telegram bot as a launchd agent on this Mac.
# Shares the ~/.hungrybear/collector checkout with the local collector (launchd can't read ~/Desktop).
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$HOME/.hungrybear/collector"
DEST="$HOME/Library/LaunchAgents/com.hungrybear.bot.plist"

[ -d "$RUN/.git" ] || "$SRC/scripts/install_local_collector.sh"
git -C "$RUN" fetch -q origin main && git -C "$RUN" reset -q --hard origin/main
"$RUN/.venv/bin/pip" install -q -e "$RUN[bot]"

# Bot token: copied from the project's .env into a private file outside the repo.
token="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$SRC/.env" | cut -d= -f2-)"
[ -n "$token" ] || { echo "TELEGRAM_BOT_TOKEN missing from $SRC/.env"; exit 1; }
umask 077
printf 'TELEGRAM_BOT_TOKEN=%s\n' "$token" > "$HOME/.hungrybear/bot.env"

sed -e "s#__REPO__#$RUN#g" -e "s#__HOME__#$HOME#g" "$SRC/scripts/com.hungrybear.bot.plist" > "$DEST"
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"
echo "installed $DEST (log: ~/Library/Logs/hungrybear-bot.log)"
