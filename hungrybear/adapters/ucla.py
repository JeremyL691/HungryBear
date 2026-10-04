# hungrybear/adapters/ucla.py
"""UCLA - dining.ucla.edu (WordPress + "jamix" menu plugin).

Menus:  /menus-at-a-glance/?date=YYYY-MM-DD  (residential halls only)
  div.at-a-glance-menu#breakfastmenu|lunchmenu|dinnermenu
    div.at-a-glance-menu__dining-location > h3          hall
      div.at-a-glance-menu__meal-station > h4           station
        li > a (dish) + img.meal-station__allergen-icon[title]
Hours:  /hours/  - one table.dining-hours-table per day, same order as select#hours-date-select.
        Covers quick-service spots too (Rendezvous, The Study, ...), which have no published menu.

The two pages name some halls differently; `options.hours_aliases` maps menu name -> hours name.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from ..models import DayMenu, Item, Location, Meal, Station, canonical_meal, clean_text, parse_time_range, slugify
from .base import Adapter, AdapterError

BASE = "https://dining.ucla.edu"
MENU_URL = f"{BASE}/menus-at-a-glance/"
HOURS_URL = f"{BASE}/hours/"

ICON_TAGS = {"vegan": "vegan", "vegetarian": "vegetarian", "halal": "halal"}
ICON_ALLERGENS = {
    "gluten": "gluten",
    "wheat": "wheat",
    "dairy": "milk",
    "soy": "soy",
    "eggs": "egg",
    "sesame": "sesame",
    "alcohol": "alcohol",
    "tree-nuts": "tree_nuts",
    "peanut": "peanuts",
    "fish": "fish",
    "crustacean-shellfish": "shellfish",
    "shellfish": "shellfish",
}


def _meal_name(label: str) -> Optional[str]:
    if re.search(r"extended|late", label, re.I):
        return "Late Night"
    return canonical_meal(label)


class UclaAdapter(Adapter):
    max_days = 7

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._hours: Optional[Dict[date, Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]]]] = None

    # ---- hours: {date: {location name: {meal: (start, end)}}} ----

    def hours(self) -> Dict[date, Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]]]:
        if self._hours is not None:
            return self._hours
        soup = BeautifulSoup(self.fetcher.get(HOURS_URL), "lxml")
        select = soup.select_one("select#hours-date-select")
        tables = soup.select("table.dining-hours-table")
        if select is None or not tables:
            raise AdapterError("hours page: date select or hours tables not found")
        days: List[date] = []
        for opt in select.select("option"):
            text = clean_text(opt.get_text()).split("–")[-1].strip()  # "Today – Saturday, October 3, 2026"
            days.append(datetime.strptime(text, "%A, %B %d, %Y").date())
        if len(days) != len(tables):
            raise AdapterError(f"hours page: {len(days)} dates but {len(tables)} tables")

        out: Dict[date, Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]]] = {}
        for day, table in zip(days, tables, strict=True):
            per_loc: Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]] = {}
            for row in table.select("tbody tr"):
                cells = row.find_all("td")
                if not cells:
                    continue
                name = clean_text(cells[0].get_text())
                meals = {}
                for cell in cells[1:]:
                    meal = _meal_name(cell.get("data-label", ""))
                    text = clean_text(cell.get_text())
                    if meal and text.lower() != "closed":
                        meals[meal] = parse_time_range(text)
                per_loc[name] = meals
            out[day] = per_loc
        self._hours = out
        return out

    # ---- menu ----

    def fetch_day(self, day: date) -> DayMenu:
        url = f"{MENU_URL}?date={day.isoformat()}"
        soup = BeautifulSoup(self.fetcher.get(MENU_URL, params={"date": day.isoformat()}), "lxml")
        title = soup.select_one("h1.at-a-glance-page-title")
        if title is None:
            raise AdapterError("h1.at-a-glance-page-title not found")
        expected = f"{day:%B} {day.day}, {day.year}"
        if expected not in title.get_text():
            raise AdapterError(f"page title {title.get_text()!r} does not mention {expected!r}")

        menus: Dict[str, Dict[str, List[Station]]] = {}  # hall -> meal -> stations
        sections = [s for s in soup.select("div.at-a-glance-menu") if s.get("id")]
        if not sections:
            # Page rendered (title matched) but nothing is being served; validate.py decides if that's plausible.
            return self.make_day(day, url, [], status="closed", reason="no menu sections on page")
        for sec in sections:
            meal = _meal_name(sec.get("id", "").replace("menu", "")) or _meal_name(sec.find("h2").get_text())
            if meal is None:
                raise AdapterError(f"unknown meal section {sec.get('id')!r}")
            for hall in sec.select("div.at-a-glance-menu__dining-location"):
                name = clean_text(hall.find("h3").get_text())
                stations = []
                for st in hall.select("div.at-a-glance-menu__meal-station"):
                    head = st.find("h4")
                    station = Station(name=clean_text(head.get_text()) if head else "Menu")
                    for li in st.select("li"):
                        a = li.find("a")
                        if a is None:
                            continue
                        icons = {(img.get("title") or "").strip().lower() for img in li.select("img")}
                        station.items.append(
                            Item(
                                name=clean_text(a.get_text()),
                                tags=sorted({ICON_TAGS[i] for i in icons if i in ICON_TAGS}),
                                allergens=sorted({ICON_ALLERGENS[i] for i in icons if i in ICON_ALLERGENS}),
                            )
                        )
                    if station.items:
                        stations.append(station)
                menus.setdefault(name, {})[meal] = stations

        hours_today = self.hours().get(day, {})
        aliases: Dict[str, str] = self.options.get("hours_aliases", {})
        locations: List[Location] = []
        seen_hours = set()
        for name, by_meal in menus.items():
            hours_name = aliases.get(name, name)
            seen_hours.add(hours_name)
            loc_hours = hours_today.get(hours_name, {})
            meals = []
            for meal_name in {*by_meal, *loc_hours}:
                stations = by_meal.get(meal_name, [])
                if not stations and meal_name not in loc_hours:
                    continue  # listed with no food and no hours -> not served
                start, end = loc_hours.get(meal_name, (None, None))
                meals.append(Meal(name=meal_name, start=start, end=end, stations=stations))
            locations.append(Location(id=slugify(name), name=name, type="dining_hall", meals=meals))

        # Quick-service locations: hours only, no published menu.
        for name, loc_hours in hours_today.items():
            if name in seen_hours:
                continue
            meals = [Meal(name=m, start=s, end=e) for m, (s, e) in loc_hours.items()]
            locations.append(Location(id=slugify(name), name=name, type="restaurant", meals=meals))

        return self.make_day(day, url, locations)
