# hungrybear/adapters/mydininghub.py
"""Aramark "MyDiningHub" / Elevate DXP (UC Irvine: uci.mydininghub.com).

The site is a JS app over a public GraphQL mesh. We send the same headers the website's
own frontend sends (including its public client key) - nothing here is a secret.

  getLocations(campusUrlKey)                     locations (+ hasActiveMenus)
  getLocation(campusUrlKey, locationUrlKey)      stations (children), meal periods, hours schedule
  getLocationRecipes(..., date, mealPeriod)      station -> SKUs, and product details per SKU
  Commerce_storeConfig                           code -> label for allergens and diet preferences

Options: store (e.g. "ch_uci_en"), campus_url_key (default "campus").
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Dict, List, Optional, Tuple

from ..models import DayMenu, Item, Location, Meal, Station, canonical_meal, clean_text, slugify
from .base import Adapter, AdapterError

GRAPHQL = "https://api.elevate-dxp.com/api/mesh/c087f756-cc72-4649-a36f-3a41b700c519/graphql"
PUBLIC_CLIENT_KEY = "ElevateAPIProd"  # shipped in the public web app's JS

Q_STORE = "query StoreConfig{Commerce_storeConfig{allergens_intolerances{label value} menu_preferences{label value}}}"
Q_LOCATIONS = (
    "query getLocations($campus_url_key:String!){getLocations(campusUrlKey:$campus_url_key)"
    "{commerceAttributes{url_key hasActiveMenus} aemAttributes{name}}}"
)
Q_LOCATION = (
    "query getLocation($campus_url_key:String!$location_url_key:String!){getLocation(campusUrlKey:$campus_url_key "
    "locationUrlKey:$location_url_key){commerceAttributes{url_key children{id name} meal_periods{id name position}} "
    "aemAttributes{name hoursOfOperation{schedule}}}}"
)
Q_RECIPES = (
    "query getLocationRecipes($campusUrlKey:String!$locationUrlKey:String!$date:String!$mealPeriod:Int"
    "$viewType:Commerce_MenuViewType!){getLocationRecipes(campusUrlKey:$campusUrlKey locationUrlKey:$locationUrlKey "
    "date:$date mealPeriod:$mealPeriod viewType:$viewType){locationRecipesMap{dateSkuMap{date stations{id "
    "skus{simple configurable{sku variants}}}}} products{items{name sku attributes{name value}}}}}"
)

DIET_LABELS = {
    "vegan": "vegan",
    "vegetarian": "vegetarian",
    "halal": "halal",
    "gluten free": "gluten_free",
    "kosher": "kosher",
}
ALLERGEN_LABELS = {
    "eggs": "egg",
    "fish": "fish",
    "milk": "milk",
    "peanuts": "peanuts",
    "sesame": "sesame",
    "shellfish": "shellfish",
    "soy": "soy",
    "tree nuts": "tree_nuts",
    "wheat": "wheat",
    "gluten": "gluten",
}
DAY_CODES = ["mo", "tu", "we", "th", "fr", "sa", "su"]


def meal_name(period: str) -> Optional[str]:
    low = period.lower()
    if "evening snack" in low or "overnight" in low:
        return "Late Night"
    if "snack" in low:
        return "Snack"
    return canonical_meal(period)


def _day_set(spec: str) -> set[int]:
    days: set[int] = set()
    for part in spec.lower().split(","):
        if "-" in part:
            a, b = (DAY_CODES.index(x.strip()) for x in part.split("-"))
            days.update(range(a, b + 1) if a <= b else [*range(a, 7), *range(0, b + 1)])
        elif part.strip() in DAY_CODES:
            days.add(DAY_CODES.index(part.strip()))
    return days


def opening_hours_for(rule: str, weekday: int) -> Optional[Tuple[str, str]]:
    """OSM-style 'Mo-Fr 07:15-11:00; Sa-Su off' -> ('07:15', '11:00') for that weekday, or None."""
    result: Optional[Tuple[str, str]] = None
    for clause in (rule or "").split(";"):
        m = re.match(r"\s*([A-Za-z,\- ]+?)\s+(off|(\d{1,2}:\d{2})-(\d{1,2}:\d{2}))\s*$", clause)
        if not m or weekday not in _day_set(m.group(1)):
            continue
        result = None if m.group(2) == "off" else (m.group(3).zfill(5), m.group(4).zfill(5))
    return result


class MyDiningHubAdapter(Adapter):
    max_days = 7

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.store: str = self.options["store"]
        self.campus_key: str = self.options.get("campus_url_key", "campus")
        self._codes: Optional[Tuple[Dict[str, str], Dict[str, str]]] = None
        self._locations: Optional[List[dict]] = None

    def gql(self, op: str, query: str, variables: dict) -> dict:
        code = self.store.rsplit("_", 1)[0]
        headers = {
            "accept": "application/json",
            "x-api-key": PUBLIC_CLIENT_KEY,
            "store": self.store,
            "magento-store-view-code": self.store,
            "magento-store-code": code,
            "magento-website-code": code,
        }
        params = {"operationName": op, "query": query, "variables": json.dumps(variables, sort_keys=True)}
        data = self.fetcher.get_json(GRAPHQL, params=params, headers=headers)
        if data.get("errors"):
            raise AdapterError(f"GraphQL {op}: {str(data['errors'])[:300]}")
        return data["data"]

    def codes(self) -> Tuple[Dict[str, str], Dict[str, str]]:
        """({allergen code: canonical}, {preference code: canonical tag})."""
        if self._codes is None:
            cfg = self.gql("StoreConfig", Q_STORE, {})["Commerce_storeConfig"]
            allergens = {
                o["value"]: ALLERGEN_LABELS[o["label"].lower()]
                for o in cfg["allergens_intolerances"]
                if o["label"].lower() in ALLERGEN_LABELS
            }
            prefs = {
                o["value"]: DIET_LABELS[o["label"].lower()]
                for o in cfg["menu_preferences"]
                if o["label"].lower() in DIET_LABELS
            }
            if not allergens or not prefs:
                raise AdapterError("store config returned no allergen/preference codes")
            self._codes = (allergens, prefs)
        return self._codes

    def locations(self) -> List[dict]:
        """Locations that publish menus, with stations / meal periods / hours (getLocation)."""
        if self._locations is None:
            listing = self.gql("getLocations", Q_LOCATIONS, {"campus_url_key": self.campus_key})["getLocations"]
            out = []
            for loc in listing:
                ca = loc.get("commerceAttributes") or {}
                if not ca.get("hasActiveMenus"):
                    continue
                detail = self.gql(
                    "getLocation", Q_LOCATION, {"campus_url_key": self.campus_key, "location_url_key": ca["url_key"]}
                )["getLocation"]
                out.append(detail)
            if not out:
                raise AdapterError("no locations with active menus")
            self._locations = out
        return self._locations

    def fetch_day(self, day: date) -> DayMenu:
        locations = [self._location(loc, day) for loc in self.locations()]
        return self.make_day(day, f"https://{self.store.split('_')[1]}.mydininghub.com/en/locations", locations)

    def _hours(self, loc: dict, day: date) -> Optional[Dict[str, Optional[Tuple[str, str]]]]:
        """{period name: (start, end) or None if closed}, or None if no schedule covers the day."""
        schedule = ((loc.get("aemAttributes") or {}).get("hoursOfOperation") or {}).get("schedule") or []
        covering = [
            s
            for s in schedule
            if (not s.get("start_date") or date.fromisoformat(s["start_date"]) <= day)
            and (not s.get("end_date") or day <= date.fromisoformat(s["end_date"]))
        ]
        if not covering:
            return None
        # Date-bounded "special" schedules (holidays, breaks) win over the standing one.
        entry = sorted(covering, key=lambda s: (s.get("type") == "special", bool(s.get("start_date"))))[-1]
        return {
            mp["meal_period"]: opening_hours_for(mp.get("opening_hours", ""), day.weekday())
            for mp in entry.get("meal_periods", [])
        }

    def _location(self, loc: dict, day: date) -> Location:
        ca = loc["commerceAttributes"]
        name = clean_text((loc.get("aemAttributes") or {}).get("name") or ca["url_key"])
        stations_by_id = {str(c["id"]): clean_text(c["name"]) for c in ca.get("children") or []}
        hours = self._hours(loc, day)
        allergen_codes, pref_codes = self.codes()

        meals: Dict[str, Meal] = {}
        for period in sorted(ca.get("meal_periods") or [], key=lambda p: p.get("position", 0)):
            canonical = meal_name(period["name"])
            if canonical is None:
                continue
            span = hours.get(period["name"]) if hours is not None else None
            if hours is not None and span is None:
                continue  # schedule says this period isn't served today
            data = self.gql(
                "getLocationRecipes",
                Q_RECIPES,
                {
                    "campusUrlKey": self.campus_key,
                    "locationUrlKey": ca["url_key"],
                    "date": day.isoformat(),
                    "mealPeriod": int(period["id"]),
                    "viewType": "DAILY",
                },
            )["getLocationRecipes"]
            stations = self._stations(data, day, stations_by_id, allergen_codes, pref_codes)
            if not stations:
                continue
            meal = meals.get(canonical)
            if meal is None:
                meal = meals[canonical] = Meal(
                    name=canonical, start=span[0] if span else None, end=span[1] if span else None
                )
            meal.stations += stations
        return Location(
            id=slugify(re.sub(r"^the\s+", "", name, flags=re.I)),
            name=name,
            type="dining_hall",
            meals=list(meals.values()),
        )

    @staticmethod
    def _stations(data: dict, day: date, names: Dict[str, str], allergen_codes, pref_codes) -> List[Station]:
        products = {p["sku"]: p for p in (data.get("products") or {}).get("items") or []}
        day_map = next(
            (
                d
                for d in (data.get("locationRecipesMap") or {}).get("dateSkuMap") or []
                if d.get("date") == day.isoformat()
            ),
            None,
        )
        if day_map is None:
            return []
        out = []
        for st in day_map.get("stations") or []:
            station = Station(name=names.get(str(st["id"]), "Menu"))
            skus = st.get("skus") or {}
            refs = list(skus.get("simple") or [])
            for conf in skus.get("configurable") or []:
                refs.append(conf["sku"] if conf["sku"] in products else next(iter(conf.get("variants") or []), ""))
            for sku in refs:
                p = products.get(sku)
                if p is None:
                    continue
                attrs = {a["name"]: a["value"] for a in p.get("attributes") or []}
                if str(attrs.get("is_hide_from_web_menu", "no")).lower() == "yes":
                    continue
                as_list = lambda v: v if isinstance(v, list) else ([] if v in (None, "") else [v])  # noqa: E731
                cal = attrs.get("calories")
                station.items.append(
                    Item(
                        name=clean_text(attrs.get("marketing_name") or p["name"]),
                        tags=sorted(
                            {pref_codes[c] for c in as_list(attrs.get("recipe_attributes")) if c in pref_codes}
                        ),
                        allergens=sorted(
                            {
                                allergen_codes[c]
                                for c in as_list(attrs.get("allergens_intolerances"))
                                if c in allergen_codes
                            }
                        ),
                        calories=round(float(cal)) if cal not in (None, "") else None,
                    )
                )
            if station.items:
                out.append(station)
        return out
