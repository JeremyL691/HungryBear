#!/usr/bin/env python3
"""Build the HungryBear UC dining-menu dataset from the full git history of the `data` branch.

The HungryBear scraper publishes one JSON file per campus per day (v1/<campus>/<date>.json) on its
`data` branch, but only keeps a rolling window of about 14 days. Older days survive only in git
history, so this script walks EVERY commit reachable from the data branch, reads every distinct
version of every menu file, and unions them into one tidy table.

Usage:
    git clone https://github.com/JeremyL691/HungryBear.git
    python scripts/build_dataset.py /path/to/HungryBear [--ref origin/data] [--out <dataset dir>]

The clone is only read (git rev-list / ls-tree / cat-file); nothing in it is modified.
Output is deterministic for a given clone state.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------------------------
# Vocabularies (mirrors hungrybear/models.py, plus aliases for defensive spelling cleanup)
# --------------------------------------------------------------------------------------------
MEAL_ORDER = ["Breakfast", "Brunch", "Lunch", "Snack", "Dinner", "Late Night", "All Day"]
MEAL_ALIASES = {m.lower().replace(" ", ""): m for m in MEAL_ORDER}
MEAL_ALIASES.update({"latenight": "Late Night", "late-night": "Late Night", "allday": "All Day",
                     "snacks": "Snack"})

TAG_ORDER = ["vegan", "vegetarian", "halal", "kosher", "gluten_free"]
TAG_ALIASES = {"gluten-free": "gluten_free", "glutenfree": "gluten_free", "gf": "gluten_free",
               "veg": "vegetarian", "vegetarian_": "vegetarian"}

ALLERGEN_ORDER = ["milk", "egg", "fish", "shellfish", "tree_nuts", "peanuts", "wheat", "soy",
                  "sesame", "gluten", "pork", "alcohol", "coconut"]
ALLERGEN_ALIASES = {"dairy": "milk", "eggs": "egg", "tree nuts": "tree_nuts", "treenuts": "tree_nuts",
                    "tree_nut": "tree_nuts", "peanut": "peanuts", "soybean": "soy", "soybeans": "soy",
                    "shell_fish": "shellfish", "crustacean_shellfish": "shellfish"}

CAMPUS_ORDER = ["ucb", "ucd", "uci", "ucla", "ucm", "ucr", "ucsb", "ucsc", "ucsd"]
CAMPUS_NAMES = {  # fallback if index.json lacks a campus
    "ucb": "UC Berkeley", "ucd": "UC Davis", "uci": "UC Irvine", "ucla": "UCLA", "ucm": "UC Merced",
    "ucr": "UC Riverside", "ucsb": "UC Santa Barbara", "ucsc": "UC Santa Cruz", "ucsd": "UC San Diego",
}

PATH_RE = re.compile(r"^v1/([a-z]+)/(\d{4}-\d{2}-\d{2})\.json$")
PUNCT_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'", "`": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
    "\u2013": "-", "\u2014": "-", "\u2012": "-", "\u2212": "-", "\u00a0": " ", "\u200b": "",
    "\u00ae": "", "\u2122": "",
})


# --------------------------------------------------------------------------------------------
# Text normalization
# --------------------------------------------------------------------------------------------
def clean_text(s: str | None) -> str:
    """Unicode NFKC, ASCII quotes/dashes, drop (R)/TM marks, collapse whitespace."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).translate(PUNCT_MAP)
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C" or ch in " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([,;:)\]])", r"\1", s)   # "Rice , Beans" -> "Rice, Beans"
    s = re.sub(r"([(\[])\s+", r"\1", s)
    return s


def dish_key(clean_name: str) -> str:
    """Case/spacing-insensitive matching key for a dish name."""
    k = clean_name.casefold()
    k = re.sub(r"\s*&\s*", " & ", k)
    k = re.sub(r"\s*/\s*", "/", k)
    k = re.sub(r"\s+", " ", k).strip(" .*-")
    return k


def dish_id_for(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def norm_vocab(values, aliases, order) -> list[str]:
    out = set()
    for v in values or []:
        t = clean_text(v).lower().replace("-", "_").strip()
        t = aliases.get(t, aliases.get(t.replace("_", " "), t))
        if t:
            out.add(t)
    known = [t for t in order if t in out]
    return known + sorted(out - set(order))


def norm_meal(name: str) -> str:
    c = clean_text(name)
    return MEAL_ALIASES.get(c.lower().replace(" ", ""), c.title())


# --------------------------------------------------------------------------------------------
# Git access (read-only)
# --------------------------------------------------------------------------------------------
def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout


class BlobReader:
    def __init__(self, repo: Path):
        self.p = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def read(self, sha: str) -> bytes:
        self.p.stdin.write((sha + "\n").encode())
        self.p.stdin.flush()
        hdr = self.p.stdout.readline().split()
        if len(hdr) < 3 or hdr[1] == b"missing":
            raise RuntimeError(f"missing blob {sha}")
        data = self.p.stdout.read(int(hdr[2]))
        self.p.stdout.read(1)
        return data

    def close(self):
        self.p.stdin.close()
        self.p.wait()


def resolve_ref(repo: Path, ref: str | None) -> str:
    for r in ([ref] if ref else ["origin/data", "data", "refs/remotes/origin/data"]):
        try:
            git(repo, "rev-parse", "--verify", "--quiet", r + "^{commit}")
            return r
        except subprocess.CalledProcessError:
            continue
    sys.exit(f"could not find the data branch (tried {ref or 'origin/data, data'}); "
             "clone with all branches or pass --ref")


# --------------------------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------------------------
def collect(repo: Path, ref: str):
    # Every commit reachable from the data branch (merge parents included), oldest first.
    # Ordering: committer timestamp, then topological position as a tie-breaker.
    lines = git(repo, "rev-list", "--topo-order", "--reverse", "--format=%H %ct", ref).splitlines()
    commits = []
    for ln in lines:
        if ln.startswith("commit "):
            continue
        sha, ct = ln.split()
        commits.append((int(ct), len(commits), sha))
    commits.sort()
    order = {sha: i for i, (_, _, sha) in enumerate(commits)}

    # (path, blob) -> first/last commit index containing it
    first_ix, last_ix = {}, {}
    path_latest = {}                      # path -> (commit index, blob) of the newest version
    index_blob = None
    for ct, _, sha in commits:
        i = order[sha]
        for ln in git(repo, "ls-tree", "-r", sha, "v1").splitlines():
            meta, path = ln.split("\t", 1)
            blob = meta.split()[2]
            if path == "v1/index.json":
                index_blob = blob        # ends as the newest index.json
                continue
            if not PATH_RE.match(path):
                continue
            k = (path, blob)
            first_ix.setdefault(k, i)
            last_ix[k] = i
            if path not in path_latest or path_latest[path][0] <= i:
                path_latest[path] = (i, blob)
    return commits, first_ix, last_ix, path_latest, index_blob


def campus_names(reader: BlobReader, index_blob: str | None) -> dict:
    names = dict(CAMPUS_NAMES)
    if index_blob:
        try:
            for c in json.loads(reader.read(index_blob)).get("campuses", []):
                names[c["id"]] = clean_text(c.get("name")) or names.get(c["id"], c["id"])
        except Exception:
            pass
    return names


def records_from_blob(doc: dict):
    """Yield one merged record per (location, meal, dish_key) in a single snapshot file."""
    out = {}
    for loc in doc.get("locations") or []:
        for meal in loc.get("meals") or []:
            meal_name = norm_meal(meal.get("name", ""))
            for st in meal.get("stations") or []:
                station = clean_text(st.get("name"))
                for it in st.get("items") or []:
                    raw = it.get("name") or ""
                    clean = clean_text(raw)
                    if not clean:
                        continue
                    key = (loc.get("id"), meal_name, dish_key(clean))
                    tags = norm_vocab(it.get("tags"), TAG_ALIASES, TAG_ORDER)
                    alls = norm_vocab(it.get("allergens"), ALLERGEN_ALIASES, ALLERGEN_ORDER)
                    cal = it.get("calories")
                    cal = int(cal) if isinstance(cal, (int, float)) and not isinstance(cal, bool) else None
                    desc = clean_text(it.get("description")) or None
                    r = out.get(key)
                    if r is None:
                        out[key] = dict(
                            hall_id=loc.get("id"), dining_hall=clean_text(loc.get("name")),
                            hall_type=loc.get("type"), meal=meal_name,
                            meal_start=meal.get("start"), meal_end=meal.get("end"),
                            meal_note=clean_text(meal.get("note")) or None,
                            stations=[station] if station else [], dish_name_raw=raw,
                            dish_clean=clean, tags=tags, allergens=alls, calories=cal,
                            description=desc, n_listings=1, calorie_values={cal} - {None},
                        )
                    else:  # same dish listed twice in one meal (e.g. at two stations): merge
                        if station and station not in r["stations"]:
                            r["stations"].append(station)
                        r["tags"] = norm_vocab(r["tags"] + tags, {}, TAG_ORDER)
                        r["allergens"] = norm_vocab(r["allergens"] + alls, {}, ALLERGEN_ORDER)
                        if r["calories"] is None:
                            r["calories"] = cal
                        r["calorie_values"] |= {cal} - {None}
                        r["description"] = r["description"] or desc
                        r["n_listings"] += 1
    return out


def build_rows(repo: Path, ref: str, log):
    commits, first_ix, last_ix, path_latest, index_blob = collect(repo, ref)
    commit_time = [ct for ct, _, _ in commits]
    log(f"commits on {ref}: {len(commits)}; distinct menu-file versions: {len(first_ix)}; "
        f"distinct (campus, date) files: {len(path_latest)}")
    reader = BlobReader(repo)
    names = campus_names(reader, index_blob)

    final = {}        # full key -> record (latest version wins)
    first_cap = {}    # full key -> earliest fetched_at in which the record appeared
    n_versions = Counter()
    stats = Counter()
    in_latest = set()
    # Process file versions oldest -> newest (by the LAST commit that carried each version), so a
    # later correction of the same record overwrites the earlier one.
    for (path, blob), li in sorted(last_ix.items(), key=lambda kv: (kv[1], kv[0])):
        m = PATH_RE.match(path)
        campus, day = m.group(1), m.group(2)
        doc = json.loads(reader.read(blob))
        stats["file_versions"] += 1
        if doc.get("status") != "ok":
            stats["file_versions_not_ok"] += 1
        fetched = doc.get("fetched_at") or pd.Timestamp(commit_time[li], unit="s", tz="UTC").isoformat()
        recs = records_from_blob(doc)
        is_latest = path_latest[path][1] == blob
        for (hall, meal, dk), r in recs.items():
            stats["raw_listings"] += r["n_listings"]
            stats["snapshot_records"] += 1
            k = (day, campus, hall, meal, dk)
            n_versions[k] += 1
            if k not in first_cap or fetched < first_cap[k]:
                first_cap[k] = fetched
            if k in final and final[k]["_sig"] != _sig(r):
                stats["records_corrected_by_later_snapshot"] += 1
            r.update(date=day, campus_id=campus, campus=names.get(campus, campus),
                     last_captured_at=fetched, source_url=doc.get("source_url"), _sig=_sig(r))
            final[k] = r
            if is_latest:
                in_latest.add(k)
    reader.close()
    for k, r in final.items():
        r["first_captured_at"] = first_cap[k]
        r["n_snapshot_versions"] = n_versions[k]
        r["in_latest_snapshot"] = k in in_latest
        r["_dish_key"] = k[4]
    stats["commits"] = len(commits)
    stats["first_commit_utc"] = pd.Timestamp(commit_time[0], unit="s", tz="UTC").isoformat()
    stats["last_commit_utc"] = pd.Timestamp(commit_time[-1], unit="s", tz="UTC").isoformat()
    stats["distinct_campus_date_files"] = len(path_latest)
    return list(final.values()), stats


def _sig(r):
    return (r["dish_clean"], tuple(r["stations"]), tuple(r["tags"]), tuple(r["allergens"]),
            r["calories"], r["description"], r["meal_start"], r["meal_end"])


# --------------------------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------------------------
MENU_COLUMNS = [
    "date", "weekday", "campus_id", "campus", "hall_id", "dining_hall", "hall_type", "meal",
    "meal_start", "meal_end", "meal_note", "station", "dish_id", "dish_name", "dish_name_raw",
    "tags", "allergens", "calories", "description", "is_vegan", "is_vegetarian", "is_halal",
    "is_kosher", "is_gluten_free", "tags_reported_by_campus", "allergens_reported_by_campus",
    "calories_reported_by_campus", "n_listings", "n_snapshot_versions", "in_latest_snapshot",
    "first_captured_at", "last_captured_at", "source_url",
]


def make_tables(rows):
    df = pd.DataFrame(rows)
    # Canonical display name per dish key: most frequent cleaned spelling (ties -> alphabetical).
    variants = df.groupby(["_dish_key", "dish_clean"]).size().reset_index(name="n")
    variants = variants.sort_values(["_dish_key", "n", "dish_clean"], ascending=[True, False, True])
    canon = variants.drop_duplicates("_dish_key").set_index("_dish_key")["dish_clean"]
    n_variants = variants.groupby("_dish_key").size()
    df["dish_name"] = df["_dish_key"].map(canon)
    df["dish_id"] = df["_dish_key"].map(dish_id_for)

    # Which campuses ever report each field? If a campus never does, the field is NA (unknown)
    # for all its rows; otherwise an empty list means "none listed by the source".
    rep = {
        "tags": df.groupby("campus_id")["tags"].apply(lambda s: any(len(x) for x in s)),
        "allergens": df.groupby("campus_id")["allergens"].apply(lambda s: any(len(x) for x in s)),
        "calories": df.groupby("campus_id")["calories"].apply(lambda s: s.notna().any()),
    }
    for f in ("tags", "allergens", "calories"):
        df[f"{f}_reported_by_campus"] = df["campus_id"].map(rep[f]).astype(bool)
    for t in TAG_ORDER:
        col = "is_" + t
        df[col] = df["tags"].apply(lambda x, t=t: t in x).astype("boolean")
        df.loc[~df["tags_reported_by_campus"], col] = pd.NA
    df["tags"] = df["tags"].apply("|".join)
    df["allergens"] = df["allergens"].apply("|".join)
    df.loc[~df["tags_reported_by_campus"], "tags"] = pd.NA
    df.loc[~df["allergens_reported_by_campus"], "allergens"] = pd.NA
    df["station"] = df["stations"].apply(lambda x: "|".join(x) if x else None)
    df["calories"] = df["calories"].astype("Int64")
    df["weekday"] = pd.to_datetime(df["date"]).dt.day_name()
    df["n_listings"] = df["n_listings"].astype("int16")
    df["n_snapshot_versions"] = df["n_snapshot_versions"].astype("int32")
    multi_cal = int((df["calorie_values"].apply(len) > 1).sum())

    meal_rank = {m: i for i, m in enumerate(MEAL_ORDER)}
    df["_mr"] = df["meal"].map(meal_rank).fillna(99)
    df = df.sort_values(["date", "campus_id", "hall_id", "_mr", "meal", "dish_name", "_dish_key"],
                        kind="mergesort").reset_index(drop=True)
    for c in ("meal_start", "meal_end", "meal_note", "description", "station", "source_url"):
        df[c] = df[c].astype("string")
    menus = df[MENU_COLUMNS].copy()
    for c in ("date", "weekday", "campus_id", "campus", "hall_id", "dining_hall", "hall_type", "meal",
              "dish_id", "dish_name", "dish_name_raw", "tags", "allergens",
              "first_captured_at", "last_captured_at"):
        menus[c] = menus[c].astype("string")

    # ---- dishes dimension -------------------------------------------------------------------
    g = df.groupby("dish_id", sort=True)
    def union(col, order):
        def f(s):
            vals = set()
            for x in s.dropna():
                vals.update(v for v in x.split("|") if v)
            return "|".join([v for v in order if v in vals] + sorted(vals - set(order)))
        return f
    dishes = pd.DataFrame({
        "dish_id": g.size().index,
        "dish_name": g["dish_name"].first().values,
        "n_appearances": g.size().values,
        "n_days": g["date"].nunique().values,
        "first_seen": g["date"].min().values,
        "last_seen": g["date"].max().values,
        "n_campuses": g["campus_id"].nunique().values,
        "campuses": g["campus_id"].apply(lambda s: "|".join(sorted(set(s)))).values,
        "n_halls": df.drop_duplicates(["dish_id", "campus_id", "hall_id"])
                     .groupby("dish_id", sort=True).size().values,
        "meals": g["meal"].apply(lambda s: "|".join(m for m in MEAL_ORDER if m in set(s))).values,
        "tags_any": g["tags"].apply(union("tags", TAG_ORDER)).values,
        "allergens_any": g["allergens"].apply(union("allergens", ALLERGEN_ORDER)).values,
        "calories_median": g["calories"].median().round().values,
        "n_name_variants": g["_dish_key"].first().map(n_variants).values,
    })
    dishes["calories_median"] = pd.array(dishes["calories_median"], dtype="Int64")
    for c in ("tags_any", "allergens_any"):
        dishes.loc[dishes[c] == "", c] = pd.NA
    dishes = dishes.sort_values(["n_appearances", "dish_id"], ascending=[False, True]).reset_index(drop=True)
    for c in ("dish_id", "dish_name", "first_seen", "last_seen", "campuses", "meals", "tags_any",
              "allergens_any"):
        dishes[c] = dishes[c].astype("string")
    dishes["n_appearances"] = dishes["n_appearances"].astype("int32")
    return menus, dishes, {"rows_with_conflicting_calories_merged": multi_cal}


# --------------------------------------------------------------------------------------------
# Output helpers (deterministic)
# --------------------------------------------------------------------------------------------
def write_csv_gz(df, path: Path):
    buf = io.StringIO()
    df.to_csv(buf, index=False, lineterminator="\n")
    with open(path, "wb") as fh, gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0,
                                                compresslevel=9) as gz:
        gz.write(buf.getvalue().encode("utf-8"))


def write_parquet(df, path: Path):
    df.to_parquet(path, index=False, engine="pyarrow", compression="zstd")


def coverage_table(menus):
    rows = []
    for cid in sorted(menus["campus_id"].unique(), key=lambda c: (CAMPUS_ORDER + [c]).index(c)):
        s = menus[menus["campus_id"] == cid]
        rows.append({
            "campus_id": cid, "campus": s["campus"].iloc[0],
            "first_date": s["date"].min(), "last_date": s["date"].max(),
            "days_with_menus": s["date"].nunique(), "rows": len(s),
            "halls": s["hall_id"].nunique(), "unique_dishes": s["dish_id"].nunique(),
            "pct_with_calories": round(100 * s["calories"].notna().mean(), 1),
            "pct_with_allergens": round(100 * s["allergens"].fillna("").ne("").mean(), 1),
            "pct_with_tags": round(100 * s["tags"].fillna("").ne("").mean(), 1),
        })
    return pd.DataFrame(rows)


def make_charts(menus, outdir: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    outdir.mkdir(parents=True, exist_ok=True)
    piv = (menus.groupby(["date", "campus"]).size().unstack(fill_value=0).sort_index())
    piv.index = pd.to_datetime(piv.index)
    piv = piv.reindex(pd.date_range(piv.index.min(), piv.index.max()), fill_value=0)
    import matplotlib.dates as mdates
    fig, ax = plt.subplots(figsize=(11, 4.8), dpi=110)
    colors = plt.get_cmap("tab10").colors
    ax.stackplot(piv.index, *[piv[c].values for c in piv.columns], labels=list(piv.columns),
                 colors=colors[:len(piv.columns)], alpha=0.9, linewidth=0)
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.set_xlim(piv.index.min(), piv.index.max())
    ax.set_title("HungryBear: dish appearances per day, by campus")
    ax.set_ylabel("rows (dish appearances)")
    ax.set_xlabel("menu date")
    ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), frameon=False, fontsize=9)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(outdir / "rows_per_campus_over_time.png", metadata={"Software": None})
    plt.close(fig)

    cov = coverage_table(menus).set_index("campus")
    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=110)
    cov[["pct_with_tags", "pct_with_allergens", "pct_with_calories"]].rename(columns={
        "pct_with_tags": "diet tags", "pct_with_allergens": "allergens",
        "pct_with_calories": "calories"}).plot.bar(ax=ax, width=0.8)
    ax.set_ylabel("% of rows with field present")
    ax.set_ylim(0, 105)
    ax.set_xlabel("")
    ax.set_title("Field completeness by campus")
    ax.grid(axis="y", alpha=0.3)
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(outdir / "field_completeness_by_campus.png", metadata={"Software": None})
    plt.close(fig)


# --------------------------------------------------------------------------------------------
# Sanity checks
# --------------------------------------------------------------------------------------------
def sanity_checks(menus, dishes, stats):
    key = ["date", "campus_id", "hall_id", "meal", "dish_id"]
    checks = {}
    dup = int(menus.duplicated(key).sum())
    checks["duplicate_keys"] = dup
    assert dup == 0, f"{dup} duplicate (date, campus, hall, meal, dish) keys"
    d = pd.to_datetime(menus["date"])
    assert d.min() >= pd.Timestamp("2025-01-01") and d.max() <= pd.Timestamp("2028-01-01"), "odd dates"
    last_commit = pd.Timestamp(stats["last_commit_utc"]).tz_convert(None)
    ahead = (d - last_commit.normalize()).dt.days.max()
    checks["max_days_ahead_of_last_commit"] = int(ahead)
    assert ahead <= 10, "menu dates far in the future"
    assert menus["dish_name"].str.len().min() > 0
    assert (menus["calories"].dropna() >= 0).all()
    assert menus["dish_id"].nunique() == len(dishes)
    assert int(dishes["n_appearances"].sum()) == len(menus)
    assert set(menus["meal"]) <= set(MEAL_ORDER), set(menus["meal"]) - set(MEAL_ORDER)
    for col, vocab in (("tags", TAG_ORDER), ("allergens", ALLERGEN_ORDER)):
        seen = {v for x in menus[col].dropna() for v in x.split("|") if v}
        checks[f"unknown_{col}"] = sorted(seen - set(vocab))
        assert not checks[f"unknown_{col}"], f"unknown {col}: {checks[f'unknown_{col}']} (add an alias)"
    checks["calories_equal_zero"] = int((menus["calories"] == 0).sum())
    return checks


# --------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", type=Path, help="path to a clone of JeremyL691/HungryBear")
    ap.add_argument("--ref", default=None, help="data branch ref (default: origin/data, then data)")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent,
                    help="dataset folder to write into (default: the folder containing scripts/)")
    ap.add_argument("--sample-rows", type=int, default=200)
    ap.add_argument("--no-readme", action="store_true", help="do not regenerate README.md")
    args = ap.parse_args()
    t0 = time.time()
    log = lambda m: print(f"[{time.time() - t0:6.1f}s] {m}", flush=True)

    repo = args.repo.resolve()
    ref = resolve_ref(repo, args.ref)
    rows, stats = build_rows(repo, ref, log)
    log(f"merged records: {len(rows):,}")
    menus, dishes, extra = make_tables(rows)
    stats.update(extra)
    checks = sanity_checks(menus, dishes, stats)
    log(f"sanity checks passed: {checks}")

    out = args.out.resolve()
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    write_csv_gz(menus, data / "hungrybear_menus.csv.gz")
    write_parquet(menus, data / "hungrybear_menus.parquet")
    write_csv_gz(dishes, data / "hungrybear_dishes.csv.gz")
    write_parquet(dishes, data / "hungrybear_dishes.parquet")
    # Preview sample: equal share per campus, fixed seed, sorted like the full table.
    per = max(1, args.sample_rows // menus["campus_id"].nunique())
    parts = [s.sample(min(per, len(s)), random_state=42)
             for _, s in menus.groupby("campus_id", sort=True)]
    sample = pd.concat(parts).sort_index()
    sample.to_csv(data / "sample_menus.csv", index=False, lineterminator="\n")
    cov = coverage_table(menus)
    cov.to_csv(data / "coverage_by_campus.csv", index=False, lineterminator="\n")
    make_charts(menus, out / "charts")

    summary = {
        "source_repo": "https://github.com/JeremyL691/HungryBear",
        "source_ref": ref,
        "source_head": git(repo, "rev-parse", ref).strip(),
        "rows": len(menus),
        "unique_dishes": len(dishes),
        "date_min": menus["date"].min(),
        "date_max": menus["date"].max(),
        "campuses": int(menus["campus_id"].nunique()),
        "halls": int(menus[["campus_id", "hall_id"]].drop_duplicates().shape[0]),
        "stats": {k: (int(v) if isinstance(v, (int,)) else v) for k, v in stats.items()},
        "checks": checks,
    }
    (data / "build_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    for f in sorted(data.iterdir()):
        size = f.stat().st_size
        if size > 50 * 1024 * 1024:
            log(f"WARNING: {f.name} is {size / 1e6:.1f} MB (> 50 MB)")
    if not args.no_readme:
        render_readme(out, summary, cov, menus, dishes)
    log(f"done: {len(menus):,} rows, {len(dishes):,} dishes, "
        f"{summary['date_min']} .. {summary['date_max']} -> {out}")


# README rendering lives at the bottom so the template is easy to edit.
def render_readme(out: Path, summary, cov, menus, dishes):
    tmpl_path = Path(__file__).resolve().parent / "README.template.md"
    tmpl = tmpl_path.read_text()
    covmd = ["| Campus | Dates covered | Days | Rows | Halls | Unique dishes | % w/ calories | % w/ allergens | % w/ diet tags |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in cov.itertuples():
        covmd.append(f"| {r.campus} (`{r.campus_id}`) | {r.first_date} to {r.last_date} | {r.days_with_menus} | "
                     f"{r.rows:,} | {r.halls} | {r.unique_dishes:,} | {r.pct_with_calories:g} | "
                     f"{r.pct_with_allergens:g} | {r.pct_with_tags:g} |")
    covmd.append(f"| **All** | {summary['date_min']} to {summary['date_max']} | "
                 f"{menus['date'].nunique()} | **{summary['rows']:,}** | {summary['halls']} | "
                 f"**{summary['unique_dishes']:,}** | "
                 f"{100 * menus['calories'].notna().mean():.1f} | "
                 f"{100 * menus['allergens'].fillna('').ne('').mean():.1f} | "
                 f"{100 * menus['tags'].fillna('').ne('').mean():.1f} |")
    files = []
    for name, desc in [("hungrybear_menus.csv.gz", "Main table, one row per dish appearance (gzip CSV)"),
                       ("hungrybear_menus.parquet", "Same table as Parquet (typed, zstd)"),
                       ("hungrybear_dishes.csv.gz", "Dish dimension table, one row per unique dish"),
                       ("hungrybear_dishes.parquet", "Same as Parquet"),
                       ("sample_menus.csv", f"{len(pd.read_csv(out / 'data' / 'sample_menus.csv')):,}-row preview, readable on GitHub"),
                       ("coverage_by_campus.csv", "The coverage table below as CSV"),
                       ("build_summary.json", "Build provenance (source commit) and sanity-check results")]:
        size = (out / "data" / name).stat().st_size
        files.append(f"| [`data/{name}`](data/{name}) | {size / 1e6:.1f} MB | {desc} |"
                     if size >= 100_000 else
                     f"| [`data/{name}`](data/{name}) | {size / 1e3:.0f} KB | {desc} |")
    top = dishes.head(10)
    topmd = ["| Dish | Appearances | Campuses |", "|---|---:|---:|"]
    topmd += [f"| {r.dish_name} | {r.n_appearances:,} | {r.n_campuses} |" for r in top.itertuples()]
    s = summary["stats"]
    repl = {
        "{{ROWS}}": f"{summary['rows']:,}",
        "{{DISHES}}": f"{summary['unique_dishes']:,}",
        "{{DATE_MIN}}": summary["date_min"],
        "{{DATE_MAX}}": summary["date_max"],
        "{{N_DAYS}}": str(menus["date"].nunique()),
        "{{HALLS}}": str(summary["halls"]),
        "{{COMMITS}}": f"{s['commits']:,}",
        "{{FILE_VERSIONS}}": f"{s['file_versions']:,}",
        "{{RAW_LISTINGS}}": f"{s['raw_listings']:,}",
        "{{CORRECTED}}": f"{s.get('records_corrected_by_later_snapshot', 0):,}",
        "{{SOURCE_HEAD}}": summary["source_head"][:10],
        "{{LAST_COMMIT}}": s["last_commit_utc"][:16].replace("T", " ") + " UTC",
        "{{COVERAGE_TABLE}}": "\n".join(covmd),
        "{{FILES_TABLE}}": "\n".join(files),
        "{{TOP_DISHES}}": "\n".join(topmd),
        "{{NOT_IN_LATEST}}": f"{int((~menus['in_latest_snapshot']).sum()):,}",
        "{{MULTI_STATION}}": f"{int((menus['n_listings'] > 1).sum()):,}",
    }
    for k, v in repl.items():
        tmpl = tmpl.replace(k, v)
    (out / "README.md").write_text(tmpl)


if __name__ == "__main__":
    main()
