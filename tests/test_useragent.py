"""SEC enforces two rules on the User-Agent; both are regressions we've hit."""
from __future__ import annotations

from scraper.useragent import DEFAULT_CONTACT, build_user_agent


def test_an_email_contact_is_embedded() -> None:
    ua = build_user_agent("ops@example.org")
    assert "ops@example.org" in ua
    assert ua.startswith("Sand Hill VC Map")


def test_falls_back_to_the_placeholder_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("SCRAPER_CONTACT", raising=False)
    assert DEFAULT_CONTACT in build_user_agent()


def test_reads_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("SCRAPER_CONTACT", "someone@example.net")
    assert "someone@example.net" in build_user_agent()


def test_never_emits_a_bare_url_by_default(monkeypatch) -> None:
    # Regression: defaulting the contact to the repo URL 403s every SEC host.
    monkeypatch.delenv("SCRAPER_CONTACT", raising=False)
    ua = build_user_agent()
    assert "://" not in ua
    assert "github.com" not in ua


def test_warns_when_the_contact_is_a_url(monkeypatch, caplog) -> None:
    monkeypatch.setenv("SCRAPER_CONTACT", "https://github.com/victory-c/x")
    with caplog.at_level("WARNING"):
        build_user_agent()
    assert any("403" in r.getMessage() for r in caplog.records)


def test_every_scraper_module_shares_the_one_string() -> None:
    from scraper import edgar, form_d, geocode, nvca, sec_bulk, wikipedia
    from scraper.useragent import USER_AGENT

    for mod in (edgar, form_d, geocode, nvca, sec_bulk, wikipedia):
        assert mod.USER_AGENT == USER_AGENT, mod.__name__
