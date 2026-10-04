# hungrybear/store.py
"""Layout of the published data (= the public JSON API, served by GitHub Pages).

  v1/index.json             campuses, health, available dates
  v1/status.json            per-campus health + consecutive failure counts (drives alerts)
  v1/{campus}/{date}.json   one DayMenu

`LocalStore` is used by the collector (writes into a checkout of the `data` branch).
`RemoteStore` is used by the bot (reads over HTTPS with ETag caching).
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from .models import DayMenu

API_VERSION = "v1"
RETENTION_DAYS = 14


def _dump(obj, compact: bool = False) -> str:
    if compact:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str) + "\n"


class LocalStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root) / API_VERSION
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- day menus ----

    def day_path(self, campus: str, day: date) -> Path:
        return self.root / campus / f"{day.isoformat()}.json"

    def read_day(self, campus: str, day: date) -> Optional[DayMenu]:
        p = self.day_path(campus, day)
        return DayMenu.model_validate_json(p.read_text()) if p.exists() else None

    def write_day(self, menu: DayMenu) -> bool:
        """Write unless only fetched_at changed (keeps data-branch commits meaningful). Returns True if written."""
        p = self.day_path(menu.campus, menu.date)
        new = menu.model_dump(mode="json", exclude_none=True)
        if p.exists():
            old = json.loads(p.read_text())
            if {**old, "fetched_at": None} == {**new, "fetched_at": None}:
                return False
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_dump(new, compact=True))
        return True

    def dates(self, campus: str) -> List[date]:
        d = self.root / campus
        return sorted(date.fromisoformat(p.stem) for p in d.glob("*.json")) if d.exists() else []

    def history_counts(self, campus: str, day: date, weeks: int = 2) -> List[int]:
        """Item totals for the same weekday in previous weeks (for anomaly detection)."""
        from .validate import total_items

        out = []
        for w in range(1, weeks + 1):
            prev = self.read_day(campus, day - timedelta(weeks=w))
            if prev and prev.status == "ok":
                out.append(total_items(prev))
        return out

    def prune(self, campus: str, today: date) -> None:
        cutoff = today - timedelta(days=RETENTION_DAYS)
        for d in self.dates(campus):
            if d < cutoff:
                self.day_path(campus, d).unlink()

    # ---- status / index ----

    def read_json(self, name: str) -> dict:
        p = self.root / name
        return json.loads(p.read_text()) if p.exists() else {}

    def write_json(self, name: str, obj: dict) -> None:
        (self.root / name).write_text(_dump(obj))


class RemoteStore:
    """Read-only HTTP client for the published data, with in-memory ETag cache."""

    def __init__(self, base_url: str, ttl: float = 600.0) -> None:
        self.base = base_url.rstrip("/") + f"/{API_VERSION}"
        self.ttl = ttl
        self._cache: Dict[str, tuple[float, Optional[str], object]] = {}
        self._client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)

    async def _get(self, path: str):
        now = time.monotonic()
        hit = self._cache.get(path)
        if hit and now - hit[0] < self.ttl:
            return hit[2]
        headers = {"If-None-Match": hit[1]} if hit and hit[1] else {}
        try:
            resp = await self._client.get(f"{self.base}/{path}", headers=headers)
        except httpx.HTTPError:
            if hit:  # serve stale rather than nothing
                return hit[2]
            raise
        if resp.status_code == 304 and hit:
            self._cache[path] = (now, hit[1], hit[2])
            return hit[2]
        if resp.status_code == 404:
            self._cache[path] = (now, None, None)
            return None
        resp.raise_for_status()
        data = resp.json()
        self._cache[path] = (now, resp.headers.get("etag"), data)
        return data

    async def index(self) -> dict:
        return await self._get("index.json") or {}

    async def day(self, campus: str, day: date) -> Optional[DayMenu]:
        data = await self._get(f"{campus}/{day.isoformat()}.json")
        return DayMenu.model_validate(data) if data else None


class LocalReadStore:
    """Same async interface as RemoteStore, reading a local directory (dev / running next to the data)."""

    def __init__(self, root: Path) -> None:
        self.local = LocalStore(root)

    async def index(self) -> dict:
        return self.local.read_json("index.json")

    async def day(self, campus: str, day: date) -> Optional[DayMenu]:
        return self.local.read_day(campus, day)
