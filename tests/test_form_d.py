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
    ]
    info = _parse_hits("Sequoia Capital", hits, total=3)
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
    info = _parse_hits("Sequoia", hits, total=7)
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
    info = _parse_hits("Khosla Ventures", hits, total=3)
    assert info.total_filings == 3
    assert info.distinct_funds == [
        "Khosla Ventures VI, L.P.  (CIK 0001493113)",
        "Khosla Ventures V, L.P.  (CIK 0001493112)",
    ]
    assert info.fund_ciks == ["1493113", "1493112"]


def test_parse_hits_returns_empty_info_when_all_filtered() -> None:
    hits = [_hit("Unrelated Firm Inc.", "0000999999", "2024-01-01")]
    info = _parse_hits("Sequoia Capital", hits, total=1)
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
