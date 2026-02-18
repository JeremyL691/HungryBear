# src/menu_scraper.py

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from .constants import DINING_MENUS_URL, DINING_LOCATIONS


@dataclass
class MenuResult:
    location: str
    meal: str
    date_str: Optional[str]
    hours: Optional[str]
    categories: Dict[str, List[str]]
    debug: Optional[str] = None


class MenuScraper:
    """
    HungryBear (fast + robust)

    New feature:
    - get_available_meals(location): returns only meals that exist for that dining hall today
      e.g. ["Breakfast", "Lunch"] or ["All Day"] or ["Lunch"].

    Why:
    - Some halls don't serve dinner.
    - Some only have 1 meal or only "All Day".
    """

    ALLERGEN_TAGS = {
        "milk", "egg", "fish", "shellfish", "tree nuts", "wheat", "peanuts", "soybeans", "sesame",
        "gluten", "pork", "alcohol",
        "halal", "kosher",
        "vegan option", "vegetarian option",
        "low carbon footprint", "medium carbon footprint", "high carbon footprint",
        "co2",
    }

    UI_NOISE = {
        "filters", "include", "exclude", "location", "meal", "date",
        "food legend", "open all day", "now open", "now closed",
    }

    KNOWN_CATEGORIES = {
        "center plate",
        "lemon grass",
        "grill",
        "pasta",
        "soup",
        "dessert",
        "allergen friendly",
        "composed salads",
        # Foothill
        "chef's table",
        "chefs table",
        "iron & ember",
        "iron and ember",
        "fire & flour",
        "fire and flour",
        "pure plates",
    }

    DINING_LOCATIONS_SET = {re.sub(r"\s+", " ", name.strip().lower()) for name in DINING_LOCATIONS}

    def fetch_html(self) -> str:
        headers = {"User-Agent": "HungryBear/1.0 (student project)"}
        resp = requests.get(DINING_MENUS_URL, headers=headers, timeout=25)
        resp.raise_for_status()
        return resp.text

    def _norm(self, s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip().lower())

    def _clean_line(self, s: str) -> str:
        s = (s or "").strip()
        s = re.sub(r"^[\*\-\•]\s*", "", s)
        s = re.sub(r"\bImage:\s*", "", s, flags=re.I)
        s = re.sub(r"\s{2,}", " ", s).strip()
        return s

    def _is_date_line(self, s: str) -> bool:
        return bool(re.match(r"^[A-Za-z]{3},\s+[A-Za-z]{3}\s+\d{1,2}$", s.strip()))

    def _is_time_line(self, s: str) -> bool:
        return bool(re.search(r"\d{1,2}:\d{2}\s*(a\.m\.|p\.m\.)", s, flags=re.I))

    def _is_meal_header(self, s: str) -> bool:
        low = self._norm(s)
        return ("-" in low) and any(low.endswith(x) for x in ["breakfast", "lunch", "dinner", "all day"])

    def _meal_type_from_header(self, header_line: str) -> Optional[str]:
        """
        "Spring - Breakfast" -> "Breakfast"
        "Spring - All Day"   -> "All Day"
        """
        low = self._norm(header_line)
        if low.endswith("breakfast"):
            return "Breakfast"
        if low.endswith("lunch"):
            return "Lunch"
        if low.endswith("dinner"):
            return "Dinner"
        if low.endswith("all day"):
            return "All Day"
        return None

    def _is_location_name_line(self, s: str) -> bool:
        return self._norm(s) in self.DINING_LOCATIONS_SET

    def _is_noise(self, s: str) -> bool:
        low = self._norm(s)
        if not low:
            return True
        if low in self.ALLERGEN_TAGS:
            return True
        if low in self.UI_NOISE:
            return True
        if self._is_location_name_line(s):
            return True
        if len(low) <= 2:
            return True
        return False

    def _dedupe_keep_order(self, items: List[str]) -> List[str]:
        seen = set()
        out = []
        for it in items:
            k = self._norm(it)
            if k and k not in seen:
                seen.add(k)
                out.append(it)
        return out

    def _html_to_lines(self, html: str) -> List[str]:
        soup = BeautifulSoup(html, "lxml")
        main = soup.find("main") or soup.body
        text = main.get_text("\n", strip=True)
        lines = [self._clean_line(ln) for ln in text.splitlines()]
        return [ln for ln in lines if ln]

    # ---------------- location slicing (supports 1-line and 2-line "Now Closed") ----------------

    def _find_all_location_starts(self, lines: List[str]) -> List[Tuple[str, int]]:
        starts: List[Tuple[str, int]] = []
        one_line_re = re.compile(r"^(?P<loc>.+?)\s+Now\s+(Open|Closed)\s*$", re.I)

        def is_now_line(s: str) -> bool:
            low = self._norm(s)
            return low in {"now open", "now closed"} or ("now" in low and ("open" in low or "closed" in low))

        for i, ln in enumerate(lines):
            m = one_line_re.match(ln)
            if m:
                loc = self._norm(m.group("loc"))
                if loc in self.DINING_LOCATIONS_SET:
                    starts.append((loc, i))
                    continue

            if is_now_line(ln) and i - 1 >= 0:
                prev = self._norm(lines[i - 1])
                if prev in self.DINING_LOCATIONS_SET:
                    starts.append((prev, i - 1))

        seen = set()
        uniq: List[Tuple[str, int]] = []
        for loc, idx in sorted(starts, key=lambda x: x[1]):
            if loc not in seen:
                seen.add(loc)
                uniq.append((loc, idx))
        return uniq

    def _slice_location_block(self, lines: List[str], location: str) -> Optional[List[str]]:
        wanted = self._norm(location)
        starts = self._find_all_location_starts(lines)
        if not starts:
            return None

        start_idx = None
        for loc, idx in starts:
            if loc == wanted:
                start_idx = idx
                break
        if start_idx is None:
            return None

        end_idx = len(lines)
        for loc, idx in starts:
            if idx > start_idx:
                end_idx = idx
                break

        return lines[start_idx:end_idx]

    # ---------------- new: available meals for the chosen hall ----------------

    def get_available_meals(self, location: str) -> Tuple[List[str], Optional[str]]:
        """
        Returns (meals, debug_message_if_any)
        meals are among: Breakfast, Lunch, Dinner, All Day
        """
        html = self.fetch_html()
        lines = self._html_to_lines(html)

        loc_block = self._slice_location_block(lines, location)
        if not loc_block:
            starts = self._find_all_location_starts(lines)
            found = [loc for loc, _ in starts]
            return [], f"Location block not found. Detected hall headers: {found}"

        meals: List[str] = []
        for ln in loc_block:
            if self._is_meal_header(ln):
                mt = self._meal_type_from_header(ln)
                if mt:
                    meals.append(mt)

        meals = self._dedupe_keep_order(meals)
        return meals, None

    # ---------------- meal slicing for menu extraction ----------------

    def _slice_best_meal_block(self, loc_lines: List[str], meal: str) -> Optional[List[str]]:
        wanted = meal.strip().lower()

        candidates = [i for i, ln in enumerate(loc_lines) if self._is_meal_header(ln) and self._norm(ln).endswith(wanted)]
        if not candidates:
            return None

        meal_indices = [i for i, ln in enumerate(loc_lines) if self._is_meal_header(ln)]
        meal_indices.sort()

        def score(start: int, end: int) -> int:
            s = 0
            for ln in loc_lines[start + 1 : min(end, start + 320)]:
                if self._is_meal_header(ln) or self._is_date_line(ln) or self._is_time_line(ln):
                    continue
                if self._is_noise(ln):
                    continue
                s += 1
            return s

        best_block = None
        best_score = -1

        for start in candidates:
            end = len(loc_lines)
            for mi in meal_indices:
                if mi > start:
                    end = mi
                    break
            sc = score(start, end)
            if sc > best_score:
                best_score = sc
                best_block = loc_lines[start:end]

        return best_block

    # ---------------- date + hours ----------------

    def _extract_date(self, loc_lines: List[str]) -> Optional[str]:
        for ln in loc_lines[:260]:
            if self._is_date_line(ln):
                return ln
        return None

    def _extract_hours_for_meal(self, loc_lines: List[str], meal: str) -> Optional[str]:
        """
        Best effort:
        - Many halls list time ranges in order (Breakfast/Lunch/Dinner) or just one.
        - We'll pick by meal position IF 3 exist; otherwise pick the closest sensible.
        """
        time_ranges = []
        for ln in loc_lines[:320]:
            if re.search(r"\d{1,2}:\d{2}\s*(a\.m\.|p\.m\.)\s*-\s*\d{1,2}:\d{2}", ln, flags=re.I):
                time_ranges.append(ln)

        time_ranges = self._dedupe_keep_order(time_ranges)
        if not time_ranges:
            return None

        if meal.strip().lower() == "all day":
            # for all-day halls, they often show just one range
            return time_ranges[0]

        meal_map = {"breakfast": 0, "lunch": 1, "dinner": 2}
        idx = meal_map.get(meal.strip().lower())

        if idx is None:
            return time_ranges[0]

        # if we have 3 ranges, use the index
        if len(time_ranges) >= 3:
            return time_ranges[idx]

        # if only 2 ranges, dinner usually isn't available; pick lunch for idx=1 else first
        if len(time_ranges) == 2:
            return time_ranges[1] if idx >= 1 else time_ranges[0]

        # only 1 range
        return time_ranges[0]

    # ---------------- parse categories + dishes ----------------

    def _looks_like_dish(self, s: str) -> bool:
        if self._is_noise(s) or self._is_date_line(s) or self._is_time_line(s) or self._is_meal_header(s):
            return False
        return len(s) >= 3

    def _is_category_strong(self, cur: str, nxt: Optional[str], nxt2: Optional[str]) -> bool:
        if not cur or self._is_noise(cur) or self._is_meal_header(cur) or self._is_date_line(cur) or self._is_time_line(cur):
            return False

        cur_norm = self._norm(cur)
        if cur_norm in self.KNOWN_CATEGORIES:
            return True

        if len(cur) > 35:
            return False
        if not nxt or not nxt2:
            return False
        if not self._looks_like_dish(nxt):
            return False
        if not self._is_noise(nxt2):
            return False
        return True

    def _parse_categories_and_dishes(self, meal_lines: List[str]) -> Dict[str, List[str]]:
        if meal_lines and self._is_meal_header(meal_lines[0]):
            meal_lines = meal_lines[1:]

        categories: Dict[str, List[str]] = {}
        current_cat = "Uncategorized"
        categories.setdefault(current_cat, [])

        n = len(meal_lines)
        i = 0
        while i < n:
            cur = meal_lines[i]
            nxt = meal_lines[i + 1] if i + 1 < n else None
            nxt2 = meal_lines[i + 2] if i + 2 < n else None

            if self._is_noise(cur) or self._is_date_line(cur) or self._is_time_line(cur):
                i += 1
                continue

            if self._is_category_strong(cur, nxt, nxt2):
                current_cat = cur
                categories.setdefault(current_cat, [])
                i += 1
                continue

            if self._looks_like_dish(cur):
                categories.setdefault(current_cat, [])
                categories[current_cat].append(cur)

            i += 1

        cleaned: Dict[str, List[str]] = {}
        for cat, items in categories.items():
            items = [x for x in items if self._looks_like_dish(x) and not self._is_noise(x)]
            items = self._dedupe_keep_order(items)
            if items:
                cleaned[cat] = items
        return cleaned

    # ---------------- public: get menu ----------------

    def get_menu(self, location: str, meal: str) -> MenuResult:
        html = self.fetch_html()
        lines = self._html_to_lines(html)

        loc_block = self._slice_location_block(lines, location)
        if not loc_block:
            starts = self._find_all_location_starts(lines)
            found = [loc for loc, _ in starts]
            return MenuResult(location, meal, None, None, {}, f"Location block not found. Detected hall headers: {found}")

        date_str = self._extract_date(loc_block)
        hours = self._extract_hours_for_meal(loc_block, meal)

        meal_block = self._slice_best_meal_block(loc_block, meal)
        if not meal_block:
            detected = [ln for ln in loc_block if self._is_meal_header(ln)]
            detected = self._dedupe_keep_order(detected)
            return MenuResult(location, meal, date_str, hours, {}, f"Meal block not found. Meal headers detected: {detected[:30]}")

        categories = self._parse_categories_and_dishes(meal_block)
        if not categories:
            preview = meal_block[:160]
            return MenuResult(location, meal, date_str, hours, {}, "Meal block found, but 0 dishes parsed. Preview:\n" + "\n".join(preview))

        return MenuResult(location, meal, date_str, hours, categories, None)
