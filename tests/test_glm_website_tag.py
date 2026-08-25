"""Offline tests for the website-grounded tagger's evidence dedupe.

The network call and the page fetch are exercised by manual smoke runs; here
we drive the pure reducer that turns the model's raw ``sectors`` list into the
tags that reach ``firm.sectors``.
"""

from __future__ import annotations

from scraper.glm_website_tag import _dedupe_verified


def _v(sector: str, quote: str) -> dict:
    return {"sector": sector, "quote": quote}


def test_distinct_sectors_and_quotes_all_survive() -> None:
    verified = [
        _v("healthcare", "we back medical devices"),
        _v("bio", "and life sciences platforms"),
    ]
    deduped, shared_quote, repeat_sector = _dedupe_verified(verified)
    assert [d["sector"] for d in deduped] == ["healthcare", "bio"]
    assert (shared_quote, repeat_sector) == (0, 0)


def test_one_quote_cannot_justify_several_sectors() -> None:
    # The model pastes the same sentence under two tags; only the first has
    # distinct evidence.
    verified = [
        _v("ai_infra", "we invest in AI"),
        _v("fintech", "we invest in AI"),
    ]
    deduped, shared_quote, repeat_sector = _dedupe_verified(verified)
    assert [d["sector"] for d in deduped] == ["ai_infra"]
    assert (shared_quote, repeat_sector) == (1, 0)


def test_shared_quote_match_ignores_whitespace_and_case() -> None:
    verified = [
        _v("ai_infra", "We invest in AI"),
        _v("fintech", "we   invest\nin ai"),
    ]
    deduped, shared_quote, _ = _dedupe_verified(verified)
    assert [d["sector"] for d in deduped] == ["ai_infra"]
    assert shared_quote == 1


def test_repeated_sector_collapses_to_one_tag() -> None:
    # This is the shape that shipped ["healthcare", "healthcare", "healthcare"]
    # to firms.json: one sector, three different supporting quotes. Left in, it
    # rendered as repeated chips and gave Alpine's x-for a duplicate :key.
    verified = [
        _v("healthcare", "we invest exclusively in healthcare services"),
        _v("healthcare", "healthcare technology"),
        _v("healthcare", "medical devices"),
    ]
    deduped, shared_quote, repeat_sector = _dedupe_verified(verified)
    assert [d["sector"] for d in deduped] == ["healthcare"]
    assert deduped[0]["quote"] == "we invest exclusively in healthcare services"
    assert (shared_quote, repeat_sector) == (0, 2)


def test_repeat_sector_keeps_the_first_entry_and_its_quote() -> None:
    verified = [
        _v("consumer", "we are passionate about the consumer space"),
        _v("fintech", "payments infrastructure"),
        _v("consumer", "we invest in emerging consumer brands"),
    ]
    deduped, _, repeat_sector = _dedupe_verified(verified)
    assert [d["sector"] for d in deduped] == ["consumer", "fintech"]
    assert deduped[0]["quote"] == "we are passionate about the consumer space"
    assert repeat_sector == 1


def test_empty_input_is_a_no_op() -> None:
    assert _dedupe_verified([]) == ([], 0, 0)
