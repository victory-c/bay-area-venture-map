"""Platform tagging must be exact — a false positive libels a real fund."""
from __future__ import annotations

from scraper.platforms import (
    CANDIDATE_MIN_FILINGS,
    PLATFORM_FIRMS,
    annotate_platforms,
    platform_candidates,
)


def _firm(name, crd, **kw):
    return {"id": f"sec-{crd}", "name": name, "sec_crd": crd, **kw}


def test_tags_listed_platforms_with_a_note() -> None:
    firms = [_firm("Vauban Advisers LLC", "319358")]
    assert annotate_platforms(firms) == 1
    assert firms[0]["firm_role"] == "platform"
    assert "third-party" in firms[0]["platform_note"]


def test_does_not_tag_prolific_real_funds() -> None:
    # Volume is not the signal: Sequoia files more than Wefunder.
    firms = [
        _firm("Sequoia Capital", "111111", form_d_total_filings=217),
        _firm("Accel", "222222", form_d_total_filings=104),
        _firm("Eclipse", "333333", form_d_total_filings=104),
        _firm("Khosla Ventures", "444444", form_d_total_filings=42),
    ]
    assert annotate_platforms(firms) == 0
    assert all("firm_role" not in f for f in firms)


def test_matches_on_crd_not_name_substring() -> None:
    # "Forgepoint Capital" contains "forge"; it is a cybersecurity VC.
    firms = [_firm("Forgepoint Capital Management, L.L.C.", "281676")]
    assert annotate_platforms(firms) == 0
    assert "firm_role" not in firms[0]


def test_delisting_a_firm_removes_the_tag() -> None:
    firm = _firm("Formerly A Platform", "999999",
                 firm_role="platform", platform_note="stale")
    assert annotate_platforms([firm]) == 0
    assert "firm_role" not in firm
    assert "platform_note" not in firm


def test_candidates_ignore_already_listed_platforms() -> None:
    firms = [_firm("Vauban Advisers LLC", "319358", form_d_total_filings=289,
                   form_d_distinct_funds=["X, a Series of Vauban Platform LP"])]
    annotate_platforms(firms)
    assert platform_candidates(firms) == []


def test_candidates_report_unlisted_series_heavy_filers() -> None:
    firms = [_firm("New Syndicate Platform LLC", "888888",
                   form_d_total_filings=CANDIDATE_MIN_FILINGS + 10,
                   form_d_distinct_funds=[f"Deal {i}, a series of NSP Master LP"
                                          for i in range(5)])]
    found = platform_candidates(firms)
    assert [r[0] for r in found] == ["New Syndicate Platform LLC"]
    # Reporting only — never auto-tagged.
    assert "firm_role" not in firms[0]


def test_candidates_ignore_ordinary_spv_naming() -> None:
    # "Khosla Ventures MM SPV, LLC" is deal-by-deal, not a platform.
    firms = [_firm("Khosla Ventures", "444444", form_d_total_filings=200,
                   form_d_distinct_funds=["Khosla Ventures MM SPV, LLC",
                                          "Khosla Ventures VII, L.P."])]
    assert platform_candidates(firms) == []


def test_every_listed_crd_has_a_substantive_note() -> None:
    for crd, note in PLATFORM_FIRMS.items():
        assert crd.isdigit(), crd
        assert len(note) > 40, crd
