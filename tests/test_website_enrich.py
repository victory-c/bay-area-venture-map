"""Offline tests for the website enricher: URL filtering, HTML-to-text
cleanup, and the build-hook gating (only fires on lite firms missing
LLM data with a real website).

Live Vertex AI calls and live HTTP fetches are exercised by manual
smoke tests.
"""

from __future__ import annotations

import pytest

from scraper.build import enrich_websites
from scraper.llm_enrich import EnrichmentResult
from scraper.website_enrich import _html_to_text, _is_scrapable_url


def test_is_scrapable_url_blocks_socials() -> None:
    assert _is_scrapable_url("https://www.felicis.com") is True
    assert _is_scrapable_url("https://www.facebook.com/people/Alameda/100") is False
    assert _is_scrapable_url("https://x.com/AnagramAssetM") is False
    assert _is_scrapable_url("https://twitter.com/somefirm") is False
    assert _is_scrapable_url("https://www.linkedin.com/in/alice") is False
    assert _is_scrapable_url("https://medium.com/@firm") is False


def test_is_scrapable_url_handles_malformed_input() -> None:
    assert _is_scrapable_url("") is False
    assert _is_scrapable_url("not a url") is False
    # Hostname missing entirely (just a path) -> not scrapable.
    assert _is_scrapable_url("/team") is False


def test_html_to_text_strips_scripts_and_nav() -> None:
    html = """
    <html>
      <head><script>var x = 1;</script><style>body{}</style></head>
      <body>
        <nav><a href="/">Home</a><a href="/team">Team</a></nav>
        <header>HEADER JUNK</header>
        <main>
          <h1>About Us</h1>
          <p>We invest in seed-stage AI startups.</p>
          <p>Partners: Alice, Bob, Carol.</p>
        </main>
        <footer>FOOTER JUNK</footer>
        <script>alert('x')</script>
      </body>
    </html>
    """
    out = _html_to_text(html)
    assert "var x = 1" not in out
    assert "HEADER JUNK" not in out
    assert "FOOTER JUNK" not in out
    assert "Home" not in out
    assert "About Us" in out
    assert "seed-stage AI" in out
    assert "Alice, Bob, Carol" in out


def test_html_to_text_collapses_blank_line_runs() -> None:
    html = "<html><body><p>A</p><br><br><br><br><p>B</p></body></html>"
    out = _html_to_text(html)
    # Should not have a run of 3+ consecutive blank lines.
    assert "\n\n\n" not in out
    assert "A" in out and "B" in out


def test_html_to_text_returns_empty_on_garbage() -> None:
    assert _html_to_text("") == ""


# -------------------- build-hook gating ----------------------------------


class _StubEnricher:
    """Stand-in for WebsiteEnricher used by the build-hook test."""

    def __init__(self, responses: dict[str, EnrichmentResult | None]):
        self.responses = responses
        self.calls: list[str] = []

    def enrich(self, firm: dict):
        self.calls.append(firm["id"])
        return self.responses.get(firm["id"])

    def close(self):
        pass


def test_enrich_websites_only_targets_lite_firms_missing_llm_data(monkeypatch) -> None:
    firms = [
        # Rich firm — never scraped.
        {"id": "rich1", "tier": "rich", "website": "https://x.com", "name": "X"},
        # Lite + already has LLM data — skipped.
        {"id": "lite-have", "tier": "lite", "name": "Y",
         "website": "https://y.com", "llm_enriched": True},
        # Lite + no LLM data + no website — skipped (can't scrape).
        {"id": "lite-nosite", "tier": "lite", "name": "Z"},
        # Lite + no LLM data + has website — IS scraped.
        {"id": "lite-target", "tier": "lite", "name": "W",
         "website": "https://w.com"},
    ]
    fake_info = EnrichmentResult(
        website="https://w.com", confidence=0.85,
        partners=[{"name": "Alice", "title": "GP"}],
        sectors=["enterprise_saas"], stages=["seed"],
    )
    stub = _StubEnricher({"lite-target": fake_info})
    monkeypatch.setattr("scraper.build.WebsiteEnricher", lambda cache_path: stub)

    enrich_websites(firms)

    # Only the lite-target firm was sent through the enricher.
    assert stub.calls == ["lite-target"]
    target = firms[3]
    assert target["website_enriched"] is True
    assert target["partners"][0]["name"] == "Alice"
    assert target["sectors"] == ["enterprise_saas"]
    # Other firms left alone.
    assert "website_enriched" not in firms[0]
    assert "website_enriched" not in firms[1]
    assert "website_enriched" not in firms[2]


def test_enrich_websites_skips_low_confidence_results(monkeypatch) -> None:
    firms = [{"id": "lite-target", "tier": "lite", "name": "W",
              "website": "https://w.com"}]
    weak = EnrichmentResult(website="https://w.com", confidence=0.3,
                            partners=[{"name": "Alice", "title": None}])
    stub = _StubEnricher({"lite-target": weak})
    monkeypatch.setattr("scraper.build.WebsiteEnricher", lambda cache_path: stub)

    enrich_websites(firms)

    # Below the 0.50 merge threshold — nothing applied, no provenance flag.
    assert "partners" not in firms[0]
    assert "website_enriched" not in firms[0]
