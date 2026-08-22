"""Offline tests for the Form D enricher: brand-token derivation, hit
parsing + display-name validation, and the build-hook merge semantics.

The actual EFTS network call is exercised by manual smoke tests; here we
feed canned EFTS payloads through the pure parser and use a stub client
to exercise ``enrich_form_d``.
"""

from __future__ import annotations

from scraper.build import enrich_form_d
from scraper.form_d import (
    FilingMeta,
    _brand_matches,
    FormDInfo,
    _brand_token,
    _parse_hits,
)


def test_brand_token_strips_corporate_suffixes() -> None:
    assert _brand_token("Sequoia Capital") == "sequoia"
    assert _brand_token("Khosla Ventures") == "khosla"
    assert _brand_token("Andreessen Horowitz LLC") == "andreessen horowitz"
    assert _brand_token("AH Capital Management, L.P.") == "ah"


def test_brand_token_handles_parenthetical_aliases() -> None:
    assert _brand_token("Andreessen Horowitz (a16z)") == "andreessen horowitz"


def test_brand_token_empty_after_strip_falls_back_to_lowercase() -> None:
    # If suffix-stripping eats everything, fall back to the original lowercase
    # (this is a defensive path; in practice firms always have a brand word).
    assert _brand_token("LLC") == "llc"


def _hit(filer: str, cik: str, date: str, form: str = "D", adsh: str = "x-0-0") -> dict:
    return {
        "_source": {
            "display_names": [f"{filer}  (CIK {cik})"],
            "ciks": [cik],
            "file_date": date,
            "form": form,
            "adsh": adsh,
        }
    }


def test_parse_hits_filters_cross_contamination() -> None:
    # Pretend EFTS returned a Sequoia filing AND an unrelated "Capital Group"
    # filing that happened to match the quoted-string search.
    hits = [
        _hit("Sequoia Capital Fund, L.P.", "0001906948", "2025-02-05"),
        _hit("Capital Group Companies Inc.", "0000123456", "2024-10-10"),
        _hit("SEQUOIA CAPITAL CHINA GROWTH 2010 FUND, L.P.", "0001493111", "2010-06-07"),
        # Mid-name mention: a feeder, not a Sequoia vehicle. Must be dropped.
        _hit("Wisconsin Sequoia Capital, LLC", "0000777777", "2023-01-01"),
    ]
    info = _parse_hits("Sequoia Capital", hits)
    # Capital Group dropped; both Sequoia entries kept.
    assert info.total_filings == 2
    assert info.latest_filing_date == "2025-02-05"
    names = info.distinct_funds
    assert any("Sequoia Capital Fund" in n for n in names)
    assert any("SEQUOIA CAPITAL CHINA" in n for n in names)
    assert all("Capital Group" not in n for n in names)


def test_parse_hits_sorts_recent_filings_desc_and_caps() -> None:
    # Seven Sequoia filings; we should keep all in distinct_funds (≤10) but
    # cap recent_filings at MAX_RECENT (5) and order them newest-first.
    hits = [
        _hit(f"Sequoia Fund {i}, L.P.", f"000000000{i}", f"2020-01-{i:02d}", adsh=f"x-{i}")
        for i in range(1, 8)
    ]
    info = _parse_hits("Sequoia", hits)
    assert info.total_filings == 7
    assert len(info.recent_filings) == 5
    assert info.recent_filings[0].file_date == "2020-01-07"
    assert info.recent_filings[-1].file_date == "2020-01-03"


def test_parse_hits_dedupes_distinct_funds_and_ciks() -> None:
    # Same fund entity files D, then D/A — should count both as filings but
    # only list the fund once in distinct_funds / fund_ciks.
    hits = [
        _hit("Khosla Ventures V, L.P.", "0001493112", "2024-06-01", form="D/A"),
        _hit("Khosla Ventures V, L.P.", "0001493112", "2024-05-01", form="D"),
        _hit("Khosla Ventures VI, L.P.", "0001493113", "2025-01-01", form="D"),
    ]
    info = _parse_hits("Khosla Ventures", hits)
    assert info.total_filings == 3
    assert info.distinct_funds == [
        "Khosla Ventures VI, L.P.  (CIK 0001493113)",
        "Khosla Ventures V, L.P.  (CIK 0001493112)",
    ]
    assert info.fund_ciks == ["1493113", "1493112"]


def test_parse_hits_returns_empty_info_when_all_filtered() -> None:
    hits = [_hit("Unrelated Firm Inc.", "0000999999", "2024-01-01")]
    info = _parse_hits("Sequoia Capital", hits)
    assert info.total_filings == 0
    assert info.latest_filing_date is None
    assert info.distinct_funds == []


class _StubClient:
    """In-memory FormDClient stand-in for build-hook tests."""

    def __init__(self, responses: dict[str, FormDInfo | None]):
        self._responses = responses
        self.calls: list[str] = []

    def lookup(self, name: str):
        self.calls.append(name)
        return self._responses.get(name)

    def close(self):
        pass


def test_enrich_form_d_merges_only_on_hit(monkeypatch) -> None:
    firms = [
        {"id": "a", "name": "Sequoia Capital"},
        {"id": "b", "name": "Random Shell Co"},
    ]
    sequoia_info = FormDInfo(
        total_filings=2,
        latest_filing_date="2025-02-05",
        distinct_funds=["Sequoia Capital Fund, L.P."],
        fund_ciks=["1906948"],
        recent_filings=[
            FilingMeta(
                accession="x", file_date="2025-02-05", form="D/A",
                cik="1906948", filer_name="Sequoia Capital Fund, L.P.",
            )
        ],
    )
    stub = _StubClient({"Sequoia Capital": sequoia_info, "Random Shell Co": None})
    monkeypatch.setattr("scraper.build.FormDClient", lambda cache_path: stub)

    enrich_form_d(firms)

    assert firms[0]["form_d_total_filings"] == 2
    assert firms[0]["form_d_latest_filing_date"] == "2025-02-05"
    assert firms[0]["form_d_fund_ciks"] == ["1906948"]
    assert firms[0]["form_d_recent_filings"][0]["accession"] == "x"
    # The no-hit firm gets no Form D fields.
    assert "form_d_total_filings" not in firms[1]
    assert stub.calls == ["Sequoia Capital", "Random Shell Co"]


# ---------------------------------------------------------------------------
# Anchored brand matching (regression: unrelated filers credited to a firm)
# ---------------------------------------------------------------------------


def test_brand_matches_requires_the_brand_to_lead_the_filer_name() -> None:
    assert _brand_matches("sequoia", "Sequoia Capital Fund, L.P.")
    assert _brand_matches("sequoia", "SEQUOIA CAPITAL CHINA GROWTH 2010 FUND, L.P.")
    # Leading article is not a brand.
    assert _brand_matches("khosla", "The Khosla Ventures V, L.P.")
    # Mid-name mentions are feeders / SPVs / unrelated shops.
    assert not _brand_matches("sequoia", "Wisconsin Sequoia Capital, LLC")
    assert not _brand_matches("menlo", "iCapital-Menlo Ventures Select I RCM Access Fund, L.P.")
    assert not _brand_matches("accel", "Genesis Accel Opportunity Fund Series 2 LP")


def test_brand_matches_allows_series_llc_vehicles() -> None:
    # "<Something> Fund I, a series of <Brand> Funds, LP" really is the
    # sponsor's vehicle, so the series construction is the one exception
    # to the leading-brand rule.
    assert _brand_matches("trinity", "AU Fund I, a series of Trinity Ventures Funds, LP")
    assert _brand_matches("trinity", "BR-1012 Fund II, a series of Trinity Ventures Funds, LP")
    # ...but only when the brand follows "series of", not merely appears.
    assert not _brand_matches(
        "kleiner perkins",
        "OW Kleiner Perkins 22 & Select IV a series of Allocations 2026 Master, LLC",
    )


def test_brand_matches_rejects_short_token_false_positives() -> None:
    # Regression: brand token "ai" substring-matched every fund with "AI"
    # anywhere in its name, crediting Ai Ventures with 24 foreign filings.
    assert not _brand_matches("ai", "Magnetar AI Ventures Fund LP")
    assert not _brand_matches("ai", "Moringa x AI Ventures V a Series of Moringa Capital Ventures LLC")
    # "pa" must not match "PALO ALTO..."; word boundary, not prefix-of-word.
    assert not _brand_matches("pa", "PALO ALTO GROWTH CAPITAL LLC")


def test_parse_hits_rejects_wholly_foreign_filings() -> None:
    # Every one of Founders Fund's 15 attributed filings belonged to an
    # unrelated shop whose vehicle was named "<X> Founders Fund". The
    # correct answer is zero, not fifteen.
    hits = [
        _hit("BWC Founders Fund, LLC", "0002058434", "2025-03-01"),
        _hit("ARIZONA FOUNDERS FUND, LLC", "0001684894", "2024-02-01"),
        _hit("Aequitas ETC Founders Fund, LLC", "0001534219", "2023-01-01"),
    ]
    assert _parse_hits("Founders Fund LLC", hits).total_filings == 0


# ---------------------------------------------------------------------------
# Pagination (regression: filing counts saturated at one page of results)
# ---------------------------------------------------------------------------


def _paged_transport(total: int, page_size: int):
    """MockTransport serving `total` Sequoia hits in `page_size` pages."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("from", 0))
        size = int(request.url.params.get("size", page_size))
        window = min(size, page_size)
        hits = [
            _hit(f"Sequoia Capital Fund {i}, L.P.", f"{i:010d}", "2024-01-01", adsh=f"a-{i}")
            for i in range(start, min(start + window, total))
        ]
        return httpx.Response(
            200, json={"hits": {"total": {"value": total}, "hits": hits}}
        )

    return httpx.MockTransport(handler)


def test_lookup_paginates_past_the_first_page(tmp_path) -> None:
    import httpx
    from scraper.form_d import FormDClient

    client = httpx.Client(transport=_paged_transport(total=237, page_size=100))
    fd = FormDClient(client=client, cache_path=tmp_path / "cache.json")
    fd._throttle = lambda: None  # no real sleeping in tests
    info = fd.lookup("Sequoia Capital")
    # Previously this returned 100 — the page size, not the total.
    assert info.total_filings == 237
    assert len(info.recent_filings) == 5


def test_lookup_stops_when_total_is_reached(tmp_path) -> None:
    import httpx
    from scraper.form_d import FormDClient

    transport = _paged_transport(total=7, page_size=100)
    calls: list[str] = []

    def counting(request):
        calls.append(str(request.url))
        return transport.handler(request)

    client = httpx.Client(transport=httpx.MockTransport(counting))
    fd = FormDClient(client=client, cache_path=tmp_path / "cache.json")
    fd._throttle = lambda: None
    info = fd.lookup("Sequoia Capital")
    assert info.total_filings == 7
    assert len(calls) == 1  # single page covered the total; no wasted request


# ---------------------------------------------------------------------------
# Clearing (regression: a bad attribution could never be retracted)
# ---------------------------------------------------------------------------


def test_enrich_form_d_clears_a_block_that_no_longer_matches(monkeypatch) -> None:
    # Founders Fund's 15 filings all belonged to other shops. Once the matcher
    # stops returning them the stale block must go, not linger.
    from scraper import build as build_mod

    firm = {
        "id": "founders-fund",
        "name": "Founders Fund LLC",
        "form_d_total_filings": 15,
        "form_d_latest_filing_date": "2025-03-01",
        "form_d_distinct_funds": ["BWC Founders Fund, LLC  (CIK 0002058434)"],
        "form_d_fund_ciks": ["2058434"],
        "form_d_recent_filings": [{"accession": "x", "file_date": "2025-03-01",
                                   "form": "D", "cik": "2058434",
                                   "filer_name": "BWC Founders Fund, LLC"}],
        "sectors": ["deep_tech"],
    }
    monkeypatch.setattr(build_mod, "FormDClient",
                        lambda cache_path=None: _StubClient({}))
    build_mod.enrich_form_d([firm])

    for key in build_mod.FORM_D_FIELDS:
        assert key not in firm, f"{key} should have been cleared"
    # Untouched fields survive: clearing is scoped to the Form D block.
    assert firm["sectors"] == ["deep_tech"]


def test_enrich_form_d_leaves_firms_that_never_had_a_block_alone(monkeypatch) -> None:
    from scraper import build as build_mod

    firm = {"id": "x", "name": "Obscure Shell LP", "sectors": ["fintech"]}
    monkeypatch.setattr(build_mod, "FormDClient",
                        lambda cache_path=None: _StubClient({}))
    build_mod.enrich_form_d([firm])
    assert firm == {"id": "x", "name": "Obscure Shell LP", "sectors": ["fintech"]}


def test_enrich_form_d_overwrites_a_stale_block_on_a_fresh_match(monkeypatch) -> None:
    from scraper import build as build_mod

    firm = {"id": "sequoia", "name": "Sequoia Capital", "form_d_total_filings": 99}
    info = FormDInfo(
        total_filings=237,
        latest_filing_date="2026-01-05",
        distinct_funds=["Sequoia Capital Fund, L.P."],
        fund_ciks=["1906948"],
        recent_filings=[FilingMeta("a", "2026-01-05", "D", "1906948",
                                   "Sequoia Capital Fund, L.P.")],
    )
    monkeypatch.setattr(build_mod, "FormDClient",
                        lambda cache_path=None: _StubClient({"Sequoia Capital": info}))
    build_mod.enrich_form_d([firm])
    assert firm["form_d_total_filings"] == 237
