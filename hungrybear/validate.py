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
CORE_MEALS = {"Breakfast", "Brunch", "Lunch", "Dinner"}


def total_items(menu: DayMenu) -> int:
    return sum(m.item_count for loc in menu.locations for m in loc.meals)


def validate_day(
    menu: DayMenu,
    cfg: CampusConfig,
    history_counts: Sequence[int] = (),
    today: Optional[date] = None,
    previous: Optional[DayMenu] = None,
) -> DayMenu:
    """Return `menu` with status/reason updated.

    history_counts: total_items() of recent same-weekday days (anomaly baseline).
    previous: what we already published for this same date, if anything.
    """
    if menu.status == "broken":
        return menu

    today = today or today_pacific()
    errors: List[str] = []
    in_break = cfg.in_break(menu.date)

    # A menu that was already published for this date doesn't vanish or collapse - a parser that
    # suddenly finds nothing does. Without this, a broken scraper would overwrite good future days.
    prev_count = total_items(previous) if previous is not None and previous.status == "ok" else 0
    if prev_count >= 20 and total_items(menu) < prev_count * (1 - ANOMALY_DROP):
        errors.append(f"only {total_items(menu)} items, but this date previously had {prev_count}")
    near_term = menu.date <= today + timedelta(days=NEAR_TERM_DAYS)

    count = total_items(menu)
    if menu.status == "closed" or count == 0:
        # No food anywhere on a normal near-term day is far more likely a broken scraper than a closure.
        # (Far-future days are often just not published yet.)
        if near_term and not in_break and cfg.main_halls:
            errors.append("no menu items on a non-break day")
    else:
        weekend = menu.date.weekday() >= 5
        for hall_id in cfg.main_halls:
            if weekend and hall_id in cfg.weekday_only:
                continue
            hall = menu.location(hall_id)
            if hall is None:
                errors.append(f"main hall {hall_id!r} missing (renamed or removed?)")
                continue
            hall_items = sum(m.item_count for m in hall.meals)
            if hall_items == 0:
                if near_term and not in_break:
                    errors.append(f"main hall {hall_id!r} has no menu items")
                continue
            # A broken parser thins out every meal, while real menus have the odd tiny one (a weekday
            # "Brunch" with 4 items). So flag a hall only when even its biggest core meal is thin.
            core = {m.name: m.item_count for m in hall.meals if m.name in CORE_MEALS}
            if core and max(core.values()) < cfg.min_items_per_meal:
                errors.append(f"{hall_id}: largest meal has {max(core.values())} items (< {cfg.min_items_per_meal})")

        names = [it.name for loc in menu.locations for m in loc.meals for s in m.stations for it in s.items]
        junk = [n for n in names if not n or len(n) > MAX_ITEM_LEN or UI_NOISE.match(n)]
        if len(junk) / len(names) > 0.05:
            errors.append(f"{len(junk)}/{len(names)} item names look like page chrome, e.g. {junk[:3]}")

        if len(history_counts) >= 2:
            baseline = statistics.median(history_counts)
            if baseline >= 20 and count < baseline * (1 - ANOMALY_DROP):
                errors.append(f"only {count} items vs. typical {baseline:.0f} for this weekday")

    if errors:
        menu.status = "broken"
        menu.reason = "; ".join(errors)
    return menu
