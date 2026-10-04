# hungrybear/adapters/bigzpoon.py
"""bigZpoon "Eagle" menu widget (UC Merced: uc-merced-the-pavilion.widget.eagle.bigzpoon.com).

JSON API used by the widget's own frontend; it wants the same app headers the widget sends
(x-comp-id = company id, location-id, and a client-generated device-id).

  GET /locations/menugroups?isPreview=true&locationId=L      menu groups = weekdays ("MONDAY", ...)
  GET /menucategories?initialCall=menuGroup&locationId=L&menuGroupIds=G   categories ("Lunch", "FoG Breakfast")
  GET /menuitems?categoryId=C&locationId=L&menuGroupId=G&userPreferences={...}   dishes

Menus are a weekly cycle keyed by weekday, published for the current Sunday-Saturday week.

Options: company_id, locations: [{id, name}]
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from ..models import DayMenu, Item, Location, Meal, Station, clean_text, slugify
from .base import Adapter, AdapterError, NotPublished

API = "https://widget.api.eagle.bigzpoon.com"
WIDGET = "https://uc-merced-the-pavilion.widget.eagle.bigzpoon.com"
NO_PREFS = json.dumps(
    {"allergies": [], "lifestyleChoices": [], "medicalGoals": [], "preferenceApplyStatus": False}, separators=(",", ":")
)
SKIP_CATEGORIES = {"schedule", "help", "recipes"}
WEEKDAY_NAMES = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]


def category_meals(name: str) -> Tuple[List[str], str]:
    """'FoG Lunch & Dinner' -> (['Lunch', 'Dinner'], 'FoG'); 'Bakery' -> (['All Day'], 'Bakery')."""
    low = name.lower()
    meals = [m for m in ("Breakfast", "Brunch", "Lunch", "Dinner") if m.lower() in low]
    if "late night" in low:
        meals.append("Late Night")
    if not meals:
        return ["All Day"], clean_text(name).title()
    prefix = clean_text(re.sub(r"(?i)breakfast|brunch|lunch|dinner|late night|&|and", " ", name))
    return meals, prefix or "Main"


class BigzpoonAdapter(Adapter):
    max_days = 7

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.company_id: str = self.options["company_id"]
        self.device_id = str(uuid.uuid4())
        self._groups: Dict[str, Dict[str, str]] = {}

    def api(self, path: str, location_id: str, **params) -> object:
        headers = {
            "x-comp-id": self.company_id,
            "location-id": location_id,
            "device-id": self.device_id,
            "Origin": WIDGET,
            "Accept": "application/json",
        }
        data = self.fetcher.get_json(f"{API}{path}", params=params, headers=headers)
        if not isinstance(data, dict) or data.get("code") != 200:
            raise AdapterError(f"{path}: {str(data)[:200]}")
        return data["data"]

    def groups(self, location_id: str) -> Dict[str, str]:
        """{WEEKDAY: menu group id}."""
        if location_id not in self._groups:
            data = self.api("/locations/menugroups", location_id, isPreview="true", locationId=location_id)
            groups = {g["name"].strip().upper(): g["_id"] for g in data.get("menuGroups", [])}
            if not any(name in groups for name in WEEKDAY_NAMES):
                raise AdapterError(f"location {location_id}: no weekday menu groups in {list(groups)}")
            self._groups[location_id] = groups
        return self._groups[location_id]

    def fetch_day(self, day: date) -> DayMenu:
        week_start = self.today - timedelta(days=(self.today.weekday() + 1) % 7)
        if not week_start <= day < week_start + timedelta(days=7):
            raise NotPublished(f"weekly cycle menu only covers the current week ({week_start} onward)")
        locations = [self._location(spec, day) for spec in self.options["locations"]]
        return self.make_day(day, WIDGET, locations)

    def _location(self, spec: dict, day: date) -> Location:
        loc_id, name = spec["id"], spec["name"]
        loc = Location(id=slugify(name), name=name, type="dining_hall")
        group_id = self.groups(loc_id).get(WEEKDAY_NAMES[day.weekday()])
        if group_id is None:
            loc.status = "closed"  # e.g. YWDC has no weekend groups
            return loc

        categories = self.api(
            "/menucategories", loc_id, initialCall="menuGroup", locationId=loc_id, menuGroupIds=group_id
        )
        meals: Dict[str, Meal] = {}
        for cat in categories or []:
            cat_name = clean_text(cat.get("name", ""))
            if cat_name.lower() in SKIP_CATEGORIES or cat.get("status", "Active") != "Active":
                continue
            items = self.api(
                "/menuitems",
                loc_id,
                categoryId=cat["_id"],
                locationId=loc_id,
                menuGroupId=group_id,
                userPreferences=NO_PREFS,
            )
            items = items if isinstance(items, list) else (items or {}).get("menuItems", [])
            parsed = [self._item(i) for i in items if i.get("status", "Active") == "Active" and i.get("name")]
            if not parsed:
                continue
            meal_names, station_name = category_meals(cat_name)
            for meal_name in meal_names:
                meal = meals.setdefault(meal_name, Meal(name=meal_name))
                meal.stations.append(Station(name=station_name, items=list(parsed)))
        loc.meals = list(meals.values())
        return loc

    @staticmethod
    def _item(raw: dict) -> Item:
        cal: Optional[int] = None
        m = re.search(r"(\d+)\s*Cal", raw.get("caloriesInfo") or "", re.I)
        if m:
            cal = int(m.group(1))
        desc = clean_text(raw.get("description") or "") or None
        return Item(name=clean_text(raw["name"]).rstrip("!").strip() or raw["name"], description=desc, calories=cal)
