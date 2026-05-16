"""LLM-assisted enrichment via Gemini 2.5 Flash on Vertex AI with Google Search.

For each lite firm without a hand-curated profile (no seed YAML entry), call
``gemini-2.5-flash`` via the Vertex AI endpoint with the ``googleSearch``
tool to research the firm on the live web and return a JSON profile: website,
founded year, stages, sectors, partners (max 6), recent investments (max 5),
1-2 sentence notes, and a self-assessed confidence score (0.0-1.0).

Auth
----
Uses Application Default Credentials (ADC) via ``gcloud``. Set up once:

    gcloud auth application-default login
    gcloud auth application-default set-quota-project PROJECT_NUMBER

Tokens auto-refresh when they expire (~1 hour).  Falls back to API-key
auth against the AI Studio endpoint if ``GEMINI_API_KEY`` is set (useful
for testing on the free tier).

API quirk: Gemini does not allow combining the ``googleSearch`` tool
with ``responseMimeType: "application/json"``. We work around this by
asking for JSON in the prompt and parsing the response ourselves, with
a regex fallback for cases where the model wraps the JSON in
triple-backtick fences.

Cache
-----
Permanent cache at ``data/.llm-enrich-cache.json``, keyed by firm id. Each
entry stores the parsed result, source URLs, token counts, and timestamp,
so re-runs are free and we can audit which firms got fresh data.

Throttling
----------
Default: 2s between calls on the paid Vertex AI tier (~30 RPM).
Override with the ``GEMINI_MIN_INTERVAL`` env var.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vertex AI endpoint (primary) — bills to GCP project.
# Falls back to AI Studio endpoint if GEMINI_API_KEY is set.
# ---------------------------------------------------------------------------
VERTEX_URL = (
    "https://{region}-aiplatform.googleapis.com/v1/projects/{project}"
    "/locations/{region}/publishers/google/models/{model}:generateContent"
)
AI_STUDIO_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_REGION = os.environ.get("GEMINI_REGION", "us-central1")
DEFAULT_PROJECT = os.environ.get("GCP_PROJECT", "889202875087")
DEFAULT_CACHE = pathlib.Path("data/.llm-enrich-cache.json")
CACHE_VERSION = 1

# Throttle: Vertex AI paid tier supports ~360 RPM for Flash.  2s/call is
# conservative (~30 RPM) and avoids bursting the grounding-search quota.
DEFAULT_MIN_INTERVAL = float(os.environ.get("GEMINI_MIN_INTERVAL", "2.0"))

# On 429, sleep this long and retry once before giving up on a firm.
QUOTA_BACKOFF_SECONDS = 30.0

# Allowed taxonomy values — mirror site/app.js STAGE_LABELS / SECTOR_LABELS
# so the LLM doesn't invent tags the frontend can't render.
STAGES = (
    "pre_seed", "seed", "series_a", "series_b", "series_c",
    "series_d", "series_e", "growth", "late",
)
SECTORS = (
    "enterprise_saas", "consumer", "fintech", "ai_infra", "crypto",
    "healthcare", "bio", "therapeutics", "climate", "deep_tech",
    "dev_tools", "security", "data", "infra", "industrial",
    "marketplace", "consumer_social", "defense", "gaming", "mobile",
    "cross_border", "cloud", "generalist", "governance",
)

# Minimum confidence to actually merge LLM data into the firm dict.
# Below this we still cache the negative result so we don't re-query.
MIN_CONFIDENCE_TO_MERGE = 0.5


PROMPT_TEMPLATE = """Research the venture capital firm "{name}" (located at {address}, SEC CRD {crd}).
Use Google Search to find current public information.

Reply with ONLY a JSON object — no prose, no markdown fences.

Schema:
{{
  "website": string or null,
  "founded": integer year or null,
  "stages": list of strings from {stages},
  "sectors": list of strings from {sectors},
  "partners": [
    {{"name": string, "title": string or null}}
  ],
  "recent_investments": [
    {{"company": string, "year": integer or null, "stage": string or null}}
  ],
  "notes": "1-2 sentences on investment thesis / focus",
  "confidence": float 0.0-1.0
}}

Rules:
- stages and sectors MUST come from the allowed lists above. Use empty list if uncertain.
- partners: max 6, most senior first (Managing Partner, General Partner, founder).
- recent_investments: max 5, prefer 2023-2026 deals. Include stage if known (e.g. "series_b").
- confidence: 0.0 if firm cannot be confidently identified online; 0.5 partial data;
  0.85+ confident match with multiple sources.
- Never invent partners or investments. An empty list beats a fabricated entry.
- If "{name}" appears to be a holding-company shell with no public profile, return confidence < 0.3.
"""


@dataclass
class EnrichmentResult:
    """Parsed Gemini response + metadata. Stored in the cache verbatim."""
    website: Optional[str] = None
    founded: Optional[int] = None
    stages: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    partners: list[dict] = field(default_factory=list)
    recent_investments: list[dict] = field(default_factory=list)
    notes: Optional[str] = None
    confidence: float = 0.0
    sources: list[str] = field(default_factory=list)
    model: str = DEFAULT_MODEL
    prompt_tokens: int = 0
    output_tokens: int = 0
    ts: str = ""


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
# Greedy fence match — non-greedy `\{.*?\}` would stop at the first nested `}`
# (e.g. inside a partner entry) and break parsing for any non-flat JSON.
# Greedy is fine here because we anchor on the closing ``` fence.
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def _extract_json(text: str) -> Optional[dict]:
    """Pull a JSON object out of an LLM response that may be fenced or have prose."""
    if not text:
        return None
    m = _JSON_FENCE_RE.search(text)
    candidate = m.group(1) if m else None
    if candidate is None:
        # No fence — find the first balanced {...} block via brace counting,
        # since regex alone can't handle nested objects reliably.
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        end = -1
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            return None
        candidate = text[start:end]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _coerce_string_list(raw, allowed: tuple[str, ...]) -> list[str]:
    """Keep only allowed values, normalised to snake_case, deduped, in order.

    LLMs sometimes return the human-readable form ("Series A", "Enterprise SaaS")
    even when the prompt asks for snake_case enum values, so we normalise
    whitespace/hyphens to underscores before checking membership.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen = set()
    allowed_set = set(allowed)
    for v in raw:
        if not isinstance(v, str):
            continue
        key = v.strip().lower()
        key = re.sub(r"[\s\-/]+", "_", key)  # "Series A" -> "series_a"
        if key in allowed_set and key not in seen:
            seen.add(key); out.append(key)
    return out


def _coerce_partners(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({
            "name": name,
            "title": (entry.get("title") or "").strip() or None,
        })
        if len(out) >= 6:
            break
    return out


def _coerce_investments(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        company = (entry.get("company") or "").strip()
        if not company or company.lower() in seen:
            continue
        seen.add(company.lower())
        year = entry.get("year")
        if not isinstance(year, int) or year < 2000 or year > 2030:
            year = None
        stage = (entry.get("stage") or "").strip().lower() or None
        if stage and stage not in STAGES:
            stage = None  # drop unknown stage rather than poison the schema
        out.append({"company": company, "year": year, "stage": stage})
        if len(out) >= 5:
            break
    return out


def _parse_response(raw_text: str, sources: list[str], usage: dict, model: str) -> Optional[EnrichmentResult]:
    parsed = _extract_json(raw_text)
    if not isinstance(parsed, dict):
        return None
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    founded = parsed.get("founded")
    if not isinstance(founded, int) or founded < 1800 or founded > 2030:
        founded = None
    website = parsed.get("website")
    if isinstance(website, str):
        website = website.strip() or None
        if website and not re.match(r"^https?://", website, re.IGNORECASE):
            website = "https://" + website.lstrip("/")
    else:
        website = None
    return EnrichmentResult(
        website=website,
        founded=founded,
        stages=_coerce_string_list(parsed.get("stages"), STAGES),
        sectors=_coerce_string_list(parsed.get("sectors"), SECTORS),
        partners=_coerce_partners(parsed.get("partners")),
        recent_investments=_coerce_investments(parsed.get("recent_investments")),
        notes=(parsed.get("notes") or "").strip() or None,
        confidence=confidence,
        sources=sources,
        model=model,
        prompt_tokens=int(usage.get("promptTokenCount", 0) or 0),
        output_tokens=int(usage.get("candidatesTokenCount", 0) or 0),
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _grounding_sources(candidate: dict) -> list[str]:
    gm = candidate.get("groundingMetadata") or {}
    out: list[str] = []
    for chunk in gm.get("groundingChunks") or []:
        web = chunk.get("web") or {}
        uri = web.get("uri")
        if uri and uri not in out:
            out.append(uri)
    return out


# ---------------------------------------------------------------------------
# OAuth token management (Vertex AI)
# ---------------------------------------------------------------------------
def _get_access_token() -> str:
    """Get an OAuth access token via gcloud ADC. Raises RuntimeError on failure."""
    try:
        result = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"gcloud auth failed (exit {result.returncode}): {result.stderr.strip()}"
            )
        token = result.stdout.strip()
        if not token:
            raise RuntimeError("gcloud returned empty access token")
        return token
    except FileNotFoundError:
        raise RuntimeError(
            "gcloud CLI not found. Install: brew install google-cloud-sdk\n"
            "Then: gcloud auth application-default login"
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class GeminiEnricher:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        project: str = DEFAULT_PROJECT,
        region: str = DEFAULT_REGION,
        client: Optional[httpx.Client] = None,
        cache_path: Optional[pathlib.Path] = None,
        min_interval: float = DEFAULT_MIN_INTERVAL,
    ) -> None:
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self._project = project
        self._region = region
        self._use_vertex = not self._api_key  # Vertex AI when no API key
        if self._use_vertex:
            # Verify gcloud works before starting the long run.
            self._access_token = _get_access_token()
            self._token_fetched_at = time.monotonic()
            log.info("Using Vertex AI endpoint (project=%s, region=%s)", project, region)
        else:
            self._access_token = ""
            self._token_fetched_at = 0.0
            log.info("Using AI Studio endpoint (API key)")
        self._model = model
        self._client = client or httpx.Client(timeout=120.0)
        self._cache_path = cache_path or DEFAULT_CACHE
        self._cache: dict[str, dict] = self._load_cache()
        self._min_interval = min_interval
        self._last_request = 0.0

    def _load_cache(self) -> dict[str, dict]:
        if not self._cache_path.exists():
            return {}
        try:
            blob = json.loads(self._cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(blob, dict) or blob.get("version") != CACHE_VERSION:
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

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()

    def _build_body(self, firm: dict) -> dict:
        prompt = PROMPT_TEMPLATE.format(
            name=firm["name"],
            address=firm.get("address", "unknown"),
            crd=firm.get("sec_crd", "unknown"),
            stages=list(STAGES),
            sectors=list(SECTORS),
        )
        # Vertex AI requires "role" in contents; AI Studio accepts it too.
        # Vertex AI uses camelCase "googleSearch"; AI Studio uses "google_search".
        tool_key = "googleSearch" if self._use_vertex else "google_search"
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{tool_key: {}}],
            "generationConfig": {"temperature": 0.2},
        }

    def _refresh_token_if_needed(self) -> None:
        """Refresh the OAuth token if it's older than 45 minutes."""
        if not self._use_vertex:
            return
        age = time.monotonic() - self._token_fetched_at
        if age > 2700:  # 45 min (tokens expire at 60 min)
            log.info("Refreshing OAuth token (age=%.0fs)", age)
            self._access_token = _get_access_token()
            self._token_fetched_at = time.monotonic()

    def _get_url(self) -> str:
        if self._use_vertex:
            return VERTEX_URL.format(
                region=self._region,
                project=self._project,
                model=self._model,
            )
        return AI_STUDIO_URL.format(model=self._model, key=self._api_key)

    def _get_headers(self) -> dict[str, str]:
        if self._use_vertex:
            return {
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            }
        return {"Content-Type": "application/json"}

    def enrich(self, firm: dict, *, force: bool = False) -> Optional[EnrichmentResult]:
        """Return EnrichmentResult or None. Reads/writes the cache."""
        firm_id = firm.get("id")
        if not firm_id:
            return None
        if not force and firm_id in self._cache:
            cached = self._cache[firm_id]
            return EnrichmentResult(**cached)

        self._refresh_token_if_needed()
        url = self._get_url()
        body = self._build_body(firm)
        # One implicit retry: a 429 often clears in <30s on the paid tier
        # once the rolling window slides. Beyond that we re-raise
        # QuotaExceeded so the caller can stop the run.
        for attempt in (1, 2):
            self._throttle()
            try:
                resp = self._client.post(url, json=body, headers=self._get_headers())
            except httpx.HTTPError as e:
                log.warning("Gemini request failed for %s: %s", firm_id, e)
                return None
            if resp.status_code != 429:
                break
            if attempt == 1:
                log.info(
                    "429 from Gemini for %s; waiting %.0fs then retrying once",
                    firm_id, QUOTA_BACKOFF_SECONDS,
                )
                time.sleep(QUOTA_BACKOFF_SECONDS)
                continue
            raise QuotaExceeded(f"429 persists for {firm_id}: {resp.text[:200]}")
        if resp.status_code != 200:
            log.warning(
                "Gemini %s for %s: %s", resp.status_code, firm_id, resp.text[:300]
            )
            return None
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            log.warning("Gemini returned no candidates for %s", firm_id)
            return None
        cand = candidates[0]
        parts = cand.get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        usage = data.get("usageMetadata", {})
        sources = _grounding_sources(cand)
        result = _parse_response(text, sources, usage, self._model)
        if result is None:
            log.warning("Could not parse JSON from Gemini for %s", firm_id)
            return None
        # Always cache — even low-confidence — so a retry doesn't re-spend tokens.
        self._cache[firm_id] = {
            "website": result.website,
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
        self._client.close()


class QuotaExceeded(RuntimeError):
    """Raised when Gemini returns HTTP 429. Caller should stop and resume later."""


# ---------------------------------------------------------------------------
# Merge helper (called from build.py)
# ---------------------------------------------------------------------------
def merge_into_firm(firm: dict, info: EnrichmentResult) -> bool:
    """Fill missing fields on `firm` from `info`. Returns True if any change applied.

    Strictly additive: never overwrites a seed value or a value already filled
    by a higher-priority enricher (SEC bulk, Wikipedia).
    """
    if info.confidence < MIN_CONFIDENCE_TO_MERGE:
        return False
    changed = False
    if not firm.get("website") and info.website:
        firm["website"] = info.website; changed = True
    if not firm.get("founded") and info.founded:
        firm["founded"] = info.founded; changed = True
    if not firm.get("stages") and info.stages:
        firm["stages"] = info.stages; changed = True
    if not firm.get("sectors") and info.sectors:
        firm["sectors"] = info.sectors; changed = True
    if not firm.get("partners") and info.partners:
        firm["partners"] = info.partners; changed = True
    if not firm.get("recent_portfolio_sample") and info.recent_investments:
        firm["recent_portfolio_sample"] = info.recent_investments; changed = True
    if not firm.get("notes") and info.notes:
        firm["notes"] = info.notes; changed = True
    if changed:
        firm["llm_enriched"] = True
        firm["llm_confidence"] = info.confidence
        firm["llm_sources"] = info.sources
    return changed
