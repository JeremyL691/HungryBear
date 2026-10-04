# hungrybear/collector.py
"""Fetch, validate, and publish menus for every campus.

  python -m hungrybear.collector --campus all --days 7 --out data/
  python -m hungrybear.collector --campus ucla --days 2 --out /tmp/out --record /tmp/snap
  python -m hungrybear.collector --campus ucb --replay tests/fixtures/ucb --start 2026-10-05 --days 1

A broken day never overwrites the last good file for that date (last-known-good), and
status.json tracks consecutive failures per campus so report.py can alert on real outages
rather than one-off blips.
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .adapters import NotPublished, build_adapter
from .config import PACIFIC, CampusConfig, load_campuses, today_pacific
from .http import Fetcher, RecordingFetcher, ReplayFetcher
from .models import DayMenu, sort_meals
from .store import LocalStore
from .validate import total_items, validate_day

log = logging.getLogger("hungrybear.collector")


@dataclass
class CampusRun:
    campus: str
    days: List[DayMenu] = field(default_factory=list)
    written: int = 0

    @property
    def broken(self) -> List[DayMenu]:
        return [d for d in self.days if d.status == "broken"]


def carry_over_past_meals(old: Optional[DayMenu], new: DayMenu, now_hhmm: str) -> DayMenu:
    """Some sites drop meals from *today's* page once they end (UCSB). Keep ones we saw earlier today."""
    if old is None or old.status != "ok" or old.date != new.date:
        return new
    for old_loc in old.locations:
        loc = new.location(old_loc.id)
        if loc is None:
            continue
        have = {m.name for m in loc.meals}
        for meal in old_loc.meals:
            end = "24:00" if meal.end == "00:00" else meal.end
            if meal.name not in have and meal.stations and end and end <= now_hhmm:
                loc.meals.append(meal)
        loc.meals = sort_meals(loc.meals)
        if loc.meals:
            loc.status = "open"
    if new.status == "closed" and any(loc.meals for loc in new.locations):
        new.status = "ok"
    return new


def fetch_validated(adapter, cfg: CampusConfig, store: LocalStore, day: date, start: date) -> DayMenu:
    """Fetch + validate one day. Any failure becomes a 'broken' DayMenu (data for the alert, never a crash).
    NotPublished propagates so the caller can skip the day."""
    try:
        menu = adapter.fetch_day(day)
        if day == today_pacific():
            now = datetime.now(PACIFIC).strftime("%H:%M")
            menu = carry_over_past_meals(store.read_day(cfg.id, day), menu, now)
        return validate_day(menu, cfg, store.history_counts(cfg.id, day), today=start)
    except NotPublished:
        raise
    except Exception as e:
        log.debug("%s %s failed:\n%s", cfg.id, day, traceback.format_exc())
        return DayMenu(
            campus=cfg.id,
            date=day,
            fetched_at=datetime.now(UTC).replace(microsecond=0),
            source_url="",
            status="broken",
            reason=f"{type(e).__name__}: {e}",
        )


def collect_campus(
    cfg: CampusConfig,
    store: LocalStore,
    start: date,
    days: int,
    record: Optional[Path] = None,
    replay: Optional[Path] = None,
) -> CampusRun:
    run = CampusRun(cfg.id)
    if replay:
        fetcher: Fetcher = ReplayFetcher(replay)
    elif record:
        fetcher = RecordingFetcher(record / cfg.id)
    else:
        fetcher = Fetcher()
    try:
        adapter = build_adapter(cfg, fetcher, today=start)
        for offset in range(min(days, adapter.max_days)):
            day = start + timedelta(days=offset)
            try:
                menu = fetch_validated(adapter, cfg, store, day, start)
            except NotPublished as e:
                log.info("%-5s %s skip   %s", cfg.id, day, e)
                continue
            run.days.append(menu)
            if menu.status != "broken" and store.write_day(menu):
                run.written += 1
            log.info("%-5s %s %-6s items=%-4d %s", cfg.id, day, menu.status, total_items(menu), menu.reason or "")
    finally:
        fetcher.close()
    return run


def update_status(store: LocalStore, campuses: Dict[str, CampusConfig], runs: List[CampusRun], today: date) -> dict:
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    status = store.read_json("status.json") or {"campuses": {}}
    for run in runs:
        prev = status["campuses"].get(run.campus, {})
        entry = {
            "status": "broken" if run.broken else "ok",
            "checked_at": now,
            "last_success": now if not run.broken else prev.get("last_success"),
            "consecutive_failures": (prev.get("consecutive_failures", 0) + 1) if run.broken else 0,
            "days": {d.date.isoformat(): {"status": d.status, "reason": d.reason} for d in run.days},
        }
        status["campuses"][run.campus] = entry
    status["generated_at"] = now
    store.write_json("status.json", status)

    index = {
        "api_version": "v1",
        "generated_at": now,
        "today": today.isoformat(),
        "campuses": [
            {
                "id": cid,
                "name": cfg.name,
                "short_name": cfg.short_name,
                "status": status["campuses"].get(cid, {}).get("status", "unknown"),
                "last_success": status["campuses"].get(cid, {}).get("last_success"),
                "dates": [d.isoformat() for d in store.dates(cid) if d >= today],
            }
            for cid, cfg in campuses.items()
            if cfg.enabled
        ],
    }
    store.write_json("index.json", index)
    return status


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campus", default="all", help="comma-separated campus ids, or 'all'")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--start", type=date.fromisoformat, default=None, help="first day (default: today, Pacific)")
    ap.add_argument("--out", type=Path, default=Path("data"))
    ap.add_argument("--record", type=Path, default=None, help="save raw responses here (per campus)")
    ap.add_argument("--replay", type=Path, default=None, help="serve responses recorded in this dir (single campus)")
    ap.add_argument("--strict", action="store_true", help="exit 1 if any campus is broken")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    campuses = load_campuses()
    wanted = [c for c in campuses if campuses[c].enabled] if args.campus == "all" else args.campus.split(",")
    unknown = [c for c in wanted if c not in campuses]
    if unknown:
        ap.error(f"unknown campus: {unknown}")
    if args.replay and len(wanted) != 1:
        ap.error("--replay works with a single --campus")

    start = args.start or today_pacific()
    store = LocalStore(args.out)
    with ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(
            pool.map(lambda c: collect_campus(campuses[c], store, start, args.days, args.record, args.replay), wanted)
        )
    for c in wanted:
        store.prune(c, start)
    update_status(store, campuses, runs, start)

    broken = [r for r in runs if r.broken]
    print(f"\n{len(runs) - len(broken)}/{len(runs)} campuses ok; files written: {sum(r.written for r in runs)}")
    for r in broken:
        for d in r.broken:
            print(f"  BROKEN {r.campus} {d.date}: {d.reason}")
    return 1 if (args.strict and broken) else 0


if __name__ == "__main__":
    sys.exit(main())
