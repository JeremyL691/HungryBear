# hungrybear/adapters/davis.py
"""UC Davis - housing.ucdavis.edu/dining/dining-commons/<dc>/

Each dining commons page embeds the *current* week (Sunday-Saturday) in a modal:
  div.tab-pane#sunday..#saturday > article.dcMenuListing
    h2.stickyMealHeader                         meal
    div.col-* > h3                              zone ("Red Zone") = station
      div.panel[class~=isVegan|isVegetarian|isHalal|filterDairy|...]
        .panel-title                            dish
        p "Calories: 230.65"
  .zone-hours (h3 "Monday–Friday" / "Saturday–Sunday")   per-zone hours for each meal

Days outside the current week are NotPublished.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, Comment, Tag

from ..models import DayMenu, Item, Location, Meal, Station, canonical_meal, clean_text
from .base import Adapter, AdapterError, NotPublished

BASE = "https://housing.ucdavis.edu/dining/dining-commons/{dc}/"
DEFAULT_DCS = {"segundo": "Segundo DC", "tercero": "Tercero DC", "cuarto": "Cuarto DC"}

CLASS_TAGS = {"isVegan": "vegan", "isVegetarian": "vegetarian", "isHalal": "halal"}
CLASS_ALLERGENS = {
    "filterDairy": ["milk"],
    "filterEgg": ["egg"],
    "filterSoy": ["soy"],
    "filterWheatGluten": ["wheat", "gluten"],
    "filterAlcohol": ["alcohol"],
    "filterSesame": ["sesame"],
    "filterFish": ["fish"],
    "filterTreeNuts": ["tree_nuts"],
    "filterShellfish": ["shellfish"],
    "filterPeanuts": ["peanuts"],
    "filterCoconut": ["coconut"],
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _to_minutes(h: int, m: int, ap: str) -> int:
    h = h % 12 + (12 if ap == "pm" else 0)
    return h * 60 + m


def parse_zone_range(text: str) -> Optional[Tuple[int, int]]:
    """'7–10:30 AM' / '11 AM–5 PM' / '11:30 AM–4 PM' -> minutes since midnight. None for 'Closed'."""
    parts = re.split(r"\s*[–—-]\s*", text.strip())
    if len(parts) != 2:
        return None
    parsed = []
    for p in parts:
        m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", p.strip(), re.I)
        if not m:
            return None
        ap = (m.group(3) or "").replace(".", "").lower() or None
        parsed.append((int(m.group(1)), int(m.group(2) or 0), ap))
    (h1, m1, ap1), (h2, m2, ap2) = parsed
    if ap2 is None:
        return None
    end = _to_minutes(h2, m2, ap2)
    start = _to_minutes(h1, m1, ap1 or ap2)
    if ap1 is None and start > end:  # "11–1 PM" -> 11 AM
        start = _to_minutes(h1, m1, "am")
    return start, end


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class DavisAdapter(Adapter):
    max_days = 7

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._pages: Dict[str, BeautifulSoup] = {}

    def page(self, dc: str) -> BeautifulSoup:
        if dc not in self._pages:
            soup = BeautifulSoup(self.fetcher.get(BASE.format(dc=dc)), "lxml")
            for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
                c.extract()  # the page ships a commented-out sample menu
            self._pages[dc] = soup
        return self._pages[dc]

    def fetch_day(self, day: date) -> DayMenu:
        # The page shows the Sunday-Saturday week containing "today".
        week_start = self.today - timedelta(days=(self.today.weekday() + 1) % 7)
        if not week_start <= day < week_start + timedelta(days=7):
            raise NotPublished(f"Davis only publishes the current week ({week_start} onward)")

        dcs: Dict[str, str] = self.options.get("dining_commons", DEFAULT_DCS)
        locations = [self._dc(dc, name, day) for dc, name in dcs.items()]
        return self.make_day(day, BASE.format(dc="tercero"), locations)

    def _dc(self, dc: str, name: str, day: date) -> Location:
        soup = self.page(dc)
        pane = soup.select_one(f"div.tab-pane#{WEEKDAYS[day.weekday()]} article.dcMenuListing")
        if pane is None:
            raise AdapterError(f"{dc}: weekly menu tab for {WEEKDAYS[day.weekday()]} not found")
        hours = self._hours(soup, weekend=day.weekday() >= 5)

        meals: List[Meal] = []
        for header in pane.select("h2.stickyMealHeader"):
            meal_name = canonical_meal(header.get_text())
            if meal_name is None:
                raise AdapterError(f"{dc}: unknown meal {header.get_text()!r}")
            block = header.parent
            stations: List[Station] = []
            for zone in block.select("div.row > div"):
                h3 = zone.find("h3")
                station = Station(name=clean_text(h3.get_text()) if h3 else "Menu")
                for panel in zone.select("div.panel"):
                    station.items.append(self._item(panel))
                if station.items:
                    stations.append(station)
            if stations:
                start, end = hours.get(meal_name, (None, None))
                meals.append(Meal(name=meal_name, start=start, end=end, stations=stations))
        return Location(id=dc, name=name, type="dining_hall", meals=meals)

    @staticmethod
    def _item(panel: Tag) -> Item:
        title = panel.select_one(".panel-title")
        classes = panel.get("class", [])
        cal = None
        for p in panel.select("p"):
            m = re.match(r"\s*Calories\s*:\s*([\d.]+)", p.get_text())
            if m:
                cal = round(float(m.group(1)))
        return Item(
            name=clean_text(title.get_text() if title else panel.get_text()),
            tags=sorted({CLASS_TAGS[c] for c in classes if c in CLASS_TAGS}),
            allergens=sorted({a for c in classes for a in CLASS_ALLERGENS.get(c, [])}),
            calories=cal,
        )

    @staticmethod
    def _hours(soup: BeautifulSoup, weekend: bool) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
        """{meal: (earliest zone open, latest zone close)} for weekday or weekend."""
        box = soup.select_one(".zone-hours")
        if box is None:
            return {}
        want = "saturday" if weekend else "monday"
        out: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        for h3 in box.find_all("h3"):
            if want not in h3.get_text().lower():
                continue
            row = h3.find_next_sibling("div")
            for col in row.find_all("div", recursive=False) if row else []:
                lines = [clean_text(t) for t in col.stripped_strings]
                if not lines:
                    continue
                meal = canonical_meal(lines[0])
                ranges = [r for r in (parse_zone_range(t.lstrip(": ")) for t in lines[1:]) if r]
                if meal and ranges:
                    out[meal] = (_hhmm(min(r[0] for r in ranges)), _hhmm(max(r[1] for r in ranges)))
        return out
