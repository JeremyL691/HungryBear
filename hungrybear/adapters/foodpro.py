# hungrybear/adapters/foodpro.py
"""FoodPro (Aurora Information Systems) - used by UC Santa Cruz and UC Riverside.

  {index}                               location links: shortmenu.aspx?...&locationNum=NN&locationName=...
  shortmenu.aspx?...&dtdate=M/D/YYYY    one location, one day
    div.shortmenumeals                  meal ("Breakfast")
    div.shortmenucats                   station ("-- Grill --")
    div.shortmenurecipes                dish; icon <img>s in the same <tr> (LegendImages/*, AllergenImages/*)

Options (campuses.yaml):
  base:   site root, e.g. https://nutrition.sa.ucsc.edu/
  index:  location list page, relative to base
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from ..models import DayMenu, Item, Location, Meal, Station, canonical_meal, clean_text, parse_time_range, slugify
from .base import Adapter, AdapterError

ICON_TAGS = {
    "veg": "vegetarian",
    "veggie": "vegetarian",
    "vegetarian": "vegetarian",
    "vgn": "vegan",
    "vegan": "vegan",
    "gf": "gluten_free",
    "halal": "halal",
    "kosher": "kosher",
}
ICON_ALLERGENS = {
    "milk": "milk",
    "eggs": "egg",
    "egg": "egg",
    "gluten": "gluten",
    "wheat": "wheat",
    "soy": "soy",
    "soybeans": "soy",
    "sesame": "sesame",
    "pork": "pork",
    "alcohol": "alcohol",
    "fish": "fish",
    "shellfish": "shellfish",
    "crustacean_shellfish": "shellfish",
    "peanuts": "peanuts",
    "peanut": "peanuts",
    "tree_nuts": "tree_nuts",
    "treenut": "tree_nuts",
    "coconut": "coconut",
}
NAME_NOTE = re.compile(r"\s+-\s+(open|opens|closed|reopen).*$", re.I)


def meal_from_label(label: str) -> Tuple[str, Optional[str]]:
    """FoodPro sites use free-form labels for cafes. Returns (canonical meal, note to keep or None)."""
    meal = canonical_meal(label)
    if meal:
        return meal, None
    low = label.lower().strip()
    if low in {"menu", "all", "anytime", "daily"}:
        return "All Day", None
    if re.search(r"after\s*(10|11|noon)", low):
        return "Lunch", label
    return "All Day", label  # e.g. "Continuous Service 2:30PM-4:30PM"


def location_type(name: str) -> str:
    low = name.lower()
    if "dining hall" in low or "restaurant" in low or low in {"glasgow", "lothian"}:
        return "dining_hall"
    if "market" in low:
        return "market"
    return "cafe"


class FoodProAdapter(Adapter):
    max_days = 7

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.base: str = self.options["base"]
        self._locations: Optional[List[Tuple[str, str, Dict[str, str]]]] = None

    def locations(self) -> List[Tuple[str, str, Dict[str, str]]]:
        """[(id, display name, shortmenu query params)] from the index page."""
        if self._locations is None:
            index_url = urljoin(self.base, self.options.get("index", ""))
            soup = BeautifulSoup(self.fetcher.get(index_url), "lxml")
            out = []
            for a in soup.select("a[href*='shortmenu.aspx']"):
                params = dict(parse_qsl(urlsplit(a["href"]).query))
                if "locationNum" not in params:
                    continue
                name = NAME_NOTE.sub("", clean_text(a.get_text()) or params.get("locationName", ""))
                out.append((slugify(name), name, params))
            if not out:
                raise AdapterError("no shortmenu.aspx location links on the index page")
            self._locations = out
        return self._locations

    def fetch_day(self, day: date) -> DayMenu:
        dtdate = f"{day.month}/{day.day}/{day.year}"
        url = urljoin(self.base, "shortmenu.aspx")
        locations: List[Location] = []
        for loc_id, name, params in self.locations():
            q = {**params, "naFlag": "1", "myaction": "read", "dtdate": dtdate}
            soup = BeautifulSoup(self.fetcher.get(url, params=q), "lxml")
            loc = Location(id=loc_id, name=name, type=location_type(name))
            loc.meals = self._parse(soup, name, day)
            locations.append(loc)
        return self.make_day(day, f"{url}?dtdate={dtdate}", locations)

    def _parse(self, soup: BeautifulSoup, name: str, day: date) -> List[Meal]:
        # Pages always carry the date selector; if it is gone this isn't a FoodPro menu page any more.
        if soup.find(string=re.compile(r"Select a date", re.I)) is None and not soup.select(".shortmenumeals"):
            raise AdapterError(f"{name}: not a FoodPro menu page (no date selector, no meals)")
        header = soup.find(string=re.compile(r"Menus? for", re.I))
        if header is not None:
            full = f"{day:%B} {day.day}"
            if full not in header.find_parent().get_text():
                raise AdapterError(f"{name}: page header {clean_text(header)!r} is not for {full}")

        meals: List[Meal] = []
        meal: Optional[Meal] = None
        station: Optional[Station] = None
        for el in soup.find_all(class_=["shortmenumeals", "shortmenucats", "shortmenurecipes"]):
            classes = el.get("class", [])
            text = clean_text(el.get_text())
            if "shortmenumeals" in classes:
                meal_name, note = meal_from_label(text)
                meal = next((m for m in meals if m.name == meal_name), None)
                if meal is None:
                    start, end = parse_time_range(note or "")
                    meal = Meal(name=meal_name, start=start, end=end, note=note)
                    meals.append(meal)
                station = None
            elif "shortmenucats" in classes:
                if meal is None:
                    raise AdapterError(f"{name}: station before any meal")
                station = Station(name=text.strip("- ").strip() or "Menu")
                meal.stations.append(station)
            else:
                if meal is None:
                    raise AdapterError(f"{name}: dish before any meal")
                if station is None:
                    station = Station(name="Menu")
                    meal.stations.append(station)
                station.items.append(self._item(el, text))
        for m in meals:
            m.stations = [s for s in m.stations if s.items]
        return [m for m in meals if m.stations]

    @staticmethod
    def _item(el: Tag, text: str) -> Item:
        row = el.find_parent("tr")
        icons = set()
        for img in row.find_all("img") if row else []:
            stem = PurePosixPath(urlsplit(img.get("src", "")).path).stem.lower().strip("_")
            icons.add(stem)
        return Item(
            name=text,
            tags=sorted({ICON_TAGS[i] for i in icons if i in ICON_TAGS}),
            allergens=sorted({ICON_ALLERGENS[i] for i in icons if i in ICON_ALLERGENS}),
        )
