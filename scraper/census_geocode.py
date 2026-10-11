"""Street-level coordinates for SEC-sourced firms via the US Census Geocoder.

Why this exists: the monthly refresh runs ``--enrich sec_bulk`` without any
geocoder, so every SEC firm lands on its city centroid (``CITY_TO_LATLNG``)
and 642 of the 695 map pins shared a coordinate — most firms could never be
told apart on the map. Nominatim denies the placeholder User-Agent an
unconfigured checkout sends (its policy wants a reachable contact), while the
Census Bureau's batch geocoder takes up to 10,000 US addresses per request,
needs no key or contact, and returns street-level points. Every address here
is a US one, so it is the better tool regardless.

    uv run python -m scraper.census_geocode --dry-run   # hit rate only
    uv run python -m scraper.census_geocode             # write both firms.json

Addresses the Census parser rejects (Sand Hill Road "BUILDING 2, SUITE
110", El Camino Real suites, piers) go to OpenStreetMap Nominatim as a
fallback — but only when ``SCRAPER_CONTACT`` is a real address, because
Nominatim's policy requires a reachable contact in the User-Agent and it
denies the placeholder outright. Area-level results (a city, a postcode)
are rejected so a miss never silently turns back into a centroid.

Firms that resolve get ``geo_source`` ("census" or "nominatim") and
``geo_match`` ("Exact" / "Non_Exact"), which ``build.carry_forward_enrichment``
uses to keep the point across later refreshes that only know the centroid.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import re
from typing import Optional

import httpx

from scraper.build import GEOCODE_CACHE, OUT_PATH, load_previous, write_outputs
from scraper.geocode import Geocoder
from scraper.useragent import DEFAULT_CONTACT, USER_AGENT

log = logging.getLogger(__name__)

CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
BENCHMARK = "Public_AR_Current"
GEO_SOURCE = "census"
NOMINATIM_SOURCE = "nominatim"
STREET_LEVEL_SOURCES = (GEO_SOURCE, NOMINATIM_SOURCE)

#: Nominatim ``addresstype`` values that mean "an area, not an address".
#: A free-form query for an address it cannot parse often resolves to the
#: city instead; accepting that would put the firm right back on a centroid.
_AREA_TYPES = frozenset({
    "city", "town", "village", "hamlet", "suburb", "neighbourhood", "quarter",
    "locality", "county", "state", "country", "postcode", "boundary",
    "administrative", "residential", "island", "water",
})
#: Matches that land on the street rather than the building.
_ROAD_TYPES = frozenset({"road", "street", "highway", "pedestrian", "path", "footway"})

# Unit designators the Census parser trips over when they sit inside the
# street field ("3000 SAND HILL ROAD BUILDING 2, SUITE 110"). A retry without
# them still lands on the building. BUILDING/BLDG only count when followed by
# a number or a single letter, so "1 FERRY BUILDING" keeps its name.
_UNIT_RE = re.compile(
    r"\b(?:SUITE|STE\.?|UNIT|FLOOR|FL\.?|PMB|ROOM|RM\.?)\s*[:#]?\s*(?:[\w-]+)?\.?",
    re.IGNORECASE,
)
_BLDG_RE = re.compile(r"\b(?:BUILDING|BLDG\.?)\s*[:#]?\s*(?:[A-Z]|\d[\w-]*)\b\.?", re.IGNORECASE)
_ORDINAL_FLOOR_RE = re.compile(r"\b\d+(?:ST|ND|RD|TH)\s+FLOOR\b", re.IGNORECASE)
_HASH_UNIT_RE = re.compile(r"#\s*[\w-]+")
#: Whole comma-segments that are not part of the street: care-of lines and
#: bare building-suite codes such as "2-130".
_DROP_SEGMENT_RE = re.compile(r"^(?:C/O\b|\d+-\d+$)", re.IGNORECASE)
#: "… SUITE 100 C/O CANOPY MENLO PARK": everything from the care-of on.
_CARE_OF_RE = re.compile(r"\bC/O\b.*$", re.IGNORECASE)
#: "3000 SAND HILL ROAD 3-150": a building-suite code glued to the street.
_TRAILING_CODE_RE = re.compile(r"\s+\d+-\d+$")


def parse_address(address: str) -> Optional[tuple[str, str, str, str]]:
    """Split the scraper's ``street[, extra], City, ST ZIP`` form.

    Returns ``(street, city, state, zip)`` or ``None`` when the address does
    not have that shape.
    """
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if len(parts) < 3:
        return None
    m = re.match(r"^([A-Z]{2})\s+(\d{5})", parts[-1])
    if not m:
        return None
    return ", ".join(parts[:-2]), parts[-2], m.group(1), m.group(2)


def simplify_street(street: str) -> str:
    """Drop suite / floor / building tokens so only the street address remains."""
    keep = []
    for raw in street.split(","):
        seg = raw.strip()
        if not seg or _DROP_SEGMENT_RE.match(seg):
            continue
        for pattern in (_CARE_OF_RE, _ORDINAL_FLOOR_RE, _HASH_UNIT_RE, _BLDG_RE, _UNIT_RE, _TRAILING_CODE_RE):
            seg = pattern.sub("", seg)
        seg = re.sub(r"\s{2,}", " ", seg).strip(" ,:")
        if seg:
            keep.append(seg)
    return " ".join(keep)


def geocode_batch(
    rows: list[tuple[str, str, str, str, str]],
    client: Optional[httpx.Client] = None,
) -> dict[str, dict]:
    """Geocode ``(id, street, city, state, zip)`` rows in one request.

    Returns ``{id: {"lat", "lng", "match"}}`` for the rows that matched.
    """
    if not rows:
        return {}
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    own = client is None
    client = client or httpx.Client(timeout=300.0, headers={"User-Agent": USER_AGENT})
    try:
        resp = client.post(
            CENSUS_BATCH_URL,
            data={"benchmark": BENCHMARK},
            files={"addressFile": ("addresses.csv", buf.getvalue(), "text/csv")},
        )
        resp.raise_for_status()
    finally:
        if own:
            client.close()
    out: dict[str, dict] = {}
    for r in csv.reader(io.StringIO(resp.text)):
        # id, input, status, match type, matched address, "lon,lat", tiger id, side
        if len(r) < 6 or r[2] != "Match":
            continue
        lon, lat = r[5].split(",")
        out[r[0]] = {"lat": round(float(lat), 6), "lng": round(float(lon), 6), "match": r[3]}
    return out


def geocode_firms(firms: list[dict], client: Optional[httpx.Client] = None) -> dict[str, dict]:
    """Geocode every firm with a parseable address; retry misses simplified."""
    parsed: dict[str, tuple[str, str, str, str]] = {}
    for f in firms:
        p = parse_address(f.get("address") or "")
        if p:
            parsed[f["id"]] = p
    results = geocode_batch([(fid, *p) for fid, p in parsed.items()], client)
    retry = []
    for fid, (street, city, state, zip5) in parsed.items():
        if fid in results:
            continue
        simple = simplify_street(street)
        if simple and simple != street:
            retry.append((fid, simple, city, state, zip5))
    if retry:
        log.info("Retrying %d unmatched addresses without unit designators", len(retry))
        for fid, hit in geocode_batch(retry, client).items():
            # The unit was dropped, so even an "Exact" hit is the building.
            hit["match"] = "Non_Exact"
            results[fid] = hit
    return results


def nominatim_available() -> bool:
    """True when the User-Agent carries a real contact, as Nominatim requires."""
    return DEFAULT_CONTACT not in USER_AGENT


def nominatim_fallback(firms: list[dict], geocoder: Geocoder) -> dict[str, dict]:
    """Resolve ``firms`` through Nominatim: the full address first, then the
    street without unit designators. Returns ``{id: {lat, lng, match}}``;
    area-level results are dropped, road-level ones count as Non_Exact.
    """
    out: dict[str, dict] = {}
    for f in firms:
        parsed = parse_address(f.get("address") or "")
        if not parsed:
            continue
        street, city, state, zip5 = parsed
        queries = [f.get("address")]
        simple = simplify_street(street)
        if simple and simple != street:
            queries.append(f"{simple}, {city}, {state} {zip5}")
        for i, q in enumerate(queries):
            try:
                coords = geocoder.lookup(q)
            except Exception as e:  # noqa: BLE001 — one bad address must not stop the run
                log.warning("Nominatim failed for %s (%s): %s", f.get("name"), q, e)
                continue
            if coords is None or (coords.kind in _AREA_TYPES):
                continue
            approximate = i > 0 or coords.kind in _ROAD_TYPES or coords.kind is None
            out[f["id"]] = {
                "lat": round(coords.lat, 6),
                "lng": round(coords.lng, 6),
                "match": "Non_Exact" if approximate else "Exact",
                "source": NOMINATIM_SOURCE,
            }
            break
    return out


def apply(firms: list[dict], results: dict[str, dict]) -> int:
    """Write the matched points onto the firms. Returns how many changed."""
    changed = 0
    for f in firms:
        hit = results.get(f["id"])
        if not hit:
            continue
        source = hit.get("source", GEO_SOURCE)
        if f.get("lat") == hit["lat"] and f.get("lng") == hit["lng"] and f.get("geo_source") == source:
            continue
        f["lat"] = hit["lat"]
        f["lng"] = hit["lng"]
        f["geo_source"] = source
        f["geo_match"] = hit["match"]
        changed += 1
    return changed


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="Report the hit rate, write nothing")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Re-geocode every SEC firm, not just those still on a city centroid",
    )
    parser.add_argument(
        "--no-nominatim",
        action="store_true",
        help="Skip the Nominatim fallback for Census misses even if a contact is set",
    )
    args = parser.parse_args()

    payload = load_previous(OUT_PATH)
    firms = payload.get("firms") or []
    if not firms:
        print(f"No firms in {OUT_PATH}; build first.")
        return 1
    candidates = [
        f for f in firms
        if f.get("tier") == "lite" and (args.all or f.get("geo_source") not in STREET_LEVEL_SOURCES)
    ]
    print(f"{len(candidates)} SEC firms to geocode (of {len(firms)})")
    if not candidates:
        return 0
    results = geocode_firms(candidates)
    exact = sum(1 for r in results.values() if r["match"] == "Exact")
    print(f"Census: matched {len(results)} / {len(candidates)} ({exact} exact, {len(results) - exact} to the building)")
    misses = [f for f in candidates if f["id"] not in results]
    if misses and not args.no_nominatim:
        if nominatim_available():
            print(f"Nominatim: trying {len(misses)} Census misses (1 req/s)…")
            geocoder = Geocoder(cache_path=GEOCODE_CACHE)
            try:
                extra = nominatim_fallback(misses, geocoder)
            finally:
                geocoder.close()
            print(f"Nominatim: matched {len(extra)} / {len(misses)}")
            results.update(extra)
            misses = [f for f in candidates if f["id"] not in results]
        else:
            print("Nominatim: skipped — set SCRAPER_CONTACT to a real address to enable it")
    for f in misses[:20]:
        print(f"  no match: {f['name']} — {f['address']}")
    if len(misses) > 20:
        print(f"  … and {len(misses) - 20} more")
    if args.dry_run:
        return 0
    changed = apply(firms, results)
    print(f"updated {changed} firms")
    if changed:
        write_outputs(payload)
        print(f"wrote {OUT_PATH} and the site copy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
