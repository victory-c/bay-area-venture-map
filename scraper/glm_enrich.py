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

BASE = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = "glm-4.6"
THRESHOLD = 0.7          # min self-confidence to merge a tag into firms.json
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


def _parse(content: str):
    m = re.search(r"\{.*\}", content, re.DOTALL)
    return json.loads(m.group(0)) if m else None


class GlmSectorEnricher:
    def __init__(self, cache_path: pathlib.Path = CACHE):
        key = os.environ.get("ZHIPU_API_KEY")
        if not key:
            raise SystemExit("ZHIPU_API_KEY not set (put it in a gitignored .env)")
        self._key = key
        self._cache_path = cache_path
        self._lock = threading.Lock()
        self._cache = {}
        if cache_path.exists():
            try:
                self._cache = json.loads(cache_path.read_text())
            except (OSError, json.JSONDecodeError):
                self._cache = {}

    def _save(self):
        with self._lock:
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache))
            tmp.replace(self._cache_path)

    def _call(self, firm: dict) -> dict:
        body = {
            "model": MODEL, "temperature": 0.1, "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": PROMPT.format(
                name=firm["name"], addr=firm.get("address", ""),
                funds=(firm.get("form_d_distinct_funds") or [])[:6],
                types=firm.get("firm_type_tags") or [],
                sectors=list(SECTORS), stages=list(STAGES), threshold=THRESHOLD)}],
        }
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
                p = _parse(r.json()["choices"][0]["message"]["content"]) or {}
                secs = [s for s in (p.get("sectors") or []) if s in SECTORS][:MAX_SECTORS]
                stgs = [s for s in (p.get("stages") or []) if s in STAGES]
                return {"sectors": secs, "stages": stgs, "confidence": p.get("confidence"),
                        "basis": p.get("basis")}
            except Exception as e:  # noqa: BLE001 — retry any transport error
                last = type(e).__name__
                time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
        return {"error": f"retries_exhausted:{last}"}

    def lookup(self, firm: dict) -> dict:
        fid = firm["id"]
        cached = self._cache.get(fid)
        if cached is not None and "error" not in cached:
            return cached
        res = self._call(firm)
        with self._lock:
            self._cache[fid] = res
        return res


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


def main() -> None:
    payload = json.loads(DATA_OUT.read_text())
    merged = enrich_glm_sectors(payload["firms"])
    enr = set(payload.get("generated_with_enrichers", [])); enr.add("glm_sectors")
    payload["generated_with_enrichers"] = sorted(enr)
    DATA_OUT.write_text(json.dumps(payload, indent=2))
    SITE_OUT.write_text(DATA_OUT.read_text())
    print(f"glm_sectors: merged {merged} lite firms (+25 hand-curated filterable)")


if __name__ == "__main__":
    main()
