#!/bin/bash
# Entry point for the launchd bot service (scripts/com.hungrybear.bot.plist).
# Picks up the latest code from main on every (re)start; token comes from ~/.hungrybear/bot.env.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
echo "== bot start $(date '+%Y-%m-%d %H:%M:%S')"
old_deps="$(git rev-parse HEAD:pyproject.toml)"
git fetch -q origin main && git reset -q --hard origin/main || echo "git update failed; running current code"
[ "$old_deps" = "$(git rev-parse HEAD:pyproject.toml)" ] || .venv/bin/pip install -q -e ".[bot]"
set -a; source "$HOME/.hungrybear/bot.env"; set +a
exec .venv/bin/python -m hungrybear.bot
