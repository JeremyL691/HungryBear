# hungrybear/report.py
"""Turn status.json into alerts. Runs in the collect workflow after the collector.

For each campus:
- consecutive_failures >= threshold (or FreshCal freshness verdict OVERDUE) and no open issue
    -> open a GitHub issue (labels: scraper-broken, campus:<id>) and send a Telegram alert to the admin
- consecutive_failures >= threshold (or OVERDUE) and issue open
    -> refresh the issue body (no new noise)
- healthy again and issue open
    -> comment + close, Telegram "recovered"

  python -m hungrybear.report --data data --snapshots snapshots [--dry-run] [--now ISO8601]

Env: GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_SERVER_URL, GITHUB_RUN_ID (all set by Actions);
     TELEGRAM_BOT_TOKEN + ADMIN_CHAT_ID (optional - skip Telegram if missing).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from freshcal.adapters.holidays_provider import HolidaysCalendarProvider
from freshcal.core.errors import Issue
from freshcal.core.model import EvaluationResult, Origin, RawObservation, Status
from freshcal.core.timeutil import parse_instant, to_utc
from freshcal.core.verdict import evaluate

from .config import (
    DEFAULT_FRESHNESS_GRACE,
    DEFAULT_FRESHNESS_RELEASE_TIME,
    HOLIDAY_PROVIDER,
    PACIFIC,
    CampusConfig,
    load_campuses,
)

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


def _fallback_campus(campus: str) -> CampusConfig:
    return CampusConfig(id=campus, name=campus, short_name=campus, adapter="unknown")


def evaluate_freshness(
    campus: str,
    entry: dict,
    now: datetime,
    cfg: Optional[CampusConfig] = None,
    *,
    release_time: time = DEFAULT_FRESHNESS_RELEASE_TIME,
    grace: timedelta = DEFAULT_FRESHNESS_GRACE,
    provider: Optional[HolidaysCalendarProvider] = None,
) -> EvaluationResult:
    """Evaluate a campus's `last_success` timestamp with FreshCal's calendar-aware engine."""
    now_utc = to_utc(now)
    campus_cfg = cfg or _fallback_campus(campus)
    rule = campus_cfg.freshness_rule(release_time=release_time, grace=grace)
    raw_last = entry.get("last_success")

    if raw_last is None or raw_last == "":
        observed: Optional[datetime] = None
    elif isinstance(raw_last, datetime):
        observed = raw_last
    elif isinstance(raw_last, str):
        try:
            observed = datetime.fromisoformat(raw_last)
        except ValueError as err:
            issue = Issue("E502", f"invalid last_success timestamp {raw_last!r}: {err}", campus)
            return EvaluationResult(
                source_id=campus,
                origin=Origin.CONFIG,
                status=Status.QUERY_ERROR,
                evaluated_at=now_utc,
                schedule_timezone=PACIFIC.key,
                release=None,
                deadline=None,
                observation=None,
                next_expected_arrival=None,
                explanation=f"Query error: {issue.code} {issue.message}",
                error=issue,
            )
    else:
        issue = Issue("E502", f"invalid last_success value {raw_last!r} (expected ISO 8601 string)", campus)
        return EvaluationResult(
            source_id=campus,
            origin=Origin.CONFIG,
            status=Status.QUERY_ERROR,
            evaluated_at=now_utc,
            schedule_timezone=PACIFIC.key,
            release=None,
            deadline=None,
            observation=None,
            next_expected_arrival=None,
            explanation=f"Query error: {issue.code} {issue.message}",
            error=issue,
        )

    return evaluate(rule, RawObservation(observed), now_utc, provider or HOLIDAY_PROVIDER)


def is_stale(
    entry: dict,
    now: datetime,
    cfg: Optional[CampusConfig] = None,
    *,
    campus: str = "campus",
    release_time: time = DEFAULT_FRESHNESS_RELEASE_TIME,
    grace: timedelta = DEFAULT_FRESHNESS_GRACE,
    provider: Optional[HolidaysCalendarProvider] = None,
) -> bool:
    """True when FreshCal judges the campus OVERDUE on a normal operating day."""
    result = evaluate_freshness(
        campus,
        entry,
        now,
        cfg=cfg,
        release_time=release_time,
        grace=grace,
        provider=provider,
    )
    return result.status is Status.OVERDUE


def collect_calendar_warnings(
    results: Mapping[str, EvaluationResult],
) -> Dict[str, List[Issue]]:
    """Extract FreshCal calendar expiry warnings (W005) and expired-calendar errors (E408)."""
    out: Dict[str, List[Issue]] = {}
    for campus, result in sorted(results.items()):
        issues = [w for w in result.warnings if w.code == "W005"]
        if result.error is not None and result.error.code == "E408":
            issues.append(result.error)
        if issues:
            out[campus] = issues
    return out


def _alert_reason(entry: dict, freshness: Optional[EvaluationResult]) -> str:
    broken_day_reason = next(
        (
            info["reason"]
            for info in entry.get("days", {}).values()
            if info.get("status") == "broken" and info.get("reason")
        ),
        None,
    )
    if broken_day_reason:
        return broken_day_reason
    if freshness is not None and freshness.status is Status.OVERDUE and freshness.explanation:
        return freshness.explanation
    first = next((i["reason"] for i in entry.get("days", {}).values() if i.get("reason")), None)
    return first or f"no successful update since {entry.get('last_success')}"


def issue_body(
    campus: str,
    entry: dict,
    run_url: str,
    freshness: Optional[EvaluationResult] = None,
) -> str:
    rows = "\n".join(
        f"| {day} | {info['status']} | {(info.get('reason') or '').replace('|', '/')} |"
        for day, info in sorted(entry.get("days", {}).items())
    )
    extra_lines: List[str] = []
    if freshness is not None and freshness.status is Status.OVERDUE and freshness.explanation:
        extra_lines.append(f"**Freshness ({freshness.status.value}):** {freshness.explanation}")
    if freshness is not None:
        cal_issues = [w for w in freshness.warnings if w.code == "W005"]
        if freshness.error is not None and freshness.error.code == "E408":
            cal_issues.append(freshness.error)
        for issue in cal_issues:
            extra_lines.append(f"**Calendar warning ({issue.code}):** {issue.message}")
    extra_block = ("\n" + "\n".join(extra_lines) + "\n") if extra_lines else ""

    return f"""The collector has failed for **{campus}** {entry["consecutive_failures"]} runs in a row.
Users are being served the last good data (last success: {entry.get("last_success") or "never"}).
{extra_block}
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
    ap.add_argument("--now", default=None, help="ISO 8601 timestamp with UTC offset for evaluation (default: now)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    status_path = args.data / "v1" / "status.json"
    doc = json.loads(status_path.read_text())
    status = doc["campuses"]
    now = parse_instant(args.now, flag="--now") if args.now else datetime.now(UTC)
    campuses = load_campuses()

    freshness = {c: evaluate_freshness(c, e, now, cfg=campuses.get(c)) for c, e in status.items()}
    stale = {c for c, res in freshness.items() if res.status is Status.OVERDUE}
    broken = sorted(c for c, e in status.items() if e["consecutive_failures"] >= args.threshold or c in stale)

    cal_warnings = collect_calendar_warnings(freshness)
    for campus, issues in cal_warnings.items():
        for issue in issues:
            print(f"calendar warning [{campus}] {issue.code}: {issue.message}")
    for campus, res in sorted(freshness.items()):
        if res.status in (Status.CONFIG_ERROR, Status.QUERY_ERROR) and (res.error is None or res.error.code != "E408"):
            print(f"freshness diagnostic [{campus}]: {res.explanation}", file=sys.stderr)

    # Keep only snapshots that matter, so the uploaded artifact stays small.
    if args.snapshots and args.snapshots.exists():
        for d in args.snapshots.iterdir():
            if d.is_dir() and status.get(d.name, {}).get("status") != "broken":
                shutil.rmtree(d)

    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"broken={','.join(broken)}\n")
            if cal_warnings:
                f.write(f"calendar_warnings={','.join(sorted(cal_warnings))}\n")

    repo, token = os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_TOKEN")
    changed = False
    if not repo or not token:
        for campus, entry in sorted(status.items()):
            if campus in broken:
                if not entry.get("alerted"):
                    reason = _alert_reason(entry, freshness.get(campus))
                    telegram(f"🔴 HungryBear: {campus} scraper broken\n{reason[:300]}", args.dry_run)
                    entry["alerted"] = True
                    changed = True
            elif entry.get("alerted") and entry["status"] == "ok":
                telegram(f"🟢 HungryBear: {campus} recovered", args.dry_run)
                entry.pop("alerted", None)
                changed = True
        if changed and not args.dry_run:
            status_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        print(f"GITHUB_REPOSITORY/GITHUB_TOKEN not set; broken campuses: {broken or 'none'}")
        return 0

    gh = GitHub(repo, token, dry_run=args.dry_run)
    run_url = (
        f"{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{os.getenv('GITHUB_RUN_ID', '')}"
    )
    issues = gh.open_issues()

    for campus, entry in sorted(status.items()):
        issue = issues.get(campus)
        campus_freshness = freshness.get(campus)
        if campus in broken:
            if not entry.get("alerted"):
                entry["alerted"] = True
                changed = True
            body = issue_body(campus, entry, run_url, freshness=campus_freshness)
            if issue:
                gh.update_issue(issue["number"], body=body)
                continue
            reason = _alert_reason(entry, campus_freshness)
            gh.create_issue(f"[{campus}] menu scraper broken", body, [LABEL, f"campus:{campus}"])
            telegram(f"🔴 HungryBear: {campus} scraper broken\n{reason[:300]}\n{run_url}", args.dry_run)
        elif (issue or entry.get("alerted")) and entry["status"] == "ok":
            if issue:
                gh.comment(issue["number"], f"Recovered in {run_url} — closing.")
                gh.update_issue(issue["number"], state="closed", state_reason="completed")
            telegram(f"🟢 HungryBear: {campus} recovered", args.dry_run)
            if entry.pop("alerted", None) is not None:
                changed = True

    if changed and not args.dry_run:
        status_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")

    print(f"broken campuses: {broken or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
