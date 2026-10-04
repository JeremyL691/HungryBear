# hungrybear/models.py
"""Unified menu schema shared by every campus adapter, the collector, and the bot.

This is also the public JSON API shape (/v1/{campus}/{date}.json), so changes here
are breaking changes for API consumers - bump SCHEMA_VERSION if you change it.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

# Canonical meal periods, in display order.
MEAL_ORDER = ["Breakfast", "Brunch", "Lunch", "Dinner", "Late Night", "All Day"]

# Canonical dietary tags / allergens. Adapters map their site's labels onto these.
TAGS = {"vegan", "vegetarian", "halal", "kosher", "gluten_free"}
ALLERGENS = {
    "milk",
    "egg",
    "fish",
    "shellfish",
    "tree_nuts",
    "peanuts",
    "wheat",
    "soy",
    "sesame",
    "gluten",
    "pork",
    "alcohol",
    "coconut",
}

LocationType = Literal["dining_hall", "cafe", "market", "restaurant"]
LocationStatus = Literal["open", "closed", "unknown"]
# ok      = parsed and validated
# closed  = source reachable and well-formed, but no menu that day (weekend/break/holiday)
# broken  = fetch/parse/validation failed -> alert
DayStatus = Literal["ok", "closed", "broken"]


class Item(BaseModel):
    name: str
    tags: List[str] = Field(default_factory=list)
    allergens: List[str] = Field(default_factory=list)
    calories: Optional[int] = None
    description: Optional[str] = None


class Station(BaseModel):
    name: str
    items: List[Item] = Field(default_factory=list)


class Meal(BaseModel):
    name: str  # one of MEAL_ORDER
    start: Optional[str] = None  # "HH:MM" 24h, local Pacific time
    end: Optional[str] = None
    note: Optional[str] = None  # e.g. "ends at 10:30"
    stations: List[Station] = Field(default_factory=list)

    @property
    def item_count(self) -> int:
        return sum(len(s.items) for s in self.stations)


class Location(BaseModel):
    id: str  # short slug, stable across days (used in Telegram callback_data)
    name: str
    type: LocationType = "dining_hall"
    status: LocationStatus = "unknown"
    meals: List[Meal] = Field(default_factory=list)


class DayMenu(BaseModel):
    schema_version: int = SCHEMA_VERSION
    campus: str
    date: date
    fetched_at: datetime
    source_url: str
    status: DayStatus = "ok"
    reason: Optional[str] = None
    locations: List[Location] = Field(default_factory=list)

    def location(self, loc_id: str) -> Optional[Location]:
        return next((loc for loc in self.locations if loc.id == loc_id), None)


# ---------------- helpers shared by adapters ----------------


def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def slugify(s: str) -> str:
    s = strip_accents(s).lower().replace("&", " and ").replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:32].rstrip("-")


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def canonical_meal(raw: str) -> Optional[str]:
    """Map a site's meal label ("Fall - Brunch", "BREAKFAST", "Late Night (9-12)") to MEAL_ORDER."""
    low = strip_accents(raw or "").lower()
    low = re.sub(r"\(.*?\)", " ", low)
    low = re.sub(r"\s+", " ", low).strip()
    # Check longer names first so "late night" / "all day" win over substrings.
    if "late night" in low or "latenight" in low:
        return "Late Night"
    if "all day" in low or "allday" in low:
        return "All Day"
    if "brunch" in low:
        return "Brunch"
    if "breakfast" in low:
        return "Breakfast"
    if "lunch" in low:
        return "Lunch"
    if "dinner" in low:
        return "Dinner"
    return None


_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?", re.I)


def parse_time(s: str) -> Optional[str]:
    """'4:30 p.m.' / '7AM' / '11:00AM' -> '16:30' / '07:00' / '11:00'."""
    m = _TIME_RE.search(s or "")
    if not m:
        return None
    h, mins, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
    if h == 12:
        h = 0
    if ap == "p":
        h += 12
    return f"{h:02d}:{mins:02d}"


def parse_time_range(s: str) -> tuple[Optional[str], Optional[str]]:
    """'4:30 p.m. - 9:00 p.m.' -> ('16:30', '21:00'). Handles '11-2pm' style poorly by design (returns None)."""
    parts = re.split(r"\s*[-–—]\s*|\s+to\s+", s or "", maxsplit=1)
    if len(parts) != 2:
        return None, None
    return parse_time(parts[0]), parse_time(parts[1])


def sort_meals(meals: List[Meal]) -> List[Meal]:
    order = {m: i for i, m in enumerate(MEAL_ORDER)}
    return sorted(meals, key=lambda m: order.get(m.name, 99))
