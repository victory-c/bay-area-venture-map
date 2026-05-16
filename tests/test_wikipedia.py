"""Unit tests for the Wikipedia infobox parser.

Tests stay offline by feeding canned wikitext fragments through the pure
parsing helpers. Network behavior (search / page fetch / disambiguation
scoring) is covered by manual live tests during development.
"""

from __future__ import annotations

from scraper.wikipedia import (
    _expand_value_templates,
    _parse_aum_string,
    _parse_infobox,
    _parse_people_list,
    _parse_year,
    _split_template_body,
    _strip_markup,
    info_from_infobox,
)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
def test_split_template_body_respects_link_depth() -> None:
    body = "|[[Eugene Kleiner]]|[[Thomas Perkins (businessman)|Thomas Perkins]]|[[Brook Byers]]"
    parts = _split_template_body(body)
    assert parts == [
        "",
        "[[Eugene Kleiner]]",
        "[[Thomas Perkins (businessman)|Thomas Perkins]]",
        "[[Brook Byers]]",
    ]


def test_expand_us_dollar_template() -> None:
    assert "$56.3 billion" in _expand_value_templates("{{US$|56.3 billion|link=yes}}")
    assert "$17 billion" in _expand_value_templates("{{USD|17|billion}}")


def test_expand_hlist_to_comma_list() -> None:
    out = _expand_value_templates("{{hlist|Alice|Bob|Carol}}")
    assert out == "Alice, Bob, Carol"


def test_strip_markup_drops_refs_and_links() -> None:
    raw = "[[Sequoia Capital|Sequoia]]<ref>cite</ref> founded 1972"
    assert _strip_markup(raw) == "Sequoia founded 1972"


# ---------------------------------------------------------------------------
# Field parsers
# ---------------------------------------------------------------------------
def test_parse_year_extracts_first_4_digit_year() -> None:
    assert _parse_year("1972") == 1972
    assert _parse_year("{{Start date|1965}}") == 1965
    assert _parse_year("Spring 2018, in Menlo Park") == 2018
    assert _parse_year("") is None
    assert _parse_year("unknown") is None


def test_parse_aum_requires_dollar_or_unit_marker() -> None:
    # Bare numbers without $ or unit are NOT AUM (avoid grabbing dates).
    assert _parse_aum_string("Founded 2024") is None
    assert _parse_aum_string("Series A 1972") is None
    # Real AUM: dollar marker or explicit unit word.
    assert _parse_aum_string("{{US$|56.3 billion}}") == 56_300_000_000
    assert _parse_aum_string("$17 billion (2024)") == 17_000_000_000
    assert _parse_aum_string("$800 million") == 800_000_000
    assert _parse_aum_string("US$2.5 bn") == 2_500_000_000


def test_parse_people_list_handles_common_separators() -> None:
    # <br>-separated names
    assert _parse_people_list("[[Marc Andreessen]]<br>[[Ben Horowitz]]") == [
        "Marc Andreessen",
        "Ben Horowitz",
    ]
    # hlist template with link aliases
    out = _parse_people_list(
        "{{hlist|[[Michael Moritz]]|[[Douglas Leone]]|[[Jim Goetz]]}}"
    )
    assert out == ["Michael Moritz", "Douglas Leone", "Jim Goetz"]


def test_parse_people_filters_role_interleaved_lists() -> None:
    # NEA-style: {{ubl|Name|Role|Name|Role}} — roles must be filtered out
    out = _parse_people_list(
        "{{ubl|Scott Sandell|Exec. chairman|Tony Florence|Co-CEO|Mohamad Makhzoumi|Co-CEO}}"
    )
    assert out == ["Scott Sandell", "Tony Florence", "Mohamad Makhzoumi"]


def test_parse_people_strips_role_parentheticals() -> None:
    assert _parse_people_list("John Doe (CEO)") == ["John Doe"]


def test_parse_people_strips_refs_before_splitting() -> None:
    # A <ref>...</ref> with internal commas must not contaminate the split.
    raw = "Dick Kramlich<ref>{{Cite web |last=B |first=B |title=Foo, bar}}</ref>, Chuck Newhall"
    assert _parse_people_list(raw) == ["Dick Kramlich", "Chuck Newhall"]


# ---------------------------------------------------------------------------
# End-to-end infobox -> WikiInfo mapping
# ---------------------------------------------------------------------------
def test_parse_infobox_extracts_named_fields() -> None:
    wikitext = """
Some lead paragraph here.

{{Infobox company
| name = Sequoia Capital
| founded = 1972
| founder = [[Don Valentine]]
| key_people = {{hlist|[[Michael Moritz]]|[[Douglas Leone]]}}
| aum = {{US$|56.3 billion|link=yes}} (2024)
| hq_location = Menlo Park, California, U.S.
| industry = [[Venture capital]]
}}

== History ==
Sequoia Capital was founded...
""".strip()
    ib = _parse_infobox(wikitext)
    assert ib is not None
    assert ib["founded"] == "1972"
    info = info_from_infobox("Sequoia Capital", ib)
    assert info.founded == 1972
    assert info.founders == ["Don Valentine"]
    assert info.key_people == ["Michael Moritz", "Douglas Leone"]
    assert info.aum_usd == 56_300_000_000
    assert info.headquarters == "Menlo Park, California, U.S."
    assert info.industry == "Venture capital"
    assert info.url == "https://en.wikipedia.org/wiki/Sequoia_Capital"


def test_parse_infobox_returns_none_when_no_infobox() -> None:
    assert _parse_infobox("Just prose, no infobox here.") is None
