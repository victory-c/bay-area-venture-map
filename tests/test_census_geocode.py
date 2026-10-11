"""scraper.census_geocode: address parsing, the Nominatim fallback, apply()."""

from __future__ import annotations

from scraper.census_geocode import (
    apply,
    nominatim_available,
    nominatim_fallback,
    parse_address,
    simplify_street,
)
from scraper.geocode import Coordinates


def test_parse_address_splits_scraper_form() -> None:
    assert parse_address("3000 SAND HILL ROAD BUILDING 2, SUITE 110, Menlo Park, CA 94025") == (
        "3000 SAND HILL ROAD BUILDING 2, SUITE 110", "Menlo Park", "CA", "94025",
    )
    assert parse_address("Menlo Park, CA 94025") is None
    assert parse_address("") is None


def test_simplify_street_drops_unit_designators() -> None:
    assert simplify_street("3000 SAND HILL ROAD BUILDING 2, SUITE 110") == "3000 SAND HILL ROAD"
    assert simplify_street("601 CALIFORNIA STREET, FLOOR 19 FLOOR 19") == "601 CALIFORNIA STREET"
    assert simplify_street("425 PAGE MILL RD #200") == "425 PAGE MILL RD"
    assert simplify_street("1999 SOUTH BASCOM AVE. SUITE 700, PMB#738") == "1999 SOUTH BASCOM AVE."
    assert simplify_street("1 FERRY BUILDING SUITE 201") == "1 FERRY BUILDING"
    assert simplify_street("3000 SAND HILL ROAD, BLDG. 1, SUITE: 140") == "3000 SAND HILL ROAD"
    assert simplify_street("3000 SAND HILL ROAD, 2-130") == "3000 SAND HILL ROAD"
    assert simplify_street("1300 EL CAMINO REAL, SUITE 100 C/O CANOPY MENLO PARK") == "1300 EL CAMINO REAL"
    assert simplify_street("3000 SAND HILL ROAD 3-150") == "3000 SAND HILL ROAD"
    assert simplify_street("PIER 5 SUITE 102") == "PIER 5"


class StubGeocoder:
    """Answers from a fixed table; records the queries it was asked."""

    def __init__(self, table: dict[str, Coordinates | None]) -> None:
        self.table = table
        self.queries: list[str] = []

    def lookup(self, address: str) -> Coordinates | None:
        self.queries.append(address)
        return self.table.get(address)


def test_nominatim_fallback_prefers_the_full_address() -> None:
    firm = {"id": "a", "name": "A", "address": "405 EL CAMINO REAL SUITE 245, Menlo Park, CA 94025"}
    g = StubGeocoder({firm["address"]: Coordinates(37.45, -122.18, kind="building")})
    out = nominatim_fallback([firm], g)
    assert out == {"a": {"lat": 37.45, "lng": -122.18, "match": "Exact", "source": "nominatim"}}
    assert g.queries == [firm["address"]]


def test_nominatim_fallback_retries_without_the_unit_as_non_exact() -> None:
    firm = {"id": "a", "name": "A", "address": "3000 SAND HILL ROAD BUILDING 2, SUITE 110, Menlo Park, CA 94025"}
    g = StubGeocoder({"3000 SAND HILL ROAD, Menlo Park, CA 94025": Coordinates(37.42, -122.20, kind="building")})
    out = nominatim_fallback([firm], g)
    assert out["a"]["match"] == "Non_Exact"
    assert g.queries == [firm["address"], "3000 SAND HILL ROAD, Menlo Park, CA 94025"]


def test_nominatim_fallback_rejects_area_level_matches() -> None:
    # A query Nominatim cannot parse often resolves to the city: that is the
    # centroid we are trying to get away from, so it must not count.
    firm = {"id": "a", "name": "A", "address": "PIER 5 SUITE 102, San Francisco, CA 94111"}
    g = StubGeocoder({
        firm["address"]: Coordinates(37.77, -122.42, kind="city"),
        "PIER 5, San Francisco, CA 94111": Coordinates(37.80, -122.40, kind="road"),
    })
    out = nominatim_fallback([firm], g)
    assert out == {"a": {"lat": 37.80, "lng": -122.40, "match": "Non_Exact", "source": "nominatim"}}


def test_nominatim_fallback_survives_a_failing_lookup() -> None:
    class Boom(StubGeocoder):
        def lookup(self, address: str):
            raise RuntimeError("503")

    out = nominatim_fallback([{"id": "a", "name": "A", "address": "1 MAIN ST, Palo Alto, CA 94301"}], Boom({}))
    assert out == {}


def test_nominatim_is_off_without_a_real_contact() -> None:
    # The checkout under test has no SCRAPER_CONTACT, so the UA carries the
    # placeholder and the fallback must stay off.
    assert nominatim_available() is False


def test_apply_writes_source_and_skips_unchanged() -> None:
    firms = [
        {"id": "a", "lat": 1.0, "lng": 2.0, "geo_source": "city_centroid"},
        {"id": "b", "lat": 3.0, "lng": 4.0, "geo_source": "census", "geo_match": "Exact"},
        {"id": "c", "lat": 0.0, "lng": 0.0},
    ]
    results = {
        "a": {"lat": 9.0, "lng": 8.0, "match": "Non_Exact", "source": "nominatim"},
        "b": {"lat": 3.0, "lng": 4.0, "match": "Exact"},
    }
    assert apply(firms, results) == 1
    assert firms[0] == {"id": "a", "lat": 9.0, "lng": 8.0, "geo_source": "nominatim", "geo_match": "Non_Exact"}
    assert firms[1]["geo_source"] == "census"
    assert "geo_source" not in firms[2]


def test_sec_bulk_fixes_known_street_typos() -> None:
    from scraper.sec_bulk import _format_address

    row = {
        "Main Office Street Address 1": "1300 EL CAMINO ROAD",
        "Main Office Street Address 2": "SUITE 170",
        "Main Office City": "MENLO PARK",
        "Main Office State": "CA",
        "Main Office Postal Code": "94025-1234",
    }
    assert _format_address(row) == "1300 EL CAMINO REAL SUITE 170, Menlo Park, CA 94025"
