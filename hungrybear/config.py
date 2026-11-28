# hungrybear/config.py

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple
from zoneinfo import ZoneInfo

import yaml
from freshcal.adapters.holidays_provider import HolidaysCalendarProvider, validate_ref
from freshcal.core.calendar import BusinessCalendar
from freshcal.core.errors import CalendarError, Issue
from freshcal.core.model import (
    BusinessDaysSchedule,
    CalendarSpec,
    FreshnessTarget,
    HolidayCalendarRef,
    Origin,
    SourceRule,
)
from freshcal.core.timeutil import local_date
from freshcal.core.verdict import calendar_notice
from pydantic import BaseModel, Field, field_validator, model_validator

PACIFIC = ZoneInfo("America/Los_Angeles")
CAMPUSES_FILE = Path(__file__).with_name("campuses.yaml")

# Shared holiday provider cache across all campus calendars.
HOLIDAY_PROVIDER = HolidaysCalendarProvider()

# Earliest daily collector refresh is 11:30 UTC (03:30 PST / 04:30 PDT).
# Setting the expected release at 03:00 Pacific ensures every morning run satisfies
# that day's release (arrival requires observed >= release) while avoiding DST gaps/folds.
DEFAULT_FRESHNESS_RELEASE_TIME = time(3, 0)
DEFAULT_FRESHNESS_GRACE = timedelta(hours=6)


def parse_holiday_ref(spec: str) -> HolidayCalendarRef:
    """Parse a holiday code like 'US' or 'US-CA' into a validated FreshCal HolidayCalendarRef."""
    cleaned = spec.strip()
    if not cleaned:
        raise ValueError("holiday calendar code must not be empty")
    if ":" in cleaned:
        kind, code = cleaned.split(":", 1)
        kind = kind.strip()
        code = code.strip()
        if kind == "financial":
            ref = HolidayCalendarRef(kind="financial", code=code)
        elif kind == "country":
            country, _, subdiv = code.partition("-")
            ref = HolidayCalendarRef(kind="country", code=country, subdivision=subdiv or None)
        else:
            raise ValueError(f"unsupported holiday calendar kind {kind!r} in {spec!r}")
    else:
        country, _, subdiv = cleaned.partition("-")
        ref = HolidayCalendarRef(kind="country", code=country, subdivision=subdiv or None)
    issue = validate_ref(ref, f"holidays[{cleaned}]")
    if issue is not None:
        raise ValueError(issue.message)
    return ref


@lru_cache(maxsize=64)
def _build_calendar_spec(
    name: str,
    holidays: Tuple[str, ...],
    break_ranges: Tuple[Tuple[date, date], ...],
    valid_until: Optional[date],
) -> CalendarSpec:
    holiday_refs = tuple(parse_holiday_ref(spec) for spec in holidays)
    non_working: set[date] = set()
    for start, end in break_ranges:
        day = start
        while day <= end:
            non_working.add(day)
            day += timedelta(days=1)
    # UC dining halls operate on weekends, so weekend is empty.
    return CalendarSpec(
        weekend=frozenset(),
        holiday_calendars=holiday_refs,
        extra_non_working_days=frozenset(non_working),
        valid_until=valid_until,
        name=name,
    )


class BreakWindow(BaseModel):
    start: date
    end: date
    name: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_sequence(cls, value: Any) -> Any:
        if isinstance(value, (tuple, list)):
            if len(value) == 2:
                return {"start": value[0], "end": value[1]}
            if len(value) == 3:
                return {"start": value[0], "end": value[1], "name": value[2]}
            raise ValueError(f"break window must have 2 or 3 items, got {len(value)}")
        return value

    @model_validator(mode="after")
    def _validate_order(self) -> BreakWindow:
        if self.end < self.start:
            raise ValueError(f"break end {self.end.isoformat()} is before start {self.start.isoformat()}")
        return self


class CampusConfig(BaseModel):
    id: str
    name: str
    short_name: str
    adapter: str
    enabled: bool = True
    # Where the collector runs: "ci" (GitHub Actions) or "local" (the maintainer's Mac, for sites that
    # block cloud IPs). See scripts/collect_local.sh.
    runner: Literal["ci", "local"] = "ci"
    options: Dict[str, Any] = Field(default_factory=dict)
    # Validation expectations (see validate.py)
    main_halls: List[str] = Field(default_factory=list)  # location ids expected to serve full meals
    weekday_only: List[str] = Field(default_factory=list)  # main halls that are normally closed Sat/Sun
    min_items_per_meal: int = 8  # for main halls
    # FreshCal calendar configuration (statutory holidays + campus academic breaks)
    holidays: List[str] = Field(default_factory=lambda: ["US-CA"])
    calendar_valid_until: Optional[date] = None
    breaks: List[BreakWindow] = Field(default_factory=list)  # inclusive; suppress closure/volume alerts

    @field_validator("holidays")
    @classmethod
    def _validate_holidays(cls, values: List[str]) -> List[str]:
        for spec in values:
            parse_holiday_ref(spec)
        return values

    def calendar_spec(self) -> CalendarSpec:
        """Build the FreshCal CalendarSpec for this campus (weekend=(), US-CA holidays, academic breaks)."""
        return _build_calendar_spec(
            self.id,
            tuple(self.holidays),
            tuple((brk.start, brk.end) for brk in self.breaks),
            self.calendar_valid_until,
        )

    def business_calendar(self, provider: Optional[HolidaysCalendarProvider] = None) -> BusinessCalendar:
        """Return a FreshCal BusinessCalendar driving operating-day and break checks for this campus."""
        return BusinessCalendar(self.calendar_spec(), provider or HOLIDAY_PROVIDER, source_id=self.id)

    def freshness_rule(
        self,
        *,
        release_time: time = DEFAULT_FRESHNESS_RELEASE_TIME,
        grace: timedelta = DEFAULT_FRESHNESS_GRACE,
    ) -> SourceRule:
        """Build the FreshCal SourceRule for daily operating-day freshness evaluation."""
        return SourceRule(
            source_id=self.id,
            origin=Origin.CONFIG,
            schedule=BusinessDaysSchedule(at=release_time, timezone=PACIFIC),
            calendar=self.calendar_spec(),
            grace=grace,
            target=FreshnessTarget(relation=f"status.{self.id}", loaded_at_field="last_success"),
        )

    def in_break(self, day: date) -> bool:
        """True when `day` is a statutory holiday or configured campus break (non-operating day)."""
        return not self.business_calendar().is_business_day(day)

    def break_reason(self, day: date) -> Optional[str]:
        """Human-readable holiday or academic break reason for `day`, or None on a normal operating day."""
        cal = self.business_calendar()
        if cal.is_business_day(day):
            return None
        holiday = cal.holiday_name(day)
        if holiday is not None:
            # FreshCal formats country holidays as "Thanksgiving Day (country US-CA)" -> "Thanksgiving Day (US-CA)".
            return holiday.replace("(country ", "(")
        for brk in self.breaks:
            if brk.start <= day <= brk.end and brk.name:
                return brk.name
        return cal.non_business_reason(day)

    def expiry_warning(
        self,
        now: Optional[datetime] = None,
        *,
        provider: Optional[HolidaysCalendarProvider] = None,
    ) -> Optional[Issue]:
        """Return FreshCal's E408 (expired) or W005 (approaching valid_until) issue, if any."""
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        rule = self.freshness_rule()
        cal = self.business_calendar(provider=provider)
        try:
            cal.check_valid_at(local_date(instant, PACIFIC))
        except CalendarError as err:
            return Issue(err.issue.code, err.issue.message, self.id)
        return calendar_notice(rule, cal, instant)


def calendar_expiry_warnings(
    campuses: Mapping[str, CampusConfig],
    now: Optional[datetime] = None,
    *,
    provider: Optional[HolidaysCalendarProvider] = None,
) -> Dict[str, Issue]:
    """Return `{campus_id: Issue}` for campuses whose calendar is expiring soon (W005) or expired (E408)."""
    warnings: Dict[str, Issue] = {}
    for cid, cfg in campuses.items():
        issue = cfg.expiry_warning(now=now, provider=provider)
        if issue is not None:
            warnings[cid] = issue
    return warnings


def load_campuses(path: Optional[Path] = None) -> Dict[str, CampusConfig]:
    raw = yaml.safe_load((path or CAMPUSES_FILE).read_text())
    return {cid: CampusConfig(id=cid, **spec) for cid, spec in raw.items()}


def today_pacific() -> date:
    return datetime.now(PACIFIC).date()
