"""Offline tests for the Gemini enricher: response parsing, schema coercion,
and merge-into-firm semantics. Network behavior (real Gemini calls) is
verified manually via the spot-test runs in development.
"""

from __future__ import annotations

from scraper.llm_enrich import (
    EnrichmentResult,
    SECTORS,
    STAGES,
    _coerce_investments,
    _coerce_partners,
    _coerce_string_list,
    _extract_json,
    _parse_response,
    merge_into_firm,
)


def test_extract_json_from_markdown_fenced_response() -> None:
    text = 'Here you go:\n```json\n{"a": 1, "b": [2,3]}\n```\nLet me know.'
    assert _extract_json(text) == {"a": 1, "b": [2, 3]}


def test_extract_json_from_bare_object_with_prose() -> None:
    text = 'Sure thing: {"website": "https://x.com", "confidence": 0.8} done.'
    assert _extract_json(text) == {"website": "https://x.com", "confidence": 0.8}


def test_extract_json_returns_none_on_garbage() -> None:
    assert _extract_json("definitely not json") is None
    assert _extract_json("") is None


def test_coerce_string_list_drops_unknown_values() -> None:
    out = _coerce_string_list(["seed", "Series A", "made_up_stage", "growth"], STAGES)
    # series_a normalised from "Series A"; made_up_stage rejected
    assert out == ["seed", "series_a", "growth"]


def test_coerce_string_list_dedupes_preserving_order() -> None:
    out = _coerce_string_list(["consumer", "consumer", "fintech"], SECTORS)
    assert out == ["consumer", "fintech"]


def test_coerce_partners_caps_at_six_and_drops_blanks() -> None:
    raw = [
        {"name": "Alice", "title": "Managing Partner"},
        {"name": "", "title": "junk"},
        {"name": "Bob"},
        {"name": "Carol"}, {"name": "Dan"}, {"name": "Eve"},
        {"name": "Frank"}, {"name": "Grace"},  # over the cap
    ]
    out = _coerce_partners(raw)
    assert [p["name"] for p in out] == ["Alice", "Bob", "Carol", "Dan", "Eve", "Frank"]
    assert out[0] == {"name": "Alice", "title": "Managing Partner"}
    assert out[1]["title"] is None  # Bob had no title -> normalised to None


def test_coerce_investments_drops_invalid_year_and_unknown_stage() -> None:
    raw = [
        {"company": "Stripe", "year": 2023, "stage": "series_g"},  # stage unknown -> dropped
        {"company": "Notion", "year": "2024", "stage": "growth"},  # year wrong type -> nulled
        {"company": "OpenAI", "year": 2024, "stage": "growth"},
    ]
    out = _coerce_investments(raw)
    assert out[0] == {"company": "Stripe", "year": 2023, "stage": None}
    assert out[1] == {"company": "Notion", "year": None, "stage": "growth"}
    assert out[2] == {"company": "OpenAI", "year": 2024, "stage": "growth"}


def test_parse_response_full_payload() -> None:
    raw = """```json
    {
      "website": "felicis.com",
      "founded": 2006,
      "stages": ["seed", "series_a"],
      "sectors": ["enterprise_saas", "ai_infra", "made_up_sector"],
      "partners": [{"name": "Aydin Senkut", "title": "Managing Partner"}],
      "recent_investments": [{"company": "Adept", "year": 2023, "stage": "series_b"}],
      "notes": "Generalist with founder-first thesis.",
      "confidence": 0.92
    }
    ```"""
    result = _parse_response(raw, ["https://felicis.com"], {"promptTokenCount": 300, "candidatesTokenCount": 200}, "gemini-2.5-flash")
    assert result is not None
    assert result.website == "https://felicis.com"  # scheme added
    assert result.founded == 2006
    assert result.stages == ["seed", "series_a"]
    assert result.sectors == ["enterprise_saas", "ai_infra"]  # made_up_sector dropped
    assert len(result.partners) == 1
    assert result.confidence == 0.92
    assert result.sources == ["https://felicis.com"]
    assert result.prompt_tokens == 300
    assert result.output_tokens == 200


def test_parse_response_clamps_invalid_confidence() -> None:
    raw = '{"confidence": 1.7}'
    result = _parse_response(raw, [], {}, "x")
    assert result.confidence == 1.0  # clamped down
    raw = '{"confidence": -0.4}'
    result = _parse_response(raw, [], {}, "x")
    assert result.confidence == 0.0


def test_parse_response_rejects_silly_founded_year() -> None:
    raw = '{"founded": 1500, "confidence": 0.8}'
    assert _parse_response(raw, [], {}, "x").founded is None
    raw = '{"founded": 2099, "confidence": 0.8}'
    assert _parse_response(raw, [], {}, "x").founded is None
    raw = '{"founded": 1972, "confidence": 0.8}'
    assert _parse_response(raw, [], {}, "x").founded == 1972


def test_merge_into_firm_skips_low_confidence() -> None:
    firm = {"id": "x", "name": "X", "tier": "lite"}
    info = EnrichmentResult(website="https://x.com", confidence=0.3)
    assert merge_into_firm(firm, info) is False
    assert "website" not in firm


def test_merge_into_firm_only_fills_missing() -> None:
    firm = {
        "id": "x", "name": "X", "tier": "lite",
        "website": "https://existing.com",  # pre-populated by SEC bulk
        "founded": None,
    }
    info = EnrichmentResult(
        website="https://llm-suggested.com",
        founded=2010,
        stages=["seed", "series_a"],
        partners=[{"name": "Alice", "title": "GP"}],
        confidence=0.9,
    )
    assert merge_into_firm(firm, info) is True
    assert firm["website"] == "https://existing.com"  # NOT overwritten
    assert firm["founded"] == 2010                    # was missing -> filled
    assert firm["stages"] == ["seed", "series_a"]
    assert firm["partners"][0]["name"] == "Alice"
    assert firm["llm_enriched"] is True
    assert firm["llm_confidence"] == 0.9
