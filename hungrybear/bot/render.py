# hungrybear/bot/render.py
"""Pure text/label rendering for the bot (no Telegram objects - easy to unit test)."""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from ..config import PACIFIC
from ..models import DayMenu, Item, Location, Meal

MEAL_EMOJI = {
    "Breakfast": "🍳",
    "Brunch": "🥞",
    "Lunch": "🥪",
    "Snack": "🍪",
    "Dinner": "🍽️",
    "Late Night": "🌙",
    "All Day": "🕒",
}
# Meal names <-> one-letter codes so callback_data stays under Telegram's 64-byte limit.
MEAL_CODES = {
    "Breakfast": "B",
    "Brunch": "R",
    "Lunch": "L",
    "Snack": "S",
    "Dinner": "D",
    "Late Night": "N",
    "All Day": "A",
}
CODE_MEALS = {v: k for k, v in MEAL_CODES.items()}
TAG_MARKS = {"vegan": "🌱", "vegetarian": "🥕", "halal": "Ⓗ", "kosher": "Ⓚ"}
DIETS = {"vegetarian": "🥕 Vegetarian", "vegan": "🌱 Vegan", "halal": "Ⓗ Halal"}
STALE_HOURS = 12
TELEGRAM_LIMIT = 3800  # hard limit is 4096; leave room


def hhmm_pretty(t: Optional[str]) -> str:
    if not t:
        return "?"
    h, m = map(int, t.split(":"))
    suffix = "am" if h < 12 or h == 24 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{suffix}" if m else f"{h12}{suffix}"


def hours_text(meal: Meal) -> str:
    if not meal.start and not meal.end:
        return ""
    return f"{hhmm_pretty(meal.start)}–{hhmm_pretty(meal.end)}"


def day_label(d: date, today: date) -> str:
    delta = (d - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    return f"{d:%a} {d.month}/{d.day}"


def day_title(d: date, today: date) -> str:
    """'Today (Sat Oct 3)' / 'Mon 10/5' - the full date only when the label is relative."""
    label = day_label(d, today)
    return f"{label} ({d:%a %b} {d.day})" if label in ("Today", "Tomorrow") else label


def is_now(meal: Meal, d: date, now: datetime) -> bool:
    if d != now.date() or not meal.start or not meal.end:
        return False
    cur = now.strftime("%H:%M")
    end = "24:00" if meal.end == "00:00" else meal.end
    return meal.start <= cur < end


def current_or_next_meal(loc: Location, d: date, now: datetime) -> Optional[Meal]:
    """The meal being served now, else the next one today, else the first with food."""
    with_food = [m for m in loc.meals if m.item_count]
    for m in with_food:
        if is_now(m, d, now):
            return m
    if d == now.date():
        cur = now.strftime("%H:%M")
        upcoming = [m for m in with_food if m.start and m.start > cur]
        if upcoming:
            return upcoming[0]
    return with_food[0] if with_food else None


def sort_locations(locs: List[Location]) -> List[Location]:
    """Open with a menu, then open (hours only), then closed; dining halls first within each group."""

    def key(loc: Location):
        has_food = any(m.item_count for m in loc.meals)
        group = 0 if (loc.status == "open" and has_food) else (1 if loc.status == "open" else 2)
        return (group, loc.type != "dining_hall")

    return sorted(locs, key=key)


def location_button(loc: Location) -> str:
    if loc.status != "open":
        return f"{loc.name} · closed"
    if not any(m.item_count for m in loc.meals):
        return f"{loc.name} · hours only"
    return loc.name


def meal_button(meal: Meal, now_flag: bool) -> str:
    label = f"{MEAL_EMOJI.get(meal.name, '•')} {meal.name}"
    hrs = hours_text(meal)
    if hrs:
        label += f" {hrs}"
    return ("▶ " if now_flag else "") + label


def keep(item: Item, diet: Optional[str]) -> bool:
    if diet is None:
        return True
    if diet == "vegetarian":
        return bool({"vegetarian", "vegan"} & set(item.tags))
    return diet in item.tags


def freshness(fetched_at: datetime, now: datetime) -> str:
    local = fetched_at.astimezone(PACIFIC)
    age_h = (now - local).total_seconds() / 3600
    stamp = f"{local:%a} {local:%-I:%M%p}".replace("AM", "am").replace("PM", "pm")
    if age_h > STALE_HOURS:
        return f"⚠️ Data last updated {stamp} — may be outdated"
    return f"Updated {stamp}"


def menu_text(day: DayMenu, loc: Location, meal: Meal, campus_name: str, diet: Optional[str], now: datetime) -> str:
    lines = [
        f"{MEAL_EMOJI.get(meal.name, '')} {loc.name} · {meal.name}",
        f"{campus_name} · {day.date:%a, %b} {day.date.day}",
    ]
    hrs = hours_text(meal)
    if hrs:
        lines.append(f"🕐 {hrs}" + (" (serving now)" if is_now(meal, day.date, now) else ""))
    if meal.note:
        lines.append(f"ℹ️ {meal.note}")
    if diet:
        lines.append(f"Filter: {DIETS[diet]} only  (/diet to change)")
    lines.append("")

    shown = 0
    for st in meal.stations:
        items = [it for it in st.items if keep(it, diet)]
        if not items:
            continue
        lines.append(f"【{st.name}】")
        for it in items:
            marks = "".join(TAG_MARKS[t] for t in ("vegan", "vegetarian", "halal", "kosher") if t in it.tags)
            if "vegan" in it.tags:
                marks = marks.replace(TAG_MARKS["vegetarian"], "")
            cal = f" · {it.calories} cal" if it.calories else ""
            lines.append(f"• {it.name}" + (f" {marks}" if marks else "") + cal)
        lines.append("")
        shown += len(items)

    if not meal.stations:
        lines.append("No menu published for this meal — hours only.")
        lines.append("")
    elif shown == 0:
        lines.append("Nothing matches your diet filter for this meal.")
        lines.append("")
    lines.append("🌱 vegan  🥕 vegetarian  Ⓗ halal  Ⓚ kosher")
    lines.append(freshness(day.fetched_at, now))
    return "\n".join(lines).strip()


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> List[str]:
    """Split on line boundaries so each part fits in one Telegram message."""
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    cur: List[str] = []
    size = 0
    for line in text.splitlines():
        if cur and size + len(line) + 1 > limit:
            parts.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        parts.append("\n".join(cur))
    return parts
