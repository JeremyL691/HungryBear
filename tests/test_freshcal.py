# tests/test_freshcal.py
"""Tests for FreshCal BusinessCalendar break calendars, valid_until warnings, and freshness verdicts."""

from __future__ import annotations

import json
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from freshcal.core.model import Status

from hungrybear import report
from hungrybear.config import (
    PACIFIC,
    CampusConfig,
    calendar_expiry_warnings,
    load_campuses,
)
from hungrybear.models import DayMenu, Item, Location, Meal, Station
from hungrybear.validate import validate_day

SEMESTER_CAMPUSES = {"ucb", "ucm"}
QUARTER_CAMPUSES = {"ucla", "ucsb", "ucsc", "ucr", "ucsd", "ucd", "uci"}
ALL_CAMPUSES = SEMESTER_CAMPUSES | QUARTER_CAMPUSES


def _cfg(**kw) -> CampusConfig:
    base = dict(
        id="test", name="Test", short_name="Test", adapter="berkeley", main_halls=["hall"], min_items_per_meal=5
    )
    return CampusConfig(**{**base, **kw})


def _meal(name: str, n: int) -> Meal:
    return Meal(name=name, stations=[Station(name="S", items=[Item(name=f"dish {i}") for i in range(n)])])


def _day(d: date, status: str = "ok", locations=None) -> DayMenu:
    return DayMenu(
        campus="test",
        date=d,
        fetched_at=datetime(2026, 10, 5, tzinfo=UTC),
        source_url="",
        status=status,
        locations=locations
        if locations is not None
        else [Location(id="hall", name="Hall", meals=[_meal("Lunch", 20)])],
    )


# ---------------- dependency hygiene ----------------


def test_pyproject_uses_lightweight_freshcal_without_warehouse_extras():
    data = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
    deps = data["project"]["dependencies"]
    assert any(d.startswith("freshcal>=0.1.4") for d in deps), deps
    joined = " ".join(deps).lower()
    assert "duckdb" not in joined
    assert "postgres" not in joined and "psycopg" not in joined


# ---------------- BusinessCalendar: holidays & weekend=() ----------------


def test_weekends_are_normal_operating_days():
    campuses = load_campuses()
    for cid, cfg in campuses.items():
        assert cfg.calendar_spec().weekend == frozenset(), f"{cid} should have weekend=()"
        for weekend_day in (date(2026, 10, 3), date(2026, 10, 4), date(2027, 2, 6), date(2027, 2, 7)):
            assert not cfg.in_break(weekend_day), f"{cid} should operate on weekend {weekend_day}"
            assert cfg.break_reason(weekend_day) is None


def test_us_ca_statutory_holidays_are_breaks_with_human_readable_reasons():
    ucb = load_campuses()["ucb"]
    expected = {
        date(2026, 9, 7): "Labor Day (US-CA)",
        date(2026, 11, 11): "Veterans Day (US-CA)",
        date(2026, 11, 26): "Thanksgiving Day (US-CA)",
        date(2026, 11, 27): "Day After Thanksgiving (US-CA)",
        date(2026, 12, 25): "Christmas Day (US-CA)",
        date(2027, 1, 1): "New Year's Day (US-CA)",
        date(2027, 1, 18): "Martin Luther King Jr. Day (US-CA)",
        date(2027, 3, 31): "Cesar Chavez Day (US-CA)",
        date(2027, 5, 31): "Memorial Day (US-CA)",
    }
    for holiday_day, label in expected.items():
        assert ucb.in_break(holiday_day), f"expected statutory holiday on {holiday_day}"
        assert ucb.break_reason(holiday_day) == label


def test_us_federal_calendar_option_and_validation():
    federal_cfg = _cfg(holidays=["US"])
    assert federal_cfg.in_break(date(2026, 11, 26))
    assert federal_cfg.break_reason(date(2026, 11, 26)) == "Thanksgiving Day (US)"

    with pytest.raises(ValueError, match="unknown country"):
        _cfg(holidays=["ZZ"])


# ---------------- all 9 UC campuses' break windows ----------------


def test_all_nine_campuses_academic_break_windows():
    campuses = load_campuses()
    assert set(campuses) == ALL_CAMPUSES

    for cid, cfg in campuses.items():
        # Thanksgiving break: 2026-11-25 through 2026-11-29 (all 9)
        assert not cfg.in_break(date(2026, 11, 24)), f"{cid} break started too early"
        for d in range(25, 30):
            assert cfg.in_break(date(2026, 11, d)), f"{cid} missing Thanksgiving break day 2026-11-{d}"
        assert not cfg.in_break(date(2026, 11, 30)), f"{cid} Thanksgiving break ended too late"

        # 2027 Spring break: 2027-03-20 through 2027-03-28 (all 9)
        assert not cfg.in_break(date(2027, 3, 19)), f"{cid} spring break started too early"
        for d in range(20, 29):
            assert cfg.in_break(date(2027, 3, d)), f"{cid} missing Spring break day 2027-03-{d}"
        assert not cfg.in_break(date(2027, 3, 29)), f"{cid} spring break ended too late"

        if cid in SEMESTER_CAMPUSES:
            # Semester winter break: 2026-12-19 through 2027-01-18 (UCB, UCM)
            assert not cfg.in_break(date(2026, 12, 15)), f"{cid} semester campus should not be on break Dec 15"
            assert not cfg.in_break(date(2026, 12, 18)), f"{cid} semester winter break started too early"
            for d in (date(2026, 12, 19), date(2026, 12, 31), date(2027, 1, 5), date(2027, 1, 18)):
                assert cfg.in_break(d), f"{cid} missing semester winter break day {d}"
            assert not cfg.in_break(date(2027, 1, 19)), f"{cid} semester winter break ended too late"
        else:
            # Quarter winter break: 2026-12-12 through 2027-01-03 (other 7 campuses)
            assert not cfg.in_break(date(2026, 12, 11)), f"{cid} quarter winter break started too early"
            for d in (date(2026, 12, 12), date(2026, 12, 20), date(2027, 1, 3)):
                assert cfg.in_break(d), f"{cid} missing quarter winter break day {d}"
            assert not cfg.in_break(date(2027, 1, 4)), f"{cid} quarter winter break ended too late"


def test_human_readable_academic_break_and_override_reasons():
    ucb = load_campuses()["ucb"]
    assert ucb.break_reason(date(2026, 11, 25)) == "Thanksgiving break"
    assert ucb.break_reason(date(2026, 11, 26)) == "Thanksgiving Day (US-CA)"
    assert ucb.break_reason(date(2026, 11, 28)) == "Thanksgiving break"
    assert ucb.break_reason(date(2026, 12, 20)) == "Winter break"
    assert ucb.break_reason(date(2027, 3, 22)) == "Spring break"
    assert ucb.break_reason(date(2026, 11, 30)) is None

    unnamed = _cfg(breaks=[(date(2026, 10, 5), date(2026, 10, 5))])
    assert unnamed.in_break(date(2026, 10, 5))
    assert unnamed.break_reason(date(2026, 10, 5)) == "override: non-working day"


# ---------------- valid_until advance expiry warnings ----------------


def test_valid_until_configured_and_warns_before_expiry():
    campuses = load_campuses()
    for cid, cfg in campuses.items():
        assert cfg.calendar_valid_until == date(2027, 6, 30), f"{cid} missing calendar_valid_until"

    # Well before valid_until: no warnings
    early = datetime(2026, 11, 28, 12, 0, tzinfo=PACIFIC)
    assert calendar_expiry_warnings(campuses, now=early) == {}

    # Inside FreshCal's 34-day advance notice window: W005 emitted for all 9 campuses
    approaching = datetime(2027, 6, 5, 12, 0, tzinfo=PACIFIC)
    soon_warnings = calendar_expiry_warnings(campuses, now=approaching)
    assert set(soon_warnings) == ALL_CAMPUSES
    assert soon_warnings["ucb"].code == "W005"
    assert "2027-06-30" in soon_warnings["ucb"].message

    # Past valid_until: E408 emitted
    expired = datetime(2027, 7, 5, 12, 0, tzinfo=PACIFIC)
    expired_warnings = calendar_expiry_warnings(campuses, now=expired)
    assert set(expired_warnings) == ALL_CAMPUSES
    assert expired_warnings["ucb"].code == "E408"
    assert "2027-06-30" in expired_warnings["ucb"].message


# ---------------- report.py FreshCal freshness engine ----------------


def test_report_no_false_stale_during_thanksgiving_and_winter_breaks():
    campuses = load_campuses()
    ucb = campuses["ucb"]
    ucla = campuses["ucla"]

    # Last success Tuesday evening before Thanksgiving break; check Saturday afternoon (87h later, > 36h!)
    entry = {"status": "ok", "consecutive_failures": 0, "last_success": "2026-11-24T21:00:00-08:00"}
    mid_thanksgiving = datetime(2026, 11, 28, 12, 0, tzinfo=PACIFIC)
    res = report.evaluate_freshness("ucb", entry, mid_thanksgiving, cfg=ucb)
    assert res.status is Status.ON_TIME
    assert not report.is_stale(entry, mid_thanksgiving, cfg=ucb, campus="ucb")

    # Last success before winter break: UCB (semester) is ON_TIME on Jan 15, while UCLA (quarter) is OVERDUE!
    pre_winter = {"status": "ok", "consecutive_failures": 0, "last_success": "2026-12-18T18:00:00-08:00"}
    jan_15 = datetime(2027, 1, 15, 12, 0, tzinfo=PACIFIC)
    ucb_res = report.evaluate_freshness("ucb", pre_winter, jan_15, cfg=ucb)
    ucla_res = report.evaluate_freshness("ucla", pre_winter, jan_15, cfg=ucla)
    assert ucb_res.status is Status.ON_TIME
    assert ucla_res.status is Status.OVERDUE
    assert "Overdue:" in ucla_res.explanation


def test_report_first_operating_day_after_break_transitions_on_time_not_due_overdue():
    ucb = load_campuses()["ucb"]
    entry = {"status": "ok", "consecutive_failures": 0, "last_success": "2026-11-24T21:00:00-08:00"}

    before_release = datetime(2026, 11, 30, 2, 59, tzinfo=PACIFIC)
    in_grace = datetime(2026, 11, 30, 8, 30, tzinfo=PACIFIC)
    past_deadline = datetime(2026, 11, 30, 9, 5, tzinfo=PACIFIC)

    assert report.evaluate_freshness("ucb", entry, before_release, cfg=ucb).status is Status.ON_TIME
    assert report.evaluate_freshness("ucb", entry, in_grace, cfg=ucb).status is Status.NOT_DUE
    assert not report.is_stale(entry, in_grace, cfg=ucb, campus="ucb")

    overdue = report.evaluate_freshness("ucb", entry, past_deadline, cfg=ucb)
    assert overdue.status is Status.OVERDUE
    assert report.is_stale(entry, past_deadline, cfg=ucb, campus="ucb")
    assert "Overdue:" in overdue.explanation
    assert "Mon 2026-11-30 03:00 PST" in overdue.explanation
    assert "Mon 2026-11-30 09:00 PST" in overdue.explanation
    assert "1 release missed" in overdue.explanation


def test_report_holidays_vs_normal_weekends_and_timezone_equivalence():
    ucb = load_campuses()["ucb"]

    # Labor Day (US-CA holiday): Sunday evening data is still ON_TIME on Monday evening.
    pre_labor_day = {"status": "ok", "consecutive_failures": 0, "last_success": "2026-09-06T20:00:00-07:00"}
    labor_day_evening = datetime(2026, 9, 7, 18, 0, tzinfo=PACIFIC)
    assert report.evaluate_freshness("ucb", pre_labor_day, labor_day_evening, cfg=ucb).status is Status.ON_TIME

    # Normal Saturday (weekend=()): Friday evening data is OVERDUE by Saturday 09:05 PDT.
    friday_pdt = {"status": "ok", "consecutive_failures": 0, "last_success": "2026-10-02T20:00:00-07:00"}
    friday_utc = {"status": "ok", "consecutive_failures": 0, "last_success": "2026-10-03T03:00:00Z"}
    sat_late_pdt = datetime(2026, 10, 3, 9, 5, tzinfo=PACIFIC)
    sat_late_utc = sat_late_pdt.astimezone(UTC)

    r_pdt = report.evaluate_freshness("ucb", friday_pdt, sat_late_pdt, cfg=ucb)
    r_utc = report.evaluate_freshness("ucb", friday_utc, sat_late_utc, cfg=ucb)
    assert r_pdt.status is Status.OVERDUE and r_utc.status is Status.OVERDUE
    assert r_pdt.explanation == r_utc.explanation


def test_report_handles_missing_invalid_and_naive_timestamps_cleanly():
    ucb = load_campuses()["ucb"]
    now = datetime(2026, 10, 5, 12, 0, tzinfo=PACIFIC)

    missing = report.evaluate_freshness("ucb", {"status": "ok", "consecutive_failures": 0}, now, cfg=ucb)
    assert missing.status is Status.NO_DATA
    assert not report.is_stale({"status": "ok", "consecutive_failures": 0}, now, cfg=ucb, campus="ucb")

    invalid = report.evaluate_freshness("ucb", {"last_success": "not-a-timestamp"}, now, cfg=ucb)
    assert invalid.status is Status.QUERY_ERROR
    assert invalid.error is not None and invalid.error.code == "E502"

    naive = report.evaluate_freshness("ucb", {"last_success": "2026-10-05T12:00:00"}, now, cfg=ucb)
    assert naive.status is Status.CONFIG_ERROR
    assert naive.error is not None and naive.error.code == "E214"


def test_report_main_suppresses_break_alerts_and_surfaces_overdue_and_valid_until(tmp_path, monkeypatch, capsys):
    status_file = tmp_path / "v1" / "status.json"
    status_file.parent.mkdir()
    sent: list[str] = []
    monkeypatch.setattr(report, "telegram", lambda text, dry_run: sent.append(text))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    # 1. During Thanksgiving break: last_success from Tuesday evening -> NO alert
    status_file.write_text(
        json.dumps(
            {
                "campuses": {
                    "ucb": {
                        "status": "ok",
                        "consecutive_failures": 0,
                        "last_success": "2026-11-24T21:00:00-08:00",
                        "days": {},
                    }
                }
            }
        )
    )
    report.main(["--data", str(tmp_path), "--now", "2026-11-28T12:00:00-08:00"])
    assert sent == []

    # 2. First operating day after break, past deadline -> OVERDUE Telegram alert with FreshCal explanation
    report.main(["--data", str(tmp_path), "--now", "2026-11-30T09:30:00-08:00"])
    assert len(sent) == 1
    assert "ucb scraper broken" in sent[0]
    assert "Overdue: release Mon 2026-11-30 03:00 PST missed its deadline" in sent[0]

    # 3. Approaching valid_until (2027-06-05): emits W005 calendar warning without false overdue
    status_file.write_text(
        json.dumps(
            {
                "campuses": {
                    "ucb": {
                        "status": "ok",
                        "consecutive_failures": 0,
                        "last_success": "2027-06-05T05:00:00-07:00",
                        "days": {},
                    }
                }
            }
        )
    )
    capsys.readouterr()
    report.main(["--data", str(tmp_path), "--now", "2027-06-05T12:00:00-07:00"])
    out = capsys.readouterr().out
    assert "calendar warning [ucb] W005:" in out
    assert "2027-06-30" in out
    assert len(sent) == 1  # still no extra broken alert


# ---------------- validate.py integration with FreshCal ----------------


def test_validate_break_guards_work_for_statutory_holidays_and_keep_ui_noise_and_service_over():
    ucb_like = _cfg()  # defaults to US-CA holidays, no explicit breaks list
    veterans_day = date(2026, 11, 11)
    previous = _day(veterans_day, locations=[Location(id="hall", name="H", meals=[_meal("Lunch", 40)])])

    # 1. Empty/closed day on a US-CA statutory holiday -> closed, not broken (guard #1 & #2)
    closed = _day(veterans_day, status="closed", locations=[Location(id="hall", name="H")])
    assert validate_day(closed, ucb_like, today=veterans_day, previous=previous).status == "closed"

    # 2. Missing main hall & thin meal & weekday drop -> ok on statutory holiday (guards #3, #4, history)
    missing_hall = _day(veterans_day, locations=[Location(id="other", name="O", meals=[_meal("Lunch", 12)])])
    assert validate_day(missing_hall, ucb_like, history_counts=[100, 110], today=veterans_day).status == "ok"

    thin_meal = _day(veterans_day, locations=[Location(id="hall", name="H", meals=[_meal("Lunch", 2)])])
    assert (
        validate_day(thin_meal, ucb_like, history_counts=[100, 110], today=veterans_day, previous=previous).status
        == "ok"
    )

    # 3. UI_NOISE structural crash check STILL fires on statutory holidays and academic breaks!
    junk = [Item(name="Filters"), Item(name="Include"), Item(name="Calories")] + [Item(name=f"d{i}") for i in range(10)]
    chrome = _day(
        veterans_day,
        locations=[Location(id="hall", name="H", meals=[Meal(name="Lunch", stations=[Station(name="S", items=junk)])])],
    )
    res_chrome = validate_day(chrome, ucb_like, today=veterans_day)
    assert res_chrome.status == "broken" and "page chrome" in (res_chrome.reason or "")

    # 4. Normal Saturday (weekend=()): NOT a break, so missing main hall still alerts!
    normal_sat = date(2026, 10, 3)
    sat_missing = _day(normal_sat, locations=[Location(id="other", name="O", meals=[_meal("Lunch", 20)])])
    assert validate_day(sat_missing, ucb_like, today=normal_sat).status == "broken"

    # 5. service_over=True still suppresses "previously had" on a normal operating day
    normal_mon = date(2026, 10, 5)
    prev_mon = _day(normal_mon, locations=[Location(id="hall", name="H", meals=[_meal("Lunch", 40)])])
    late_empty = _day(normal_mon, status="closed", locations=[Location(id="hall", name="H")])
    assert validate_day(late_empty, ucb_like, today=normal_mon, previous=prev_mon, service_over=True).status == "closed"
