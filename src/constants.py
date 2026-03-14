# src/constants.py

DINING_MENUS_URL = "https://dining.berkeley.edu/menus/"

# Keep the canonical names aligned with what the UCB Dining "Menus" page shows
# (so our scraper can reliably locate each hall's block).
DINING_LOCATIONS = [
    "Café 3",
    "Clark Kerr",
    "Crossroads",
    "Foothill",
    "The Golden Bear Café",
    "The Eateries at Student Union",
    "Brown's",
    "Bear Market",
    "Local x Design",
    "The Den",
]

# User-friendly aliases / legacy names that should map to the canonical names above.
# This is critical because the website has changed naming over time.
LOCATION_ALIASES = {
    "Café 3": ["Cafe 3", "Cafe3", "Café 3"],
    "Clark Kerr": ["Clark Kerr Campus", "Clark Kerr Campus Dining", "CKC", "Clark Kerr Dining"],
    "The Golden Bear Café": ["Golden Bear Cafe", "Golden Bear Cafe", "Golden Bear"],
    "The Eateries at Student Union": ["Eateries at the Student Union", "Student Union", "Eateries"],
    "Brown's": ["Browns Cafe", "Brown's Cafe", "Browns"],
    "The Den": ["Den"],
}

# We only support these 3 meals for now.
MEALS = ["Breakfast", "Lunch", "Dinner"]
