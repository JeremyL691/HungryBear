#!/bin/bash
# Collect campuses marked `runner: local` in campuses.yaml (sites that block GitHub Actions' cloud IPs)
# and push them to the data branch. Run by launchd on the Mac (scripts/com.hungrybear.collect.plist).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
WT="$REPO/data"   # git worktree of the data branch (ignored by the main checkout)
DAYS="${DAYS:-7}"
cd "$REPO"

git fetch -q origin data
if ! git -C "$WT" rev-parse --git-dir >/dev/null 2>&1; then
  rm -rf "$WT"
  git worktree prune
  git worktree add -q --detach "$WT" origin/data
fi

# GitHub Actions pushes to the same branch; start from its latest commit and retry on a race.
for attempt in 1 2 3; do
  git -C "$WT" reset -q --hard origin/data
  "$PY" -m hungrybear.collector --campus all --runner local --days "$DAYS" --out "$WT"
  git -C "$WT" add -A
  if git -C "$WT" diff --cached --quiet; then
    echo "no data changes"; exit 0
  fi
  git -C "$WT" commit -q -m "data(local): $(date -u +%Y-%m-%dT%H:%MZ)"
  if git -C "$WT" push -q origin HEAD:data; then
    echo "pushed"; exit 0
  fi
  echo "push rejected (attempt $attempt), retrying"
  git fetch -q origin data
done
exit 1
