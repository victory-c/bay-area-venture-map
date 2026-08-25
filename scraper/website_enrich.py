"""Firm-website enrichment for lite firms with no other LLM signal.

What this is for
----------------
After the Tier-D LLM pass, ~46 lite firms still have zero partners /
sectors / stages — Vertex AI either returned None or the result fell
below the merge confidence threshold. About half of those (~24) have a
``website`` field that points at something more substantial than a
Facebook / Twitter handle. This module is a targeted last-mile pass that
fetches those sites' HTML, strips it to readable text, and asks Gemini
to extract partners + sectors + stages from the *supplied text*.

Key differences vs. Tier-D (``llm_enrich.py``):

  * No Google Search grounding — we provide the source text directly,
    so the model is grounding on the firm's own /about and /team pages,
    not the open web.
  * Smaller per-firm cost: ~3 page fetches @ ≤10 KB each + one Gemini
    call with the cleaned text in the prompt.
  * Narrower target population: lite firms missing data AND with a
    real website (not a social URL).

Data flow
---------
For each candidate firm:

  1. Filter the website (skip facebook / x.com / linkedin standalone
     profiles — those URLs aren't scrapable for team / portfolio info).
  2. Fetch the home page; on success, also try ``/team``, ``/about``,
     ``/people``, ``/portfolio``. Skip silently on 4xx / timeout.
  3. Extract text from each fetched page (selectolax), de-junk, cap
     each page at 6 KB.
  4. Concatenate and send to Vertex AI Gemini with an extraction prompt
     that asks for partners / sectors / stages / notes / confidence.
  5. Parse and merge using the same ``merge_into_firm`` semantics as
     Tier-D (≥0.50 confidence required).

Cache
-----
``data/.website-enrich-cache.json``, keyed by firm id. Same shape as the
Tier-D cache so the per-firm enrichment story stays consistent. Bump
``CACHE_VERSION`` when the extraction prompt or parsing logic changes.

Throttling
----------
Per-host: 1.0 s between page fetches against the same firm site (polite).
Per-Gemini-call: shares the same 2.0 s default with Tier-D.
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from scraper.useragent import USER_AGENT as _UA
from selectolax.parser import HTMLParser

from scraper.llm_enrich import (
    DEFAULT_MODEL,
    DEFAULT_PROJECT,
    DEFAULT_REGION,
    SECTORS,
    STAGES,
    VERTEX_URL,
    EnrichmentResult,
    QuotaExceeded,
    _get_access_token,
    _parse_response,
)

log = logging.getLogger(__name__)

CACHE_VERSION = 1
DEFAULT_CACHE = pathlib.Path("data/.website-enrich-cache.json")

# Browser-shaped prefix on purpose: some VC marketing sites 403 a bare
# bot UA. The contact is still the real one from scraper.useragent.
USER_AGENT = f"Mozilla/5.0 (compatible; {_UA})"

# Common subpaths on VC firm sites where partner / sector content lives.
# Ordered roughly by hit frequency in spot-checks.
CANDIDATE_PATHS = ("/", "/team", "/about", "/people", "/portfolio")

PER_PAGE_TEXT_CAP = 6000  # chars; keeps each Gemini prompt under ~10k chars total
PER_HOST_INTERVAL = 1.0    # seconds between fetches against the same host
PAGE_TIMEOUT = 15.0

# URLs we KNOW won't yield useful structured info; skip without fetching.
_SOCIAL_HOST_BLACKLIST = (
    "facebook.com", "fb.com", "instagram.com",
    "twitter.com", "x.com",
    "linkedin.com",     # individual profiles, not company pages we can scrape
    "youtube.com",
    "medium.com",
    "substack.com",
)

EXTRACTION_PROMPT = """You are extracting structured info about a venture capital firm from its own website text.

Firm name: {name}
Source pages: {urls}

Below is the CLEANED TEXT from the firm's website (home + about / team / portfolio pages where available). Read it carefully and extract what you can VERIFY from this text alone — do NOT invent data. If the text doesn't say it, leave the field empty or null.

--- BEGIN WEBSITE TEXT ---
{text}
--- END WEBSITE TEXT ---

Return ONLY a JSON object with these keys (no prose, no markdown fences around prose):

{{
  "website": "<canonical https URL, or null>",
  "founded": <4-digit year or null>,
  "stages": [<subset of {stages}>],
  "sectors": [<subset of {sectors}>],
  "partners": [
    {{"name": "<full name>", "title": "<role, or null>"}}
  ],
  "recent_investments": [
    {{"company": "<portfolio company>", "year": <year or null>, "stage": "<stage or null>"}}
  ],
  "notes": "<2-3 sentence factual summary, or empty string>",
  "confidence": <0.0 - 1.0 — how confident the EXTRACTED fields are, given the source text>
}}

Confidence rubric:
  0.9+  : explicit team page + sector descriptions + named investments
  0.7-0.9: partial team or sector data, lightly described
  0.5-0.7: only firm description, no people or portfolio
  <0.5  : almost nothing on the page is usable

Use AT MOST 6 partners and 5 recent_investments. Drop anyone whose role on the page isn't clearly an investor (junior associates / interns / EAs)."""


class WebsiteEnricher:
    def __init__(
        self,
        *,
        project: str = DEFAULT_PROJECT,
        region: str = DEFAULT_REGION,
        model: str = DEFAULT_MODEL,
        cache_path: Optional[pathlib.Path] = None,
        page_client: Optional[httpx.Client] = None,
        gemini_client: Optional[httpx.Client] = None,
    ) -> None:
        self._project = project
        self._region = region
        self._model = model
        self._access_token = _get_access_token()
        self._token_fetched_at = time.monotonic()
        self._page_client = page_client or httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=PAGE_TIMEOUT,
            follow_redirects=True,
        )
        self._gemini_client = gemini_client or httpx.Client(timeout=120.0)
        self._cache_path = cache_path or DEFAULT_CACHE
        self._cache = self._load_cache()
        # Per-host last-fetch timestamps for polite scraping.
        self._host_last: dict[str, float] = {}

    # ----- caching -------------------------------------------------------

    def _load_cache(self) -> dict[str, dict]:
        if not self._cache_path.exists():
            return {}
        try:
            blob = json.loads(self._cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if blob.get("version") != CACHE_VERSION:
            return {}
        entries = blob.get("entries")
        return entries if isinstance(entries, dict) else {}

    def _save_cache(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps({
            "version": CACHE_VERSION,
            "entries": self._cache,
        }, indent=2))
        tmp.replace(self._cache_path)

    # ----- page fetching -------------------------------------------------

    def _host_throttle(self, host: str) -> None:
        last = self._host_last.get(host, 0.0)
        elapsed = time.monotonic() - last
        if elapsed < PER_HOST_INTERVAL:
            time.sleep(PER_HOST_INTERVAL - elapsed)
        self._host_last[host] = time.monotonic()

    def _fetch_pages(self, website: str) -> tuple[list[str], list[str]]:
        """Return (urls_fetched, cleaned_texts_per_page)."""
        parsed = urlparse(website)
        host = (parsed.hostname or "").lower()
        urls: list[str] = []
        texts: list[str] = []
        for path in CANDIDATE_PATHS:
            url = urljoin(website, path)
            self._host_throttle(host)
            try:
                resp = self._page_client.get(url)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                log.debug("fetch fail %s: %s", url, e)
                continue
            if resp.status_code >= 400:
                continue
            if "text/html" not in resp.headers.get("content-type", ""):
                continue
            text = _html_to_text(resp.text)
            if not text:
                continue
            urls.append(str(resp.url))
            texts.append(text[:PER_PAGE_TEXT_CAP])
            # Home page alone is often enough; keep going through the list
            # but bail early once we've collected 4 useful pages.
            if len(texts) >= 4:
                break
        return urls, texts

    # ----- Vertex AI call -----------------------------------------------

    def _refresh_token_if_needed(self) -> None:
        age = time.monotonic() - self._token_fetched_at
        if age > 2700:  # 45 min
            log.info("Refreshing OAuth token (age=%.0fs)", age)
            self._access_token = _get_access_token()
            self._token_fetched_at = time.monotonic()

    def _call_gemini(self, prompt: str) -> Optional[dict]:
        self._refresh_token_if_needed()
        url = VERTEX_URL.format(
            region=self._region, project=self._project, model=self._model,
        )
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        }
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        try:
            resp = self._gemini_client.post(url, json=body, headers=headers)
        except httpx.HTTPError as e:
            log.warning("Gemini POST failed: %s", e)
            return None
        if resp.status_code == 429:
            raise QuotaExceeded(f"429 from Vertex AI: {resp.text[:200]}")
        if resp.status_code != 200:
            log.warning("Vertex AI %s: %s", resp.status_code, resp.text[:200])
            return None
        return resp.json()

    # ----- public entry point -------------------------------------------

    def enrich(self, firm: dict) -> Optional[EnrichmentResult]:
        firm_id = firm.get("id")
        if not firm_id:
            return None
        if firm_id in self._cache:
            cached = self._cache[firm_id]
            return EnrichmentResult(**cached) if cached else None

        website = (firm.get("website") or "").strip()
        if not website or not _is_scrapable_url(website):
            self._cache[firm_id] = None
            self._save_cache()
            return None

        urls, texts = self._fetch_pages(website)
        if not texts:
            log.info("No pages fetched for %s (%s)", firm_id, website)
            self._cache[firm_id] = None
            self._save_cache()
            return None

        combined_text = "\n\n---\n\n".join(texts)
        prompt = EXTRACTION_PROMPT.format(
            name=firm["name"],
            urls=urls,
            text=combined_text,
            stages=list(STAGES),
            sectors=list(SECTORS),
        )
        data = self._call_gemini(prompt)
        if data is None:
            return None  # don't cache — transient failure, let re-run retry
        candidates = data.get("candidates") or []
        if not candidates:
            log.warning("Vertex returned no candidates for %s", firm_id)
            self._cache[firm_id] = None
            self._save_cache()
            return None
        cand = candidates[0]
        parts = cand.get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        usage = data.get("usageMetadata", {})
        result = _parse_response(text, urls, usage, self._model)
        if result is None:
            log.warning("Could not parse JSON from Vertex for %s", firm_id)
            self._cache[firm_id] = None
            self._save_cache()
            return None
        self._cache[firm_id] = {
            "website": result.website or website,
            "founded": result.founded,
            "stages": result.stages,
            "sectors": result.sectors,
            "partners": result.partners,
            "recent_investments": result.recent_investments,
            "notes": result.notes,
            "confidence": result.confidence,
            "sources": result.sources,
            "model": result.model,
            "prompt_tokens": result.prompt_tokens,
            "output_tokens": result.output_tokens,
            "ts": result.ts,
        }
        self._save_cache()
        return result

    def close(self) -> None:
        self._page_client.close()
        self._gemini_client.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_scrapable_url(url: str) -> bool:
    """Drop social / aggregator URLs we know we can't extract from."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    for blocked in _SOCIAL_HOST_BLACKLIST:
        if host == blocked or host.endswith("." + blocked):
            return False
    return True


# Tags whose text content is pure layout noise — drop them entirely.
_NOISE_TAGS = ("script", "style", "noscript", "svg", "header", "footer", "nav")


def _html_to_text(html: str) -> str:
    """Strip HTML to a readable text blob: nav / footer / scripts removed,
    whitespace collapsed, paragraphs separated by blank lines."""
    try:
        tree = HTMLParser(html)
    except Exception:  # noqa: BLE001 - arbitrary scraped HTML must degrade, not crash
        return ""
    for tag in _NOISE_TAGS:
        for node in tree.css(tag):
            node.decompose()
    body = tree.body if tree.body else tree.root
    if body is None:
        return ""
    raw = body.text(separator="\n", strip=True)
    # Collapse runs of >2 blank lines and trim each line.
    lines = [ln.strip() for ln in raw.splitlines()]
    cleaned: list[str] = []
    blank_streak = 0
    for ln in lines:
        if ln:
            cleaned.append(ln)
            blank_streak = 0
        else:
            blank_streak += 1
            if blank_streak <= 1:
                cleaned.append("")
    return "\n".join(cleaned).strip()
