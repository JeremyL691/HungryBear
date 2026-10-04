# hungrybear/devtools.py
"""Fixture tooling for adapter tests (also what the autofix agent uses).

# record live responses for a campus into tests/fixtures/<campus>/ and write the golden summary
python -m hungrybear.devtools fixture ucla --days 2

# re-run the adapter against the recorded responses and rewrite the golden summary
python -m hungrybear.devtools golden ucla

# print the summary for a recorded fixture without writing anything
python -m hungrybear.devtools show ucla
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import List

from .adapters import build_adapter
from .config import CampusConfig, load_campuses, today_pacific
from .http import RecordingFetcher, ReplayFetcher
from .models import DayMenu
from .validate import validate_day

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def summarize(menu: DayMenu) -> str:
    """Stable, human-diffable text rendering of a DayMenu (golden file format)."""
    out = [f"== {menu.campus} {menu.date} status={menu.status}" + (f" reason={menu.reason}" if menu.reason else "")]
    for loc in menu.locations:
        out.append(f"{loc.id} | {loc.name} [{loc.type}/{loc.status}]")
        for meal in loc.meals:
            hours = f"{meal.start or '?'}-{meal.end or '?'}"
            out.append(f"  {meal.name} {hours}" + (f" ({meal.note})" if meal.note else ""))
            for st in meal.stations:
                out.append(f"    # {st.name}")
                for it in st.items:
                    marks = ",".join(it.tags + [f"!{a}" for a in it.allergens])
                    cal = f" {it.calories}cal" if it.calories is not None else ""
                    out.append(f"      - {it.name}" + (f" [{marks}]" if marks else "") + cal)
    return "\n".join(out) + "\n"


def fixture_config(campus: str, overrides: dict) -> CampusConfig:
    """Campus config with fixture overrides applied (e.g. record only a few venues of a huge site)."""
    cfg = load_campuses()[campus]
    overrides = dict(overrides or {})
    options = {**cfg.options, **overrides.pop("options", {})}
    return cfg.model_copy(update={**overrides, "options": options})


def replay_days(campus: str) -> List[DayMenu]:
    d = FIXTURES / campus
    meta = json.loads((d / "meta.json").read_text())
    cfg = fixture_config(campus, meta.get("overrides", {}))
    start = date.fromisoformat(meta["start"])
    adapter = build_adapter(cfg, ReplayFetcher(d / "responses"), today=start)
    days = []
    for i in range(meta["days"]):
        menu = adapter.fetch_day(start + timedelta(days=i))
        days.append(validate_day(menu, cfg, today=start))
    return days


def write_golden(campus: str) -> Path:
    path = FIXTURES / campus / "expected.txt"
    path.write_text("".join(summarize(m) for m in replay_days(campus)))
    return path


def record_fixture(campus: str, start: date, days: int, overrides: dict) -> None:
    cfg = fixture_config(campus, overrides)
    d = FIXTURES / campus
    shutil.rmtree(d / "responses", ignore_errors=True)
    fetcher = RecordingFetcher(d / "responses")
    try:
        adapter = build_adapter(cfg, fetcher, today=start)
        for i in range(days):
            adapter.fetch_day(start + timedelta(days=i))
    finally:
        fetcher.close()
    meta = {"start": start.isoformat(), "days": days}
    if overrides:
        meta["overrides"] = overrides
    (d / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fixture")
    f.add_argument("campus")
    f.add_argument("--start", type=date.fromisoformat, default=None)
    f.add_argument("--days", type=int, default=2)
    f.add_argument(
        "--override", type=json.loads, default={}, help='config overrides, e.g. \'{"options": {"max_venues": 2}}\''
    )
    g = sub.add_parser("golden")
    g.add_argument("campus")
    s = sub.add_parser("show")
    s.add_argument("campus")
    args = ap.parse_args(argv)

    if args.cmd == "fixture":
        record_fixture(args.campus, args.start or today_pacific(), args.days, args.override)
        print(f"recorded -> {FIXTURES / args.campus}; golden -> {write_golden(args.campus)}")
    elif args.cmd == "golden":
        print(f"golden -> {write_golden(args.campus)}")
    else:
        sys.stdout.write("".join(summarize(m) for m in replay_days(args.campus)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
