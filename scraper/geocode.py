"""Address → (lat, lng) via OpenStreetMap Nominatim.

Nominatim's usage policy (https://operations.osmfoundation.org/policies/nominatim/)
caps free use at 1 req/sec and requires a descriptive User-Agent. Results are
cached on disk so a rebuild only hits the network for new addresses.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from scraper.useragent import USER_AGENT as _UA

log = logging.getLogger(__name__)

USER_AGENT = _UA
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
MIN_INTERVAL_SECONDS = 1.1  # respect 1 req/s policy with margin


@dataclass
class Coordinates:
    lat: float
    lng: float


class Geocoder:
    def __init__(self, cache_path: Path, client: Optional[httpx.Client] = None) -> None:
        self.cache_path = cache_path
        self._client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=20.0,
        )
        self._cache: dict[str, dict] = {}
        self._last_request: float = 0.0
        if cache_path.exists():
            self._cache = json.loads(cache_path.read_text())

    def lookup(self, address: str) -> Optional[Coordinates]:
        if address in self._cache:
            entry = self._cache[address]
            if entry.get("lat") is None:
                return None
            return Coordinates(lat=entry["lat"], lng=entry["lng"])

        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_INTERVAL_SECONDS:
            time.sleep(MIN_INTERVAL_SECONDS - elapsed)

        params = {"q": address, "format": "jsonv2", "limit": "1"}
        resp = self._client.get(NOMINATIM_URL, params=params)
        self._last_request = time.monotonic()
        resp.raise_for_status()
        results = resp.json()
        if not results:
            log.warning("No geocoding result for %r", address)
            self._cache[address] = {"lat": None, "lng": None}
            self._save_cache()
            return None
        coords = Coordinates(lat=float(results[0]["lat"]), lng=float(results[0]["lon"]))
        self._cache[address] = {"lat": coords.lat, "lng": coords.lng}
        self._save_cache()
        return coords

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache, indent=2, sort_keys=True))

    def close(self) -> None:
        self._client.close()
