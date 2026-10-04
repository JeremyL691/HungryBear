# hungrybear/adapters/berkeley.py
"""UC Berkeley - dining.berkeley.edu (WordPress "cal-dining" plugin).

The /menus/ page renders today's menus server-side and loads other days through
admin-ajax.php?action=cald_filter_xml&date=YYYYMMDD, which returns the same markup:

  li.location-name
    span.cafe-title                  "Café 3"
    div.times > span                 one time range per meal period, same order
    span.serve-date                  "Mon, Oct 5"
    li.preiod-name > span            "Fall - Breakfast (ends at 10:30)"   (sic: "preiod")
      div.cat-name > span            station
        li.recip[class=allergens…]   dish; class list carries diet tags + allergens
"""

from __future__ import annotations

import re
from datetime import date
from typing import Dict, List, Optional

from bs4 import BeautifulSoup, Tag

from ..models import DayMenu, Item, Location, Meal, Station, canonical_meal, clean_text, parse_time_range, slugify
from .base import Adapter, AdapterError

MENUS_URL = "https://dining.berkeley.edu/menus/"
AJAX_URL = "https://dining.berkeley.edu/wp-admin/admin-ajax.php"

CLASS_TAGS = {
    "vegan-option": "vegan",
    "vegetarian-option": "vegetarian",
    "halal": "halal",
    "kosher": "kosher",
}
CLASS_ALLERGENS = {
    "milk": "milk",
    "egg": "egg",
    "fish": "fish",
    "shellfish": "shellfish",
    "tree-nuts": "tree_nuts",
    "wheat": "wheat",
    "peanuts": "peanuts",
    "soybeans": "soy",
    "sesame": "sesame",
    "gluten": "gluten",
    "pork": "pork",
    "alcohol": "alcohol",
}


def location_id(name: str) -> str:
    return slugify(re.sub(r"^the\s+", "", name, flags=re.I))


class BerkeleyAdapter(Adapter):
    max_days = 7

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._all_locations: Optional[List[str]] = None

    # ---- location discovery: the filter dropdown lists every location, open or not ----

    def all_location_names(self) -> List[str]:
        if self._all_locations is None:
            soup = BeautifulSoup(self.fetcher.get(MENUS_URL), "lxml")
            select = soup.select_one("select#location")
            if select is None:
                raise AdapterError("select#location not found on /menus/ (page layout changed?)")
            names = [clean_text(o.get_text()) for o in select.select("option") if o.get("value")]
            if not names:
                raise AdapterError("select#location has no options")
            self._all_locations = names
        return self._all_locations

    # ---- day menu ----

    def fetch_day(self, day: date) -> DayMenu:
        html = self.fetcher.post(
            AJAX_URL,
            data={"action": "cald_filter_xml", "location": "", "mealperiod": "", "date": day.strftime("%Y%m%d")},
        )
        soup = BeautifulSoup(html, "lxml")
        wrap = soup.select_one("ul.cafe-location")
        if wrap is None:
            raise AdapterError("ul.cafe-location missing from cald_filter_xml response")

        parsed: Dict[str, Location] = {}
        for li in wrap.select("li.location-name"):
            loc = self._parse_location(li, day)
            parsed[loc.id] = loc

        # Every location from the dropdown appears in the output; ones missing that day are closed.
        locations: List[Location] = []
        for name in self.all_location_names():
            lid = location_id(name)
            locations.append(parsed.pop(lid, None) or Location(id=lid, name=name, status="closed"))
        # Anything on the menu page that the dropdown didn't list (shouldn't happen, keep it anyway).
        locations.extend(parsed.values())

        return self.make_day(day, f"{MENUS_URL}?date={day:%Y%m%d}", locations)

    def _parse_location(self, li: Tag, day: date) -> Location:
        title = li.select_one(".cafe-title")
        if title is None:
            raise AdapterError("li.location-name without .cafe-title")
        name = clean_text(title.get_text())

        serve = li.select_one(".serve-date")
        if serve is not None:
            expected = f"{day:%a}, {day:%b} {day.day}"
            if clean_text(serve.get_text()) != expected:
                raise AdapterError(f"{name}: serve-date {serve.get_text()!r} != requested {expected!r}")

        times = [clean_text(t.get_text()) for t in li.select(".times span")]
        periods = li.select("li.preiod-name")
        meals: List[Meal] = []
        for i, period in enumerate(periods):
            label_el = period.find("span")
            label = clean_text(label_el.get_text(" ")) if label_el else ""
            meal_name = canonical_meal(label)
            if meal_name is None:
                raise AdapterError(f"{name}: unknown meal period {label!r}")
            note_m = re.search(r"\((.*?)\)", label)
            # One time range per period, in the same order. Fall back to the only range if there is one.
            rng = times[i] if len(times) == len(periods) else (times[0] if len(times) == 1 else "")
            start, end = parse_time_range(rng)
            meal = Meal(name=meal_name, start=start, end=end, note=note_m.group(1) if note_m else None)
            meal.stations = self._parse_stations(period)
            # Markets ("All Day") list a single store description instead of dishes.
            if meal.name == "All Day" and meal.item_count == 1 and len(meal.stations[0].items[0].name) > 60:
                meal.note = meal.stations[0].items[0].name
                meal.stations = []
            meals.append(meal)

        return Location(id=location_id(name), name=name, status="open" if meals else "closed", meals=meals)

    def _parse_stations(self, period: Tag) -> List[Station]:
        stations: List[Station] = []
        for cat in period.select("div.cat-name"):
            head = cat.find("span", recursive=False)
            station = Station(name=clean_text(head.get_text()) if head else "Menu")
            for rec in cat.select("li.recip"):
                name_el = rec.find("span", recursive=False)
                if name_el is None:
                    continue
                classes = rec.get("class", [])
                station.items.append(
                    Item(
                        name=clean_text(name_el.get_text()),
                        tags=sorted({CLASS_TAGS[c] for c in classes if c in CLASS_TAGS}),
                        allergens=sorted({CLASS_ALLERGENS[c] for c in classes if c in CLASS_ALLERGENS}),
                    )
                )
            if station.items:
                stations.append(station)
        return stations
