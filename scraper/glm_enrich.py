"""Sector/stage tagging for lite firms via Zhipu GLM-4.6 (parametric).

Why this exists
---------------
Only the ~25 hand-curated marquee firms carried ``stages``/``sectors``
tags, so any stage/sector filter collapsed the whole 650+-firm directory
to that handful. This enriches the long tail: for each lite SEC firm it
asks GLM-4.6 to classify the firm's investment SECTORS and STAGES from the
firm name + its SEC Form D fund-vehicle names + type tags — no web scrape,
purely the model's knowledge grounded on those signals.

Design decisions (validated on a 40-firm pilot + 20-firm adversarial
web-verification, see PR):

  * **Parametric, not website-scrape.** Small VC sites are mostly SPA /
    thin / people-only; the firm name + Form D fund names + GLM knowledge
    tag recognizable firms far better and dodge the fetch entirely.
  * **thinking disabled.** GLM-4.6 is a reasoning model; disabling its
    thinking cuts latency ~6x (~1.7s vs ~18s) and tokens ~8x with no loss
    of accuracy for this constrained-vocab classification.
  * **Tight prompt** (1-3 sectors, ``[]`` when unsure) — obscure shell
    entities correctly come back empty rather than hallucinated; the
    confidence gate (>= ``THRESHOLD``) drops the rest.
  * **Conservative concurrency (2).** Higher rates trip Zhipu's cap and
    produce sustained 429s.

Merged tags are flagged ``inferred: true`` with ``inference_confidence`` /
``inference_basis`` / ``inference_model`` so the UI can mark them as
AI-inferred and offer a "verified only" filter — they are NOT hand-checked.

Auth
----
Reads ``ZHIPU_API_KEY`` from the environment (keep it in a gitignored
``.env``; never commit it). Uses Zhipu's OpenAI-compatible v4 endpoint, so
no GCP/Vertex billing is involved.

Cache
-----
``data/.glm-enrich-cache.json`` (gitignored), keyed by firm id. Errors are
cached distinctly so a re-run retries them but skips good results.

Run
---
    ZHIPU_API_KEY=... python -m scraper.glm_enrich

Enriches ``data/firms.json`` in place (and the ``site/`` copy). Runs
against the existing firms.json rather than ``scraper.build`` on purpose:
a bare build rebuilds from seed and would drop the 650 SEC lite firms.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from scraper.llm_enrich import SECTORS, STAGES

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_OUT = REPO_ROOT / "data" / "firms.json"
SITE_OUT = REPO_ROOT / "site" / "firms.json"
CACHE = REPO_ROOT / "data" / ".glm-enrich-cache.json"
THESIS_CACHE = REPO_ROOT / "data" / ".glm-thesis-cache.json"

BASE = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = "glm-4.6"
THRESHOLD = 0.7          # min self-confidence to merge a sector tag
THESIS_THRESHOLD = 0.6   # min self-confidence to merge a one-line thesis
MAX_SECTORS = 4
CONCURRENCY = 2          # Zhipu rate cap trips above this
BACKOFF = [8, 25, 60, 90]

PROMPT = """You are classifying a US venture capital / investment firm by the SECTORS and STAGES it invests in, for a founder-facing directory.

Firm name: {name}
Address: {addr}
SEC Form D fund vehicle names: {funds}
Firm type tags: {types}

Rules:
- List ONLY the 1-3 sectors this firm is MOST clearly and specifically known for. Do NOT add speculative or secondary sectors; when unsure about a sector, leave it out. Use "generalist" only for firms that truly invest broadly with no specialization.
- Base this ONLY on the fund vehicle names and specific, well-known public facts about THIS exact firm.
- CRITICAL: if you lack specific knowledge of this exact firm AND the fund names don't clearly indicate a focus, return EMPTY lists. An empty answer is correct and expected — most small/obscure firms should be empty. Do NOT guess from the name alone.
- confidence = how sure you are about THIS specific firm; use < {threshold} when inferring without specific knowledge (it will be discarded).
- sectors and stages MUST come ONLY from these allowed lists:
  SECTORS = {sectors}
  STAGES = {stages}

Return ONLY JSON (no fences):
{{"sectors":[...],"stages":[...],"confidence":0.0-1.0,"basis":"<=15 words: specifically what you based it on"}}"""

THESIS_PROMPT = """Write a ONE-sentence investment thesis for this venture firm, for a founder deciding whether to pitch it.

Firm: {name}
Established focus — sectors: {sectors}, stages: {stages}
SEC Form D fund vehicle names: {funds}

Rules:
- ONE sentence, <= 25 words, concrete and specific to THIS firm, consistent with the established focus above.
- If you know a DISTINGUISHING angle for this exact firm — a stage specialty, a technical/thematic focus, a founder's background, a notable strategy — lead with it (e.g. "Crypto-native firm backing early-stage infrastructure and DeFi protocols, run by former Coinbase operators.").
- Only if you have no distinguishing knowledge, fall back to a plain factual sentence from the sectors/stages (e.g. "Backs seed and Series A fintech and enterprise-software startups.").
- Do NOT invent specific portfolio companies, fund sizes, named people, or claims you are unsure of.
- confidence 0-1: how sure you are this thesis is accurate for THIS specific firm.

Return ONLY JSON (no fences):
{{"thesis":"...","confidence":0.0-1.0}}"""


def _parse(content: str):
    m = re.search(r"\{.*\}", content, re.DOTALL)
    return json.loads(m.group(0)) if m else None


class GlmSectorEnricher:
    def __init__(self, cache_path: pathlib.Path = CACHE):
        key = os.environ.get("ZHIPU_API_KEY")
        if not key:
            raise SystemExit("ZHIPU_API_KEY not set (put it in a gitignored .env)")
        self._key = key
        self._lock = threading.Lock()
        self._cache = self._load(cache_path)
        self._cache_path = cache_path
        self._thesis_cache = self._load(THESIS_CACHE)
        self._thesis_cache_path = THESIS_CACHE

    @staticmethod
    def _load(path: pathlib.Path) -> dict:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                pass
        return {}

    def _save(self):
        with self._lock:
            for cache, path in ((self._cache, self._cache_path),
                                (self._thesis_cache, self._thesis_cache_path)):
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(cache))
                tmp.replace(path)

    def _chat(self, content: str) -> dict:
        """POST one prompt, return parsed JSON dict, or {"error": ...}."""
        body = {"model": MODEL, "temperature": 0.1, "thinking": {"type": "disabled"},
                "messages": [{"role": "user", "content": content}]}
        last = "unknown"
        for attempt in range(5):
            try:
                r = httpx.post(BASE, headers={"Authorization": f"Bearer {self._key}",
                                              "Content-Type": "application/json"},
                               json=body, timeout=httpx.Timeout(60.0, connect=10.0))
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"HTTP {r.status_code}"
                    time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)]); continue
                if r.status_code != 200:
                    return {"error": f"HTTP {r.status_code}"}
                parsed = _parse(r.json()["choices"][0]["message"]["content"])
                if parsed is None:
                    # An unparseable body is a failure, not a firm with no
                    # sectors. Without the error key it would cache as a
                    # good empty result and never be retried.
                    return {"error": "unparseable_response"}
                return parsed
            except Exception as e:  # noqa: BLE001 — retry any transport error
                last = type(e).__name__
                time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
        return {"error": f"retries_exhausted:{last}"}

    def _call(self, firm: dict) -> dict:
        p = self._chat(PROMPT.format(
            name=firm["name"], addr=firm.get("address", ""),
            funds=(firm.get("form_d_distinct_funds") or [])[:6],
            types=firm.get("firm_type_tags") or [],
            sectors=list(SECTORS), stages=list(STAGES), threshold=THRESHOLD))
        if "error" in p:
            return p
        # dict.fromkeys dedupes while keeping the model's ordering: a repeated
        # tag is one tag, and it would otherwise reach firm.sectors twice and
        # give the site's x-for a duplicate :key.
        secs = list(dict.fromkeys(
            s for s in (p.get("sectors") or []) if s in SECTORS))[:MAX_SECTORS]
        stgs = list(dict.fromkeys(
            s for s in (p.get("stages") or []) if s in STAGES))
        return {"sectors": secs, "stages": stgs, "confidence": p.get("confidence"),
                "basis": p.get("basis")}

    def _thesis_call(self, firm: dict) -> dict:
        p = self._chat(THESIS_PROMPT.format(
            name=firm["name"], sectors=firm.get("sectors") or [],
            stages=firm.get("stages") or [],
            funds=(firm.get("form_d_distinct_funds") or [])[:6]))
        if "error" in p:
            return p
        thesis = (p.get("thesis") or "").strip()
        return {"thesis": thesis, "confidence": p.get("confidence")}

    def _lookup(self, firm: dict, cache: dict, fn) -> dict:
        fid = firm["id"]
        cached = cache.get(fid)
        if cached is not None and "error" not in cached:
            return cached
        res = fn(firm)
        with self._lock:
            cache[fid] = res
        return res

    def lookup(self, firm: dict) -> dict:
        return self._lookup(firm, self._cache, self._call)

    def thesis(self, firm: dict) -> dict:
        return self._lookup(firm, self._thesis_cache, self._thesis_call)


def enrich_glm_sectors(firms: list[dict]) -> int:
    """Tag lite firms in place. Returns the number merged (conf >= THRESHOLD)."""
    enricher = GlmSectorEnricher()
    lite = [f for f in firms if f.get("tier") == "lite"]
    done = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(enricher.lookup, f): f for f in lite}
        for fut in as_completed(futs):
            fut.result(); done += 1
            if done % 50 == 0:
                enricher._save()
    enricher._save()

    merged = 0
    for f in lite:
        res = enricher._cache.get(f["id"]) or {}
        secs = res.get("sectors") or []
        if secs and (res.get("confidence") or 0) >= THRESHOLD:
            f["sectors"] = secs
            if res.get("stages"):
                f["stages"] = res["stages"]
            f["inferred"] = True
            f["inference_confidence"] = res.get("confidence")
            f["inference_basis"] = res.get("basis")
            f["inference_model"] = MODEL
            merged += 1
    return merged


def enrich_thesis(firms: list[dict]) -> int:
    """Add a one-line ``inferred_thesis`` to already-tagged (inferred) lite
    firms, grounded on their established sectors/stages. Returns count merged."""
    enricher = GlmSectorEnricher()
    targets = [f for f in firms if f.get("inferred") and (f.get("sectors") or [])]
    done = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(enricher.thesis, f): f for f in targets}
        for fut in as_completed(futs):
            fut.result(); done += 1
            if done % 25 == 0:
                enricher._save()
    enricher._save()

    merged = 0
    for f in targets:
        res = enricher._thesis_cache.get(f["id"]) or {}
        thesis = (res.get("thesis") or "").strip()
        if thesis and (res.get("confidence") or 0) >= THESIS_THRESHOLD:
            f["inferred_thesis"] = thesis
            f["thesis_confidence"] = res.get("confidence")
            merged += 1
    return merged


def main() -> None:
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "sectors"
    payload = json.loads(DATA_OUT.read_text())
    if mode == "thesis":
        merged = enrich_thesis(payload["firms"])
        label = f"glm_thesis: merged {merged} one-line theses"
        tag = "glm_thesis"
    else:
        merged = enrich_glm_sectors(payload["firms"])
        label = f"glm_sectors: merged {merged} lite firms (+25 hand-curated filterable)"
        tag = "glm_sectors"
    enr = set(payload.get("generated_with_enrichers", [])); enr.add(tag)
    payload["generated_with_enrichers"] = sorted(enr)
    DATA_OUT.write_text(json.dumps(payload, indent=2))
    SITE_OUT.write_text(DATA_OUT.read_text())
    print(label)


if __name__ == "__main__":
    main()
