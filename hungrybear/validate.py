# hungrybear/validate.py
"""Sanity checks that turn "parsed without crashing" into "parsed correctly".

The dangerous failure mode for a scraper is not an exception - it is a site change that
makes us silently return an empty or garbled menu. These checks catch that and downgrade
the day to status="broken" so it alerts instead of being published.
"""

from __future__ import annotations

import re
import statistics
from datetime import date, timedelta
from typing import List, Optional, Sequence

from .config import CampusConfig, today_pacific
from .models import DayMenu

# Strings that only show up in item names when we are scraping UI chrome instead of food.
UI_NOISE = re.compile(
    r"^(filters?|include|exclude|legend|food legend|nutrition( facts)?|calories|serving size|"
    r"show (vegan|vegetarian|halal|all)|reset filters|now (open|closed)|menu|select a date|"
    r"nutrition calculator|add to (cart|order))$",
    re.I,
)
MAX_ITEM_LEN = 120
# Only near-term days must have menus; sites often publish far-future days late.
NEAR_TERM_DAYS = 1
ANOMALY_DROP = 0.6


def total_items(menu: DayMenu) -> int:
    return sum(m.item_count for loc in menu.locations for m in loc.meals)


def validate_day(
    menu: DayMenu,
    cfg: CampusConfig,
    history_counts: Sequence[int] = (),
    today: Optional[date] = None,
) -> DayMenu:
    """Return `menu` with status/reason updated. history_counts = total_items() of recent same-weekday days."""
    if menu.status == "broken":
        return menu

    today = today or today_pacific()
    errors: List[str] = []
    in_break = cfg.in_break(menu.date)
    near_term = menu.date <= today + timedelta(days=NEAR_TERM_DAYS)

    if not menu.locations:
        errors.append("no locations parsed")

    if menu.status == "closed":
        # Everything closed on a normal near-term day is far more likely a broken scraper than a closure.
        if near_term and not in_break and cfg.main_halls:
            errors.append("every location closed on a non-break day")
    else:
        for hall_id in cfg.main_halls:
            hall = menu.location(hall_id)
            if hall is None:
                errors.append(f"main hall {hall_id!r} missing (renamed or removed?)")
                continue
            for meal in hall.meals:
                if meal.item_count < cfg.min_items_per_meal:
                    errors.append(f"{hall_id} {meal.name}: only {meal.item_count} items (< {cfg.min_items_per_meal})")

        names = [it.name for loc in menu.locations for m in loc.meals for s in m.stations for it in s.items]
        junk = [n for n in names if not n or len(n) > MAX_ITEM_LEN or UI_NOISE.match(n)]
        if names and len(junk) / len(names) > 0.05:
            errors.append(f"{len(junk)}/{len(names)} item names look like page chrome, e.g. {junk[:3]}")

        if len(history_counts) >= 3:
            baseline = statistics.median(history_counts)
            count = total_items(menu)
            if baseline >= 20 and count < baseline * (1 - ANOMALY_DROP):
                errors.append(f"only {count} items vs. typical {baseline:.0f} for this weekday")

    if errors:
        menu.status = "broken"
        menu.reason = "; ".join(errors)
    return menu
