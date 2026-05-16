from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from scraper.build import _dedup_key_from_name, load_seed, merge_firms
from scraper.sec_bulk import _CACHE_FIELDS, fetch_bay_area_vc_firms

BAY_AREA_COUNTIES = {"San Francisco", "San Mateo", "Santa Clara", "Alameda", "Marin"}


def test_seed_loads_and_has_required_fields() -> None:
    firms = load_seed()
    assert len(firms) >= 20
    required = {"id", "name", "address", "website", "stages", "sectors"}
    ids = set()
    for firm in firms:
        missing = required - firm.keys()
        assert not missing, f"{firm.get('id')!r} missing fields: {missing}"
        assert firm["id"] not in ids, f"duplicate id: {firm['id']}"
        ids.add(firm["id"])
        assert isinstance(firm["stages"], list) and firm["stages"]
        assert isinstance(firm["sectors"], list) and firm["sectors"]


def test_seed_check_size_well_formed() -> None:
    for firm in load_seed():
        cs = firm["check_size"]
        assert cs["min"] <= cs["typical"] <= cs["max"], f"{firm['id']} check_size order"


def test_seed_firms_have_bay_area_county() -> None:
    firms = load_seed()
    assert firms, "expected at least one seed firm"
    for firm in firms:
        county = firm.get("county")
        assert county in BAY_AREA_COUNTIES, (
            f"{firm['id']} has county {county!r}, expected one of {BAY_AREA_COUNTIES}"
        )


def test_seed_firms_marked_as_rich_tier() -> None:
    for firm in load_seed():
        assert firm.get("tier") == "rich", f"{firm['id']} should be tier=rich"


def _write_fixture_cache(path: Path, rows: list[dict]) -> None:
    """Write a fresh cache CSV in the format scraper.sec_bulk expects."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(f"# fetched: {datetime.now(timezone.utc).isoformat()}\n")
        writer = csv.DictWriter(f, fieldnames=_CACHE_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in _CACHE_FIELDS})


def test_sec_bulk_filters_to_bay_area_cities(tmp_path) -> None:
    cache = tmp_path / "fixture.csv"
    _write_fixture_cache(
        cache,
        [
            # Bay Area: San Francisco — should be included
            {
                "crd": "111", "name": "Acme Ventures, LLC",
                "addr1": "1 Market St", "city": "SAN FRANCISCO",
                "state": "CA", "postal": "94105",
                "firm_type": "Exempt Reporting Adviser", "aum_raw": "",
            },
            # Bay Area: Menlo Park — should be included
            {
                "crd": "222", "name": "Sand Hill Partners",
                "addr1": "2800 Sand Hill Rd", "city": "MENLO PARK",
                "state": "CA", "postal": "94025",
                "firm_type": "Registered IA", "any_vc": "Y",
                "aum_raw": "1500000000",
            },
            # CA but outside the 5-county core (Sacramento) — rejected
            {
                "crd": "333", "name": "Capital Sac",
                "addr1": "100 K St", "city": "SACRAMENTO",
                "state": "CA", "postal": "95814",
                "firm_type": "Exempt Reporting Adviser",
            },
            # Empty CRD — rejected
            {
                "crd": "", "name": "No CRD",
                "city": "SAN FRANCISCO", "state": "CA",
            },
        ],
    )
    firms = fetch_bay_area_vc_firms(cache_path=cache)
    crds = {f["sec_crd"] for f in firms}
    assert crds == {"111", "222"}
    by_crd = {f["sec_crd"]: f for f in firms}
    assert by_crd["111"]["county"] == "San Francisco"
    assert by_crd["222"]["county"] == "San Mateo"
    assert by_crd["222"]["aum_usd"] == 1_500_000_000
    assert by_crd["111"]["aum_usd"] is None
    assert all(f["tier"] == "lite" for f in firms)
    assert all(f["id"] == f"sec-{f['sec_crd']}" for f in firms)
    # Schema invariant: lite firms must carry empty stages/sectors arrays so
    # frontend code (allStages, allSectors, visibleFirms) can iterate without
    # null guards. Regression check for codex review P1 finding.
    assert all(f["stages"] == [] for f in firms)
    assert all(f["sectors"] == [] for f in firms)


def test_sec_bulk_emits_enrichment_fields(tmp_path) -> None:
    """Each lite firm carries the Form ADV signal columns we ingest from the
    monthly CSV: website, phone, fund count, firm-type tags, and the AUM
    fallback priority (regulatory AUM > private-fund gross assets)."""
    cache = tmp_path / "fixture.csv"
    _write_fixture_cache(
        cache,
        [
            # Registered IA with full reporting — uses 5F(2)(c).
            {
                "crd": "100", "name": "Big Fund, LP",
                "addr1": "1 Sand Hill", "city": "MENLO PARK",
                "state": "CA", "postal": "94025",
                "firm_type": "Registered IA", "any_vc": "Y", "any_pe": "Y",
                "num_vc": "3", "num_pe": "1", "fund_count": "4",
                "aum_raw": "5000000000",
                "aum_pf_raw": "4500000000",
                "website": "bigfund.com",
                "phone": "+1 650-555-0100",
                "latest_filing": "2025-04-15",
                "employees": "42",
            },
            # ERA with only Schedule D AUM — falls back to aum_pf_raw.
            {
                "crd": "200", "name": "Era Capital, LLC",
                "addr1": "100 Market St", "city": "SAN FRANCISCO",
                "state": "CA", "postal": "94105",
                "firm_type": "Exempt Reporting Adviser", "any_vc": "Y",
                "num_vc": "2", "fund_count": "2",
                "aum_raw": "",
                "aum_pf_raw": "350000000",
                "website": "https://era.vc",
                "phone": "(415) 555-0200",
                "latest_filing": "2025-03-01",
            },
            # Bare-minimum row — every enrichment field blank/null.
            {
                "crd": "300", "name": "Quiet Fund, LP",
                "addr1": "50 California St", "city": "SAN FRANCISCO",
                "state": "CA", "postal": "94111",
                "firm_type": "Exempt Reporting Adviser", "any_vc": "Y",
            },
        ],
    )
    firms = {f["sec_crd"]: f for f in fetch_bay_area_vc_firms(cache_path=cache)}

    big = firms["100"]
    assert big["aum_usd"] == 5_000_000_000
    assert big["aum_source"].startswith("SEC Form ADV Item 5.F(2)(c)")
    assert "filed 2025-04-15" in big["aum_source"]
    assert big["aum_as_of"] == "2025-04-15"
    assert big["website"] == "https://bigfund.com"
    assert big["phone"] == "+1 650-555-0100"
    assert big["fund_count"] == 4
    assert big["vc_fund_count"] == 3
    assert big["pe_fund_count"] == 1
    assert big["employee_count"] == 42
    assert big["firm_type_tags"] == ["vc", "pe"]
    assert big["latest_filing_date"] == "2025-04-15"

    era = firms["200"]
    assert era["aum_usd"] == 350_000_000
    assert era["aum_source"].startswith("SEC Form ADV Schedule D 7.B(1)")
    assert era["website"] == "https://era.vc"  # already had scheme; preserved
    assert era["fund_count"] == 2
    assert era["firm_type_tags"] == ["vc"]
    assert era["employee_count"] is None  # 5A only populated for registered IAs

    quiet = firms["300"]
    assert quiet["aum_usd"] is None
    assert quiet["aum_source"] is None
    assert quiet["website"] is None
    assert quiet["phone"] is None
    assert quiet["fund_count"] is None
    assert quiet["firm_type_tags"] == ["vc"]


def test_merge_dedups_by_crd() -> None:
    seed = [
        {"id": "sequoia", "name": "Sequoia Capital", "sec_crd": "157518", "tier": "rich"},
        {"id": "kleiner", "name": "Kleiner Perkins", "tier": "rich"},
    ]
    scraped = [
        {"id": "sec-157518", "name": "Sequoia Capital Operations LLC",
         "sec_crd": "157518", "tier": "lite"},   # dup CRD — drop
        {"id": "sec-999", "name": "Kleiner Perkins LLC",
         "sec_crd": "999", "tier": "lite"},      # dup name (after suffix strip) — drop
        {"id": "sec-42", "name": "Founders Fund", "sec_crd": "42", "tier": "lite"},
    ]
    merged = merge_firms(seed, scraped)
    ids = [f["id"] for f in merged]
    assert ids == ["sequoia", "kleiner", "sec-42"]


def test_dedup_key_normalises_common_suffixes() -> None:
    assert _dedup_key_from_name("Sequoia Capital") == _dedup_key_from_name("Sequoia")
    assert _dedup_key_from_name("Founders Fund LP") == _dedup_key_from_name("Founders Fund")
    assert _dedup_key_from_name("Andreessen Horowitz LLC") == _dedup_key_from_name("Andreessen Horowitz")
