from __future__ import annotations

import httpx

from scraper.edgar import IapdClient


def make_client(hits: list[dict]) -> IapdClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": {"hits": hits}})

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return IapdClient(client=http)


def test_fetch_by_crd_returns_aum() -> None:
    hits = [
        {
            "_source": {
                "org_crd": "157518",
                "org_name": "Sequoia Capital Operations, LLC",
                "firm_ia_aum": "85000000000",
                "firm_ia_aum_date": "2025-03-31",
                "firm_latest_adv_filing_date": "2025-04-15",
            }
        }
    ]
    client = make_client(hits)
    info = client.fetch_by_crd("157518")
    assert info is not None
    assert info.aum_usd == 85_000_000_000
    assert info.aum_as_of == "2025-03-31"


def test_fetch_by_crd_skips_mismatched_hit() -> None:
    hits = [
        {"_source": {"org_crd": "999999", "org_name": "Other LLC", "firm_ia_aum": "1"}},
    ]
    client = make_client(hits)
    assert client.fetch_by_crd("157518") is None


def test_fetch_by_name_returns_top_hit() -> None:
    hits = [
        {
            "_source": {
                "org_crd": "1",
                "org_name": "Top Hit",
                "firm_ia_aum": "1000",
            }
        },
        {"_source": {"org_crd": "2", "org_name": "Second", "firm_ia_aum": "2000"}},
    ]
    client = make_client(hits)
    info = client.fetch_by_name("anything")
    assert info is not None
    assert info.legal_name == "Top Hit"
    assert info.aum_usd == 1000


def test_fetch_by_name_handles_no_results() -> None:
    client = make_client([])
    assert client.fetch_by_name("nothing") is None


def test_parse_handles_missing_aum() -> None:
    hits = [{"_source": {"org_crd": "1", "org_name": "X", "firm_ia_aum": ""}}]
    client = make_client(hits)
    info = client.fetch_by_crd("1")
    assert info is not None
    assert info.aum_usd is None
