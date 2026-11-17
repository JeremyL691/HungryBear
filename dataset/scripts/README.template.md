# HungryBear UC Dining Menus Dataset

> **Where this lives:** this folder is on the separate **`dataset`** branch of
> [JeremyL691/HungryBear](https://github.com/JeremyL691/HungryBear). It is a derived, read-only
> export. It does not change the scraper, the `main` branch, or the `data` branch that serves the
> live JSON API.

A tidy, deduplicated history of dining-hall menus from all **9 University of California
undergraduate campuses**, built from every snapshot the HungryBear scraper has committed.
Each row is one dish served at one dining location, for one meal, on one date. Each row carries diet tags,
allergens and calories where the campus publishes them.

| | |
|---|---|
| **Rows (dish appearances)** | **{{ROWS}}** |
| **Unique dishes** | **{{DISHES}}** |
| **Menu dates** | {{DATE_MIN}} to {{DATE_MAX}} ({{N_DAYS}} days) |
| **Campuses / dining locations** | 9 / {{HALLS}} |
| **Built from** | {{COMMITS}} commits, {{FILE_VERSIONS}} menu-file versions, data branch at `{{SOURCE_HEAD}}` (last commit {{LAST_COMMIT}}) |
| **Formats** | gzip CSV, Parquet, 200-row preview CSV |
| **License** | MIT (same as HungryBear); menu content belongs to the UC dining services |

![Rows per campus over time](charts/rows_per_campus_over_time.png)

## Files

| File | Size | Contents |
|---|---:|---|
{{FILES_TABLE}}

Start with [`data/sample_menus.csv`](data/sample_menus.csv) to see the columns in your browser.

## Quick start

```python
import pandas as pd

base = "https://raw.githubusercontent.com/JeremyL691/HungryBear/dataset/dataset/data/"
menus = pd.read_csv(base + "hungrybear_menus.csv.gz", dtype={"calories": "Int64"})
# In CSV, NA and "empty list" are both empty cells. Use *_reported_by_campus to tell them apart.
# or, faster and typed (needs pyarrow):
menus = pd.read_parquet(base + "hungrybear_menus.parquet")
dishes = pd.read_parquet(base + "hungrybear_dishes.parquet")

# example: share of vegan dishes per campus (only campuses that publish diet tags)
tagged = menus[menus.tags_reported_by_campus]
print(tagged.groupby("campus").is_vegan.mean().sort_values())

# list columns -> Python lists
menus["allergen_list"] = menus.allergens.fillna("").str.split("|").apply(lambda x: [a for a in x if a])
```

## Coverage by campus

{{COVERAGE_TABLE}}

"% w/ allergens" and "% w/ diet tags" count rows with at least one value. Rows with an empty list
are not counted, because an empty list can mean the dish really has none. UC Merced publishes
neither tags nor allergens. Berkeley, UCLA, UCR, UCSB and UCSC publish no calories.

![Field completeness by campus](charts/field_completeness_by_campus.png)

Most frequent dishes:

{{TOP_DISHES}}

## Data dictionary

### `hungrybear_menus` (one row per dish appearance)

Primary key: **(`date`, `campus_id`, `hall_id`, `meal`, `dish_id`)**, unique (checked at build time).

| Column | Type | Description |
|---|---|---|
| `date` | string `YYYY-MM-DD` | Menu date (local Pacific date the dish is served). |
| `weekday` | string | Day name of `date` (`Monday` ... `Sunday`). |
| `campus_id` | string | `ucb`, `ucd`, `uci`, `ucla`, `ucm`, `ucr`, `ucsb`, `ucsc`, `ucsd`. |
| `campus` | string | Display name, e.g. `UC Berkeley`. |
| `hall_id` | string | Stable slug of the dining location from the scraper, e.g. `cafe-3`. |
| `dining_hall` | string | Location display name, e.g. `Café 3`. |
| `hall_type` | string | `dining_hall`, `cafe`, `market` or `restaurant`. |
| `meal` | string | One of `Breakfast`, `Brunch`, `Lunch`, `Snack`, `Dinner`, `Late Night`, `All Day`. |
| `meal_start`, `meal_end` | string `HH:MM` / NA | Service hours (24h, Pacific). NA where the campus does not publish hours (UC Merced, UCSC, UCSD and most of UCR). |
| `meal_note` | string / NA | Free-text note on the meal period, e.g. holiday hours. |
| `station` | string / NA | Station or category within the meal, e.g. `Center Plate`. If the same dish was listed at several stations in one meal, they are joined with `\|` in listing order. |
| `dish_id` | string (12 hex) | `sha1(dish_key)[:12]`, where `dish_key` is the normalized, case-folded dish name. It is the same across campuses, so "Scrambled Eggs" at Berkeley and at Davis get the same id. Stable across rebuilds. |
| `dish_name` | string | Normalized display name: the most common cleaned spelling among all spellings that share the `dish_key`. |
| `dish_name_raw` | string | Name exactly as published in the latest snapshot that contained the row. |
| `tags` | string / NA | Diet tags, `\|`-separated, fixed vocabulary and order: `vegan`, `vegetarian`, `halal`, `kosher`, `gluten_free`. Empty string = campus publishes tags but none for this dish. NA = campus never publishes tags. |
| `allergens` | string / NA | `\|`-separated, fixed vocabulary and order: `milk`, `egg`, `fish`, `shellfish`, `tree_nuts`, `peanuts`, `wheat`, `soy`, `sesame`, `gluten`, `pork`, `alcohol`, `coconut`. Empty / NA work the same way as for `tags`. |
| `calories` | integer / NA | Calories per serving as published. NA = not published. `0` only when the source literally says 0 (see limitations). Missing values are never filled with 0. |
| `description` | string / NA | Item description (only UC Merced publishes these). |
| `is_vegan` ... `is_gluten_free` | boolean / NA | One-hot versions of `tags`. NA for campuses without tags. |
| `tags_reported_by_campus`, `allergens_reported_by_campus`, `calories_reported_by_campus` | boolean | Whether the campus publishes the field at all. Use these to tell "none listed" from "unknown". |
| `n_listings` | int | How many times the dish was listed within that meal in the final snapshot (more than 1 means listed at several stations). |
| `n_snapshot_versions` | int | How many distinct versions of the day's menu file contained this row. |
| `in_latest_snapshot` | boolean | `True` if the row is still in the newest version of that day's menu file. `False` means it appeared earlier but was removed in a later scrape (menu change, sold-out item, or source edit). Filter on `True` for "final published menu only". |
| `first_captured_at`, `last_captured_at` | string (ISO 8601, UTC) | Scrape time (`fetched_at`) of the first and last snapshot that contained the row. |
| `source_url` | string / NA | Menu page or API the snapshot was scraped from. |

### `hungrybear_dishes` (one row per unique `dish_id`)

| Column | Description |
|---|---|
| `dish_id`, `dish_name` | As above. |
| `n_appearances` | Number of rows in the menus table. |
| `n_days` | Distinct menu dates the dish appeared on. |
| `first_seen`, `last_seen` | First and last menu date. |
| `n_campuses`, `campuses` | Number of campuses and `\|`-separated campus ids. |
| `n_halls` | Distinct (campus, dining location) pairs. |
| `meals` | `\|`-separated meal periods the dish appeared in. |
| `tags_any`, `allergens_any` | Union of all tags / allergens seen for the dish (NA if none). |
| `calories_median` | Median published calories (NA if never published). |
| `n_name_variants` | Number of distinct cleaned spellings merged into this dish. |

## How it was built

[`scripts/build_dataset.py`](scripts/build_dataset.py) rebuilds everything here (data, charts and
this README) from a fresh clone:

```bash
git clone https://github.com/JeremyL691/HungryBear.git
pip install -r requirements.txt
python scripts/build_dataset.py /path/to/HungryBear     # reads origin/data; about 1-2 minutes
```

1. **Walk all history.** The published API keeps only about 14 days, so the script lists every commit
   reachable from the `data` branch (merge parents included), ordered by commit time. It then reads
   every distinct version of every `v1/<campus>/<date>.json` file with `git cat-file`. That is
   {{FILE_VERSIONS}} versions holding {{RAW_LISTINGS}} raw item listings.
2. **Union and deduplicate.** Records are keyed by (date, campus, location, meal, normalized dish
   name). File versions are applied oldest to newest, so when a later scrape changes a record
   (calories, tags, station, hours), **the latest version wins**. {{CORRECTED}} such overwrites happened
   during the build. Records that a later scrape dropped are **kept** and flagged with
   `in_latest_snapshot = False` ({{NOT_IN_LATEST}} rows).
3. **Merge within a meal.** If one meal lists the same dish more than once (usually at two stations),
   it becomes one row: stations are joined, tags and allergens are unioned, and the first non-missing
   calorie value is kept ({{MULTI_STATION}} rows).
4. **Normalize text.** Unicode NFKC; curly quotes become ASCII; en/em dashes become `-`; ®/™ and
   control characters are dropped; whitespace is collapsed and stray spaces before punctuation are
   removed. The matching key is also case-folded, with spacing around `&` and `/` unified, so
   `Potatoes Au Gratin`, `Potatoes au Gratin` and `Potatoes  au  Gratin` become one dish.
   Wording is not merged further: no stemming, no fuzzy matching.
5. **Unify vocabularies.** Meal names map to the 7 canonical periods. Tags and allergens are
   lower-cased, given aliases (`dairy` becomes `milk`, `gluten-free` becomes `gluten_free`, ...) and
   sorted in a fixed order. The build fails if an unknown value appears.
6. **Missing values.** NA means "not published". For tags and allergens, an empty string means "the
   campus publishes this field, and the list for this dish is empty". Calories are never filled
   with 0.
7. **Sanity checks.** No duplicate keys; dates in a plausible window and at most about a week past
   the last scrape; one dishes row per `dish_id`, with appearances summing to the row count; no
   out-of-vocabulary tags or allergens. Results are in [`data/build_summary.json`](data/build_summary.json).

Output is deterministic: the same clone state gives byte-identical CSV and gzip files.

## Known limitations

- **Short window.** About 7 weeks of Fall 2026 (scraping on the `data` branch began 2026-10-03), so
  there is no seasonality across terms. The last few dates are menus **published ahead** of time, as
  of the last scrape. They are partial (not every campus posts a full week ahead) and may still change.
- **Uneven campuses.** UCSD lists many more items per meal (about a third of all rows) than other
  campuses. Davis and Merced post only the current week, and UCLA quick-service spots publish hours
  but no menus. Locations with no items (cafes and markets that publish only hours) have no rows.
- **Calories** exist only for UCSD, Merced, Davis and part of UCI. **Berkeley has none.** A few
  hundred rows carry a published `0` (condiments, tea, salad-bar bases). Treat these with care.
- **Allergens and tags come from the campus labels.** They are not verified, and vocabularies
  differ by campus (for example, UCI, UCR and UCSB never label `gluten` separately from `wheat`). UCSB
  rarely lists allergens. An empty allergen list does **not** mean allergen-free.
- **Dish identity is name-based.** The same dish under different wording ("Mac & Cheese" vs
  "Macaroni and Cheese") gets different ids. Different recipes with the same name share one id.
  Some names include extra info such as times (`2pm Curly Fries`).
- **Scrape gaps.** If the scraper failed for a stretch, menus that were only published in that
  stretch may be missing. A broken scrape never overwrites good data upstream, so missing data
  shows up as gaps, not corrupted rows.

## License and attribution

Code and derived dataset: MIT, same as [HungryBear](https://github.com/JeremyL691/HungryBear)
(© 2026 JeremyL691). Menu names, labels and nutrition values come from the public dining websites
of the University of California campuses (Berkeley, Davis, Irvine, UCLA, Merced, Riverside, Santa
Barbara, Santa Cruz, San Diego). This dataset is not affiliated with or endorsed by UC. Nutrition
and allergen fields are for analysis only and must not be relied on for dietary or medical decisions.

Please cite as: *HungryBear UC Dining Menus Dataset, JeremyL691, 2026,
https://github.com/JeremyL691/HungryBear/tree/dataset/dataset*.
