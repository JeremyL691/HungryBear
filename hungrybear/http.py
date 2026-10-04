# hungrybear/http.py
"""HTTP layer for adapters, with record/replay.

Adapters never call httpx directly - they go through a Fetcher. That gives us:
- one place for UA, timeouts, retries, and politeness delays
- `RecordingFetcher`: saves every response to a directory (the raw snapshot attached to
  a "scraper broken" issue, and the fixture the autofix agent reproduces against)
- `ReplayFetcher`: serves those saved responses in tests, with no network
"""

from __future__ import annotations

import gzip
import hashlib
import json
import time
from pathlib import Path
from typing import Dict, Mapping, Optional

import httpx

USER_AGENT = (
    "Mozilla/5.0 (compatible; HungryBear/2.0; +https://github.com/JeremyL691/HungryBear) UC dining menu aggregator"
)
# Some sites (Akamai-fronted) reject non-browser UAs; adapters can opt into this one.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class FetchError(RuntimeError):
    pass


def request_key(method: str, url: str, params: Optional[Mapping] = None, data: Optional[Mapping] = None) -> str:
    """Stable key for a request, used to name recorded responses."""
    payload = json.dumps(
        {"m": method.upper(), "u": url, "p": dict(params or {}), "d": dict(data or {})},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


class Fetcher:
    """Live fetcher (sync). One instance per campus run so cookies stay per-site."""

    def __init__(self, timeout: float = 30.0, retries: int = 3, min_interval: float = 0.3) -> None:
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        )
        self._retries = retries
        self._min_interval = min_interval
        self._last = 0.0

    def close(self) -> None:
        self._client.close()

    def _send(self, method: str, url: str, *, params=None, data=None, headers=None) -> str:
        last_err = ""
        for attempt in range(1, self._retries + 1):
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self._client.request(method, url, params=params, data=data, headers=headers)
            except httpx.HTTPError as e:
                last_err = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code < 400:
                    return resp.text
                last_err = f"HTTP {resp.status_code}"
                # Other 4xx (403 blocked, 404 moved) won't fix themselves on retry.
                if resp.status_code < 500 and resp.status_code != 429:
                    break
            time.sleep(0.8 * attempt)
        raise FetchError(f"{method} {url} failed: {last_err}")

    def get(self, url: str, *, params: Optional[Mapping] = None, headers: Optional[Mapping] = None) -> str:
        return self._send("GET", url, params=params, headers=headers)

    def post(self, url: str, *, data: Optional[Mapping] = None, headers: Optional[Mapping] = None) -> str:
        return self._send("POST", url, data=data, headers=headers)

    def get_json(self, url: str, *, params: Optional[Mapping] = None, headers: Optional[Mapping] = None):
        return json.loads(self.get(url, params=params, headers=headers))


class RecordingFetcher(Fetcher):
    """Live fetcher that also writes each response to `directory` (gzip) + an index.json."""

    def __init__(self, directory: Path, **kw) -> None:
        super().__init__(**kw)
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.dir / "index.json"
        self._index: Dict[str, dict] = json.loads(self._index_path.read_text()) if self._index_path.exists() else {}

    def _send(self, method: str, url: str, *, params=None, data=None, headers=None) -> str:
        text = super()._send(method, url, params=params, data=data, headers=headers)
        key = request_key(method, url, params, data)
        (self.dir / f"{key}.gz").write_bytes(gzip.compress(text.encode(), mtime=0))
        self._index[key] = {"method": method, "url": url, "params": dict(params or {}), "data": dict(data or {})}
        self._index_path.write_text(json.dumps(self._index, indent=1, sort_keys=True))
        return text


class ReplayFetcher(Fetcher):
    """Serves responses recorded by RecordingFetcher. Unknown requests raise FetchError."""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self._client = None  # type: ignore[assignment]

    def close(self) -> None:
        pass

    def _send(self, method: str, url: str, *, params=None, data=None, headers=None) -> str:
        key = request_key(method, url, params, data)
        path = self.dir / f"{key}.gz"
        if not path.exists():
            raise FetchError(f"No recorded response for {method} {url} params={params} data={data}")
        return gzip.decompress(path.read_bytes()).decode()
