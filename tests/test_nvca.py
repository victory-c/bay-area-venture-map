"""Offline tests for the NVCA member-directory parser and enricher.

Network behavior (the actual GET against nvca.org) is validated by manual
live smoke tests during development. These tests stay offline by feeding
canned HTML fixtures through the pure parser and exercising the
``enrich_nvca`` build hook against a stubbed client.
"""

from __future__ import annotations

import json

import pytest

from scraper import build as build_mod
from scraper.build import enrich_nvca
from scraper.nvca import (
    NvcaClient,
    NvcaMember,
    _normalize_website,
    parse_members,
)


# A trimmed but faithful copy of the live nvca.org/nvca-members/ HTML —
# preserves the table classes, header row, link wrapping, and the trailing
# whitespace / newlines inside <td> cells that the real page emits.
SAMPLE_HTML = """
<html><body>
<table class="member_table">

    <tr class=" member_block">
        <th class="member_name asc selected sortable">Firm <span class="sort"></span></th>
        <th class="member_city sortable">City <span class="sort"></span></th>
        <th class="member_state sortable">State <span class="sort"></span></th>
    </tr>

    <tr class=" member_block">
        <td class="member_name"><a href="https://www.sequoiacap.com/">Sequoia Capital</a></td>
        <td class="member_city">Menlo Park</td>
        <td class="member_state">CA
</td>
    </tr>

    <tr class=" member_block">
        <td class="member_name"><a href="https://a16z.com/">Andreessen Horowitz</a></td>
        <td class="member_city">Menlo Park</td>
        <td class="member_state">CA</td>
    </tr>

    <tr class=" member_block">
        <td class="member_name"><a href="https://www.kpcb.com/">Kleiner Perkins, LLC</a></td>
        <td class="member_city">Menlo Park</td>
        <td class="member_state">CA</td>
    </tr>

    <tr class=" member_block">
        <td class="member_name"><a href="mailto:hi@example.com">No Site Capital</a></td>
        <td class="member_city">Boston</td>
        <td class="member_state">MA</td>
    </tr>

    <!-- Duplicate name (some firms appear with multiple office rows) -->
    <tr class=" member_block">
        <td class="member_name"><a href="https://www.sequoiacap.com/">Sequoia Capital</a></td>
        <td class="member_city">San Francisco</td>
        <td class="member_state">CA</td>
    </tr>

</table>
</body></html>
"""


def test_parse_members_extracts_name_website_city_state() -> None:
    members = parse_members(SAMPLE_HTML)
    # 4 unique firms (the duplicate Sequoia row is collapsed).
    assert [m.name for m in members] == [
        "Sequoia Capital",
        "Andreessen Horowitz",
        "Kleiner Perkins, LLC",
        "No Site Capital",
    ]
    sequoia = members[0]
    assert sequoia.website == "https://www.sequoiacap.com/"
    assert sequoia.hq_city == "Menlo Park"
    # State cell on the real page contains a trailing newline — must be stripped.
    assert sequoia.hq_state == "CA"


def test_parse_members_skips_non_http_hrefs() -> None:
    members = parse_members(SAMPLE_HTML)
    no_site = next(m for m in members if m.name == "No Site Capital")
    # mailto: should be filtered out, not silently kept.
    assert no_site.website is None


def test_normalize_website_filters_junk_schemes() -> None:
    assert _normalize_website("https://x.com/") == "https://x.com/"
    assert _normalize_website("  http://y.com  ") == "http://y.com"
    assert _normalize_website("mailto:a@b.com") is None
    assert _normalize_website("tel:+1") is None
    assert _normalize_website("javascript:void(0)") is None
    assert _normalize_website("#anchor") is None
    assert _normalize_website("/relative/path") is None
    assert _normalize_website("") is None


def test_parse_members_returns_empty_on_no_table() -> None:
    assert parse_members("<html><body><p>hi</p></body></html>") == []


# ---------------------------------------------------------------------------
# enrich_nvca: build-hook integration
# ---------------------------------------------------------------------------
class _StubClient:
    """Drop-in for NvcaClient — returns a canned member list without I/O."""

    def __init__(self, members: list[NvcaMember]) -> None:
        self._members = members
        self.closed = False

    def fetch_members(self, force_refresh: bool = False) -> list[NvcaMember]:
        return self._members

    def close(self) -> None:
        self.closed = True


def test_enrich_nvca_flags_matches_and_backfills_website_only_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = [
        NvcaMember(name="Sequoia Capital", website="https://www.sequoiacap.com/",
                   hq_city="Menlo Park", hq_state="CA"),
        # Legal-suffix mismatch on our side; dedup_key should bridge it.
        NvcaMember(name="Andreessen Horowitz", website="https://a16z.com/",
                   hq_city="Menlo Park", hq_state="CA"),
        NvcaMember(name="No-Match Ventures", website="https://x.com",
                   hq_city="Austin", hq_state="TX"),
    ]
    stub = _StubClient(members)
    monkeypatch.setattr(build_mod, "NvcaClient", lambda **_kw: stub)

    firms = [
        # Already has a website — must NOT be overwritten.
        {"id": "sequoia", "name": "Sequoia Capital, L.P.",
         "website": "https://sequoiacap.com/old", "sectors": ["enterprise"]},
        # Missing website — should be backfilled from NVCA.
        {"id": "a16z", "name": "Andreessen Horowitz LLC",
         "website": None, "sectors": []},
        # Not in NVCA — should be left untouched, no nvca_member flag.
        {"id": "random", "name": "Some Random Capital",
         "website": "https://random.example", "sectors": ["seed"]},
    ]
    enrich_nvca(firms)

    sequoia, a16z, random = firms
    assert sequoia["nvca_member"] is True
    assert sequoia["website"] == "https://sequoiacap.com/old"  # preserved
    assert a16z["nvca_member"] is True
    assert a16z["website"] == "https://a16z.com/"  # backfilled
    assert "nvca_member" not in random
    assert stub.closed is True


def test_nvca_client_uses_cache_when_fresh(tmp_path) -> None:
    cache = tmp_path / "nvca-cache.json"
    cache.write_text(json.dumps({
        "version": 1,
        "fetched_at": "2099-01-01T00:00:00+00:00",  # far future = always fresh
        "count": 1,
        "members": [
            {"name": "Cached Capital", "website": "https://cached.example",
             "hq_city": "SF", "hq_state": "CA", "sector_focus": None},
        ],
    }))

    class _Boom:
        def get(self, *a, **kw):  # pragma: no cover - must not be called
            raise AssertionError("network must not be touched on cache hit")

        def close(self) -> None:
            pass

    client = NvcaClient(client=_Boom(), cache_path=cache)
    members = client.fetch_members()
    assert len(members) == 1
    assert members[0].name == "Cached Capital"
    assert members[0].website == "https://cached.example"
