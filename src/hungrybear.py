# src/hungrybear.py

from datetime import datetime

from .constants import DINING_LOCATIONS
from .menu_scraper import MenuScraper


def choose_location() -> str:
    print("\nWhere do you want to eat today?")
    for i, loc in enumerate(DINING_LOCATIONS, start=1):
        print(f"{i}. {loc}")

    while True:
        raw = input("\nType the number (like 1) or type the location name: ").strip()
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(DINING_LOCATIONS):
                return DINING_LOCATIONS[idx - 1]
        if raw:
            return raw
        print("I got an empty input. Let me try again...")


def choose_meal_dynamic(available_meals: list[str]) -> str:
    print("\nWhich meal? (only showing meals this place actually has today)")

    for i, meal in enumerate(available_meals, start=1):
        print(f"{i}. {meal}")

    while True:
        raw = input("\nType the number (like 1) or type the meal name: ").strip().lower()
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(available_meals):
                return available_meals[idx - 1]

        # allow typing: breakfast/lunch/dinner/all day
        for m in available_meals:
            if raw == m.lower():
                return m

        print("That meal option doesn't match what's available here. Please try again.")


def print_menu(result) -> None:
    header = f"{result.location} | {result.meal}"
    if result.date_str:
        header += f" | {result.date_str}"
    print("\n" + header)

    if result.hours:
        print(f"Hours: {result.hours}")

    if not result.categories:
        print("\nI couldn't find menu items for that location/meal today.")
        print("Debug info:")
        print(result.debug or "(no debug message)")
        return

    print("\n" + "-" * 60)
    for cat, items in result.categories.items():
        print(f"\n[{cat}]")
        for it in items:
            print(f"- {it}")


def main() -> None:
    now = datetime.now()
    print(f"Today: {now.strftime('%Y-%m-%d %H:%M')}")

    scraper = MenuScraper()

    location = choose_location()

    available_meals, dbg = scraper.get_available_meals(location)
    if not available_meals:
        print("\nSorry, I couldn't detect available meals for this location today.")
        print("Debug info:")
        print(dbg or "(no debug message)")
        return

    meal = choose_meal_dynamic(available_meals)

    print(f"\nOK! I will fetch: {location} / {meal} ...\n")

    result = scraper.get_menu(location=location, meal=meal)
    print_menu(result)


if __name__ == "__main__":
    main()
