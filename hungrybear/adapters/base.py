# hungrybear/adapters/base.py

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, date, datetime
from typing import List, Optional

from ..config import CampusConfig, today_pacific
from ..http import Fetcher
from ..models import DayMenu, Location, sort_meals


class AdapterError(RuntimeError):
    """The source responded, but not in the shape we expect (site changed) -> 'broken'."""


class Adapter(ABC):
    """One adapter per menu platform. A config entry in campuses.yaml binds it to a campus.

    Contract for fetch_day():
    - return DayMenu(status="ok") with at least one location that has meals, or
    - return DayMenu(status="closed") when the source is healthy but has no menu that day, or
    - raise AdapterError / FetchError when anything looks off. Never return a silently-empty "ok".
    """

    #: how many days ahead (including today) the source publishes
    max_days: int = 7

    def __init__(self, config: CampusConfig, fetcher: Fetcher, today: Optional[date] = None) -> None:
        self.config = config
        self.fetcher = fetcher
        self.options = config.options
        # "Today" for sources that address days relative to now (UCSD dayNum); replay passes the recording date.
        self.today = today or today_pacific()

    @abstractmethod
    def fetch_day(self, day: date) -> DayMenu: ...

    # ---------------- helpers ----------------

    def make_day(
        self,
        day: date,
        source_url: str,
        locations: List[Location],
        *,
        status: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> DayMenu:
        for loc in locations:
            loc.meals = sort_meals(loc.meals)
            if loc.status == "unknown":
                loc.status = "open" if loc.meals else "closed"
        if status is None:
            status = "ok" if any(loc.meals for loc in locations) else "closed"
        return DayMenu(
            campus=self.config.id,
            date=day,
            fetched_at=datetime.now(UTC).replace(microsecond=0),
            source_url=source_url,
            status=status,  # type: ignore[arg-type]
            reason=reason,
            locations=locations,
        )
