from __future__ import annotations

import json
from pathlib import Path

import httpx

from scraper.geocode import Geocoder


def make_geocoder(tmp_path: Path, response: list[dict]) -> Geocoder:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    g = Geocoder(cache_path=tmp_path / "cache.json", client=http)
    # Bypass the 1-req/sec wall for tests by pretending the last request was ages ago.
    g._last_request = 0.0
    return g


def test_lookup_returns_coords_and_caches(tmp_path: Path) -> None:
    g = make_geocoder(tmp_path, [{"lat": "37.42", "lon": "-122.20"}])
    coords = g.lookup("2800 Sand Hill Rd, Menlo Park, CA")
    assert coords is not None
    assert coords.lat == 37.42 and coords.lng == -122.20
    cache = json.loads((tmp_path / "cache.json").read_text())
    assert cache["2800 Sand Hill Rd, Menlo Park, CA"] == {"lat": 37.42, "lng": -122.20}


def test_lookup_caches_misses(tmp_path: Path) -> None:
    g = make_geocoder(tmp_path, [])
    assert g.lookup("Nowhere") is None
    cache = json.loads((tmp_path / "cache.json").read_text())
    assert cache["Nowhere"] == {"lat": None, "lng": None}


def test_cache_hit_skips_network(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"X": {"lat": 1.0, "lng": 2.0}}))

    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Should not hit the network on a cache hit")

    http = httpx.Client(transport=httpx.MockTransport(fail))
    g = Geocoder(cache_path=cache_path, client=http)
    coords = g.lookup("X")
    assert coords is not None and coords.lat == 1.0
