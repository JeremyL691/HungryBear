# hungrybear/adapters/ucsd.py
"""UC San Diego - HDH Dining (hdh-web.ucsd.edu).

  Restaurants/Restaurants                         venue links: Venue_V3?locId=N&locDetID=M&dayNum=0
  Restaurants/Venue_V3?locId&locDetID&dayNum=K    K = days from today (0..6)
    title                                         "<Venue> Food Menu | HDH Dining | UC San Diego"
    h2.datenow                                    "Saturday, October 03 2026"
    div.meal-category#Breakfast|Lunch|Dinner|Brunch|Lunch_Dinner   (h3 "Closed" when not served)
      div.menu-category-section > h3              sub-venue ("Morning Fog")
        .panel-heading h4                         category ("Beverages")
        a.sublocsitem                             dish; sibling img[title] icons + span.cals
    Every dish is rendered twice (desktop + mobile layouts) - dedupe per station.

Pages are ~3 MB each, so this is the heaviest campus to collect.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from ..models import DayMenu, Item, Location, Meal, Station, canonical_meal, clean_text, slugify
from .base import Adapter, AdapterError

BASE = "https://hdh-web.ucsd.edu/dining/apps/diningservices/Restaurants/"
LIST_URL = urljoin(BASE, "Restaurants")
VENUE_URL = urljoin(BASE, "Venue_V3")

ICON_TAGS = {"vegan": "vegan", "vegetarian": "vegetarian", "halal": "halal", "gluten free": "gluten_free"}
ICON_ALLERGENS = {
    "contains soy": "soy",
    "contains wheat": "wheat",
    "contains gluten": "gluten",
    "contains dairy": "milk",
    "contains eggs": "egg",
    "contains sesame": "sesame",
    "contains fish": "fish",
    "contains treenuts": "tree_nuts",
    "contains shellfish": "shellfish",
    "contains peanuts": "peanuts",
    "contains pork": "pork",
    "contains alcohol": "alcohol",
}


def _meal(div_id: str) -> Optional[str]:
    if div_id.lower() in {"lunch_dinner", "lunchdinner"}:
        return "All Day"
    return canonical_meal(div_id.replace("_", " "))


class UcsdAdapter(Adapter):
    max_days = 7

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._venues: Optional[List[Tuple[str, str]]] = None
        self._names: Dict[Tuple[str, str], str] = {}

    def venues(self) -> List[Tuple[str, str]]:
        """[(locId, locDetID)] in page order, de-duplicated."""
        if self._venues is None:
            soup = BeautifulSoup(self.fetcher.get(LIST_URL), "lxml")
            out: List[Tuple[str, str]] = []
            for a in soup.select("a[href*='Venue_V3']"):
                q = dict(parse_qsl(urlsplit(a["href"]).query))
                key = (q.get("locId", ""), q.get("locDetID", ""))
                if all(key) and key not in out:
                    out.append(key)
            if not out:
                raise AdapterError("no Venue_V3 links on the Restaurants page")
            self._venues = out[: self.options.get("max_venues")]  # max_venues: test fixtures only
        return self._venues

    def fetch_day(self, day: date) -> DayMenu:
        day_num = (day - self.today).days
        if not 0 <= day_num < self.max_days:
            raise AdapterError(f"{day} is outside the 7-day window starting {self.today}")
        locations = [self._venue(loc_id, det_id, day, day_num) for loc_id, det_id in self.venues()]
        return self.make_day(day, f"{LIST_URL}?day={day}", locations)

    def _venue(self, loc_id: str, det_id: str, day: date, day_num: int) -> Location:
        html = self.fetcher.get(VENUE_URL, params={"locId": loc_id, "locDetID": det_id, "dayNum": str(day_num)})
        soup = BeautifulSoup(html, "lxml")
        title = clean_text(soup.title.get_text() if soup.title else "")
        name = re.split(r"\s+Food Menu|\s*\|", title)[0].strip()
        if not name:
            raise AdapterError(f"venue {loc_id}/{det_id}: no <title>")

        shown = soup.select_one("h2.datenow")
        expected = f"{day:%B} {day:%d} {day.year}"  # "October 03 2026"
        if shown is None or expected not in clean_text(shown.get_text()):
            raise AdapterError(f"{name}: date header {shown and clean_text(shown.get_text())!r} != {expected!r}")

        meals: List[Meal] = []
        for cat in soup.select("div.meal-category"):
            meal_name = _meal(cat.get("id", ""))
            if meal_name is None:
                raise AdapterError(f"{name}: unknown meal category {cat.get('id')!r}")
            stations = self._stations(cat)
            if stations:
                existing = next((m for m in meals if m.name == meal_name), None)
                if existing:
                    existing.stations += stations
                else:
                    meals.append(Meal(name=meal_name, stations=stations))
        return Location(id=slugify(name), name=name, type="dining_hall", meals=meals)

    @staticmethod
    def _stations(cat: Tag) -> List[Station]:
        stations: Dict[str, Station] = {}
        for section in cat.select("div.menu-category-section"):
            venue = clean_text(section.find("h3").get_text()) if section.find("h3") else "Menu"
            sub = None
            for el in section.find_all(["h4", "a"]):
                if el.name == "h4":
                    sub = clean_text(el.get_text())
                    continue
                if "sublocsitem" not in (el.get("class") or []):
                    continue
                st_name = f"{venue}: {sub}" if sub and sub.lower() != venue.lower() else venue
                station = stations.setdefault(st_name, Station(name=st_name))
                item_name = clean_text(el.get_text())
                if any(it.name == item_name for it in station.items):
                    continue  # desktop + mobile duplicate
                row = el.find_parent(class_=re.compile(r"menU-item-row|row", re.I)) or el.parent
                titles = {(img.get("title") or "").strip().lower() for img in row.select("img[title]")}
                cals = row.select_one("span.cals")
                cal_m = re.search(r"\d+", cals.get_text()) if cals else None
                station.items.append(
                    Item(
                        name=item_name,
                        tags=sorted({ICON_TAGS[t] for t in titles if t in ICON_TAGS}),
                        allergens=sorted({ICON_ALLERGENS[t] for t in titles if t in ICON_ALLERGENS}),
                        calories=int(cal_m.group()) if cal_m else None,
                    )
                )
        return [s for s in stations.values() if s.items]
