# hungrybear/report.py
"""Turn status.json into alerts. Runs in the collect workflow after the collector.

For each campus:
- consecutive_failures >= threshold and no open issue -> open a GitHub issue (labels: scraper-broken,
  campus:<id>) and send a Telegram alert to the admin
- consecutive_failures >= threshold and issue open    -> refresh the issue body (no new noise)
- healthy again and issue open                        -> comment + close, Telegram "recovered"

  python -m hungrybear.report --data data --snapshots snapshots [--dry-run]

Env: GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_SERVER_URL, GITHUB_RUN_ID (all set by Actions);
     TELEGRAM_BOT_TOKEN + ADMIN_CHAT_ID (optional - skip Telegram if missing).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional

import httpx

LABEL = "scraper-broken"
API = "https://api.github.com"


class GitHub:
    def __init__(self, repo: str, token: str, dry_run: bool = False) -> None:
        self.repo, self.dry = repo, dry_run
        self.client = httpx.Client(
            base_url=API,
            timeout=30,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )

    def open_issues(self) -> Dict[str, dict]:
        """{campus id: issue} for open scraper-broken issues."""
        resp = self.client.get(f"/repos/{self.repo}/issues", params={"labels": LABEL, "state": "open", "per_page": 100})
        resp.raise_for_status()
        out = {}
        for issue in resp.json():
            for label in issue.get("labels", []):
                name = label["name"] if isinstance(label, dict) else label
                if name.startswith("campus:"):
                    out[name.split(":", 1)[1]] = issue
        return out

    def _write(self, method: str, path: str, payload: dict) -> Optional[dict]:
        if self.dry:
            print(f"[dry-run] {method} {path} {json.dumps(payload)[:300]}")
            return None
        resp = self.client.request(method, f"/repos/{self.repo}{path}", json=payload)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    def create_issue(self, title: str, body: str, labels: List[str]) -> Optional[dict]:
        return self._write("POST", "/issues", {"title": title, "body": body, "labels": labels})

    def update_issue(self, number: int, **fields) -> None:
        self._write("PATCH", f"/issues/{number}", fields)

    def comment(self, number: int, body: str) -> None:
        self._write("POST", f"/issues/{number}/comments", {"body": body})


def telegram(text: str, dry_run: bool) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("ADMIN_CHAT_ID")
    if not token or not chat:
        return
    if dry_run:
        print(f"[dry-run] telegram: {text}")
        return
    try:
        httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": text}, timeout=15)
    except httpx.HTTPError as e:  # alerts must never fail the pipeline
        print(f"telegram alert failed: {e}", file=sys.stderr)


def issue_body(campus: str, entry: dict, run_url: str) -> str:
    rows = "\n".join(
        f"| {day} | {info['status']} | {(info.get('reason') or '').replace('|', '/')} |"
        for day, info in sorted(entry.get("days", {}).items())
    )
    return f"""The collector has failed for **{campus}** {entry["consecutive_failures"]} runs in a row.
Users are being served the last good data (last success: {entry.get("last_success") or "never"}).

| date | status | reason |
|---|---|---|
{rows}

**Latest run:** {run_url} (artifact `snapshots` has the raw responses)

Reproduce locally:
```bash
python -m hungrybear.collector --campus {campus} --days 2 --out /tmp/out --record /tmp/snap -v
```

_This issue closes automatically when the campus recovers._
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--snapshots", type=Path, default=None, help="drop snapshot dirs of healthy campuses")
    ap.add_argument("--threshold", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    status = json.loads((args.data / "v1" / "status.json").read_text())["campuses"]
    broken = sorted(c for c, e in status.items() if e["consecutive_failures"] >= args.threshold)

    # Keep only snapshots that matter, so the uploaded artifact stays small.
    if args.snapshots and args.snapshots.exists():
        for d in args.snapshots.iterdir():
            if d.is_dir() and status.get(d.name, {}).get("status") != "broken":
                shutil.rmtree(d)

    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"broken={','.join(broken)}\n")

    repo, token = os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_TOKEN")
    if not repo or not token:
        print(f"GITHUB_REPOSITORY/GITHUB_TOKEN not set; broken campuses: {broken or 'none'}")
        return 0

    gh = GitHub(repo, token, dry_run=args.dry_run)
    run_url = (
        f"{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{os.getenv('GITHUB_RUN_ID', '')}"
    )
    issues = gh.open_issues()

    for campus, entry in sorted(status.items()):
        issue = issues.get(campus)
        if campus in broken:
            body = issue_body(campus, entry, run_url)
            if issue:
                gh.update_issue(issue["number"], body=body)
                continue
            first = next((i["reason"] for i in entry.get("days", {}).values() if i.get("reason")), "unknown")
            gh.create_issue(f"[{campus}] menu scraper broken", body, [LABEL, f"campus:{campus}"])
            telegram(f"🔴 HungryBear: {campus} scraper broken\n{first[:300]}\n{run_url}", args.dry_run)
        elif issue and entry["status"] == "ok":
            gh.comment(issue["number"], f"Recovered in {run_url} — closing.")
            gh.update_issue(issue["number"], state="closed", state_reason="completed")
            telegram(f"🟢 HungryBear: {campus} recovered", args.dry_run)

    print(f"broken campuses: {broken or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
