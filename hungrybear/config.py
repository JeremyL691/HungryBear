# hungrybear/config.py

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field

PACIFIC = ZoneInfo("America/Los_Angeles")
CAMPUSES_FILE = Path(__file__).with_name("campuses.yaml")


class CampusConfig(BaseModel):
    id: str
    name: str
    short_name: str
    adapter: str
    enabled: bool = True
    options: Dict[str, Any] = Field(default_factory=dict)
    # Validation expectations (see validate.py)
    main_halls: List[str] = Field(default_factory=list)  # location ids expected to serve full meals
    weekday_only: List[str] = Field(default_factory=list)  # main halls that are normally closed Sat/Sun
    min_items_per_meal: int = 8  # for main halls
    breaks: List[Tuple[date, date]] = Field(default_factory=list)  # inclusive; no alerts if closed

    def in_break(self, day: date) -> bool:
        return any(start <= day <= end for start, end in self.breaks)


def load_campuses(path: Optional[Path] = None) -> Dict[str, CampusConfig]:
    raw = yaml.safe_load((path or CAMPUSES_FILE).read_text())
    return {cid: CampusConfig(id=cid, **spec) for cid, spec in raw.items()}


def today_pacific() -> date:
    return datetime.now(PACIFIC).date()
