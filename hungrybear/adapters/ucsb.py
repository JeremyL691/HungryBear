# hungrybear/adapters/ucsb.py
"""UC Santa Barbara - apps.dining.ucsb.edu/menu/day

  ?d=YYYY-MM-DD&dc=<hall>...&m=<meal>...   (repeat dc/m to select several; options come from select#dc / select#m)
  #menu-row > div (one column per hall)
    h4                                     hall name
    div.panel > .panel-heading h5          "Dinner <small>5:00 PM - 8:30 PM</small>"
      dl > dt                              station
         dd                                dish, with "(vgn)" / "(v)" / "(w/nuts)" suffixes

There is also an official API (developer.ucsb.edu, needs a key) we could fall back to.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from ..models import DayMenu, Item, Location, Meal, Station, canonical_meal, clean_text, parse_time_range, slugify
from .base import Adapter, AdapterError

URL = "https://apps.dining.ucsb.edu/menu/day"
SUFFIX = re.compile(r"\s*\((vgn|v|w/nuts)\)\s*", re.I)


class UcsbAdapter(Adapter):
    max_days = 7

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._filters: Optional[Tuple[Dict[str, str], List[str]]] = None

    def filters(self) -> Tuple[Dict[str, str], List[str]]:
        """({hall value: hall name}, [meal values]) from the unfiltered landing page."""
        if self._filters is None:
            landing = BeautifulSoup(self.fetcher.get(URL), "lxml")
            halls = {o["value"]: clean_text(o.get_text()) for o in landing.select("select#dc option") if o.get("value")}
            meals = [o["value"] for o in landing.select("select#m option") if o.get("value")]
            if not halls or not meals:
                raise AdapterError("select#dc / select#m filters not found")
            self._filters = (halls, meals)
        return self._filters

    def fetch_day(self, day: date) -> DayMenu:
        halls, meals = self.filters()
        params = [("d", day.isoformat())] + [("dc", h) for h in halls] + [("m", m) for m in meals]
        soup = BeautifulSoup(self.fetcher.get(URL, params=params), "lxml")
        heading = soup.find(string=re.compile(r"Menu\s*$"))
        stamp = f"{day.month}/{day.day}"
        if heading is None or stamp not in heading.find_parent().get_text():
            raise AdapterError(f"day heading for {stamp} not found")

        row = soup.select_one("#menu-row")
        if row is None:
            raise AdapterError("#menu-row not found")

        locations: List[Location] = []
        for col in row.find_all("div", recursive=False):
            h4 = col.find("h4")
            if h4 is None:
                continue
            name = clean_text(h4.get_text())
            loc = Location(id=slugify(name), name=name, type="dining_hall")
            for panel in col.select("div.panel"):
                h5 = panel.select_one(".panel-heading h5")
                if h5 is None:
                    continue
                small = h5.find("small")
                hours = clean_text(small.get_text()) if small else ""
                label = clean_text(h5.get_text().replace(hours, ""))
                meal_name = canonical_meal(label)
                if meal_name is None:
                    raise AdapterError(f"{name}: unknown meal {label!r}")
                start, end = parse_time_range(hours)
                meal = Meal(name=meal_name, start=start, end=end)
                for dl in panel.select("dl"):
                    dt = dl.find("dt")
                    station = Station(name=clean_text(dt.get_text()) if dt else "Menu")
                    for dd in dl.find_all("dd"):
                        raw = clean_text(dd.get_text())
                        marks = {m.lower() for m in SUFFIX.findall(raw)}
                        tags = ["vegan"] if "vgn" in marks else (["vegetarian"] if "v" in marks else [])
                        allergens = ["tree_nuts"] if "w/nuts" in marks else []
                        station.items.append(Item(name=SUFFIX.sub(" ", raw).strip(), tags=tags, allergens=allergens))
                    if station.items:
                        meal.stations.append(station)
                loc.meals.append(meal)
            locations.append(loc)

        # Halls with no service that day get no column; list them as closed.
        listed = {loc.id for loc in locations}
        locations += [
            Location(id=slugify(n), name=n, status="closed") for n in halls.values() if slugify(n) not in listed
        ]
        return self.make_day(day, f"{URL}?d={day}", locations)
