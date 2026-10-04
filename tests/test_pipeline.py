# tests/test_pipeline.py
"""Validator, collector merge logic, and alert decisions - independent of any campus site."""

import json
from datetime import UTC, date, datetime

import pytest

from hungrybear import report
from hungrybear.collector import carry_over_past_meals
from hungrybear.config import CampusConfig
from hungrybear.models import DayMenu, Item, Location, Meal, Station, canonical_meal, parse_time_range
from hungrybear.validate import validate_day

MON = date(2026, 10, 5)
SAT = date(2026, 10, 3)


def cfg(**kw):
    base = dict(id="x", name="X", short_name="X", adapter="berkeley", main_halls=["hall"], min_items_per_meal=5)
    return CampusConfig(**{**base, **kw})


def meal(name, n, start=None, end=None):
    return Meal(
        name=name, start=start, end=end, stations=[Station(name="S", items=[Item(name=f"dish {i}") for i in range(n)])]
    )


def day(d=MON, status="ok", locations=None):
    return DayMenu(
        campus="x",
        date=d,
        fetched_at=datetime(2026, 10, 5, tzinfo=UTC),
        source_url="",
        status=status,
        locations=locations if locations is not None else [Location(id="hall", name="Hall", meals=[meal("Lunch", 20)])],
    )


# ---------------- validate ----------------


def test_healthy_day_is_ok():
    assert validate_day(day(), cfg(), today=MON).status == "ok"


def test_missing_main_hall_is_broken():
    m = validate_day(day(locations=[Location(id="other", name="O", meals=[meal("Lunch", 20)])]), cfg(), today=MON)
    assert m.status == "broken" and "missing" in m.reason


def test_thin_hall_is_broken_but_one_small_meal_is_not():
    thin = day(locations=[Location(id="hall", name="H", meals=[meal("Lunch", 2), meal("Dinner", 3)])])
    assert validate_day(thin, cfg(), today=MON).status == "broken"
    normal = day(locations=[Location(id="hall", name="H", meals=[meal("Brunch", 2), meal("Dinner", 30)])])
    assert validate_day(normal, cfg(), today=MON).status == "ok"


def test_empty_near_term_day_is_broken_unless_break_or_far_future():
    empty = day(status="closed", locations=[Location(id="hall", name="H")])
    assert validate_day(empty.model_copy(deep=True), cfg(), today=MON).status == "broken"
    assert validate_day(empty.model_copy(deep=True), cfg(breaks=[(MON, MON)]), today=MON).status == "closed"
    far = empty.model_copy(deep=True, update={"date": date(2026, 10, 9)})
    assert validate_day(far, cfg(), today=MON).status == "closed"


def test_weekday_only_hall_may_be_empty_on_weekends():
    weekend = day(
        d=SAT, locations=[Location(id="hall", name="H"), Location(id="b", name="B", meals=[meal("Lunch", 9)])]
    )
    assert validate_day(weekend, cfg(weekday_only=["hall"]), today=SAT).status == "ok"
    assert validate_day(weekend.model_copy(deep=True), cfg(), today=SAT).status == "broken"


def test_page_chrome_in_item_names_is_broken():
    junk = [Item(name="Filters"), Item(name="Include"), Item(name="Calories")] + [Item(name=f"d{i}") for i in range(10)]
    m = day(
        locations=[Location(id="hall", name="H", meals=[Meal(name="Lunch", stations=[Station(name="S", items=junk)])])]
    )
    assert validate_day(m, cfg(), today=MON).status == "broken"


def test_sharp_drop_vs_history_is_broken():
    assert validate_day(day(), cfg(), history_counts=[100, 110], today=MON).status == "broken"
    assert validate_day(day(), cfg(), history_counts=[22, 25], today=MON).status == "ok"


# ---------------- helpers ----------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Fall - Brunch", "Brunch"),
        ("Fall - Breakfast (ends at 10:30)", "Breakfast"),
        ("BREAKFAST", "Breakfast"),
        ("Late Night", "Late Night"),
        ("Spring - All Day", "All Day"),
        ("Bakery", None),
    ],
)
def test_canonical_meal(label, expected):
    assert canonical_meal(label) == expected


def test_parse_time_range():
    assert parse_time_range("4:30 p.m. - 9:00 p.m.") == ("16:30", "21:00")
    assert parse_time_range("7:15 AM - 10:00 AM") == ("07:15", "10:00")
    assert parse_time_range("10:00 p.m. - 12:00 a.m.") == ("22:00", "00:00")


def test_carry_over_keeps_meals_that_ended():
    old = day(
        locations=[
            Location(
                id="hall", name="H", meals=[meal("Breakfast", 9, "07:00", "10:00"), meal("Dinner", 9, "17:00", "21:00")]
            )
        ]
    )
    new = day(locations=[Location(id="hall", name="H", meals=[meal("Dinner", 9, "17:00", "21:00")])])
    merged = carry_over_past_meals(old, new, "18:00")
    assert [m.name for m in merged.location("hall").meals] == ["Breakfast", "Dinner"]
    # A meal that hasn't ended yet and vanished is not resurrected.
    new2 = day(locations=[Location(id="hall", name="H", meals=[])])
    assert [m.name for m in carry_over_past_meals(old, new2, "09:00").location("hall").meals] == []


# ---------------- report ----------------


class FakeGitHub:
    def __init__(self, issues):
        self.issues, self.calls = issues, []

    def open_issues(self):
        return self.issues

    def create_issue(self, title, body, labels):
        self.calls.append(("create", labels))
        return {"number": 7}

    def update_issue(self, number, **fields):
        self.calls.append(("update", number, fields.get("state")))

    def comment(self, number, body):
        self.calls.append(("comment", number))

    def dispatch(self, workflow, ref, inputs):
        self.calls.append(("dispatch", inputs["campus"], inputs["issue"]))


def run_report(tmp_path, monkeypatch, campuses, issues):
    (tmp_path / "v1").mkdir()
    (tmp_path / "v1" / "status.json").write_text(json.dumps({"campuses": campuses}))
    fake = FakeGitHub(issues)
    monkeypatch.setattr(report, "GitHub", lambda *a, **k: fake)
    monkeypatch.setattr(report, "telegram", lambda *a, **k: None)
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    report.main(["--data", str(tmp_path)])
    return fake.calls


def test_report_opens_issue_and_dispatches_autofix_after_two_failures(tmp_path, monkeypatch):
    broken = {
        "status": "broken",
        "consecutive_failures": 2,
        "days": {"2026-10-05": {"status": "broken", "reason": "x"}},
    }
    calls = run_report(tmp_path, monkeypatch, {"ucla": broken}, {})
    assert ("create", ["scraper-broken", "campus:ucla"]) in calls
    assert ("dispatch", "ucla", "7") in calls


def test_report_ignores_a_single_blip(tmp_path, monkeypatch):
    blip = {"status": "broken", "consecutive_failures": 1, "days": {}}
    assert run_report(tmp_path, monkeypatch, {"ucla": blip}, {}) == []


def test_report_closes_issue_on_recovery(tmp_path, monkeypatch):
    ok = {"status": "ok", "consecutive_failures": 0, "days": {}}
    calls = run_report(tmp_path, monkeypatch, {"ucla": ok}, {"ucla": {"number": 3}})
    assert ("comment", 3) in calls and ("update", 3, "closed") in calls
