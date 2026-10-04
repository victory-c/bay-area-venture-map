from __future__ import annotations


def test_sector_enricher_chat_uses_runtime_api_key(monkeypatch, tmp_path) -> None:
    captured = {}

    class _Response:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}]}

    def fake_post(url, *, headers, json, timeout):
        captured["headers"] = headers
        return _Response()

    monkeypatch.setenv("ZHIPU_API_KEY", "sentinel-zhipu-key")
    monkeypatch.setattr("scraper.glm_enrich.httpx.post", fake_post)

    from scraper.glm_enrich import GlmSectorEnricher

    enricher = GlmSectorEnricher(cache_path=tmp_path / "cache.json")
    enricher._chat("test prompt")

    assert captured["headers"]["Authorization"] == "Bearer sentinel-zhipu-key"