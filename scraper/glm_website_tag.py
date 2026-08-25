"""Website-grounded sector/stage tagging for the long tail of lite firms.

Why a second tagging pass exists
--------------------------------
``glm_enrich`` already ran over all 650 lite firms, but it is *parametric*:
it asks GLM-4.6 what it knows about a firm from the name, its Form D fund
vehicle names and its ADV type tags. That prompt deliberately returns an
empty list when the model has no specific knowledge of the exact firm, and
for 444 firms it did exactly that. Those are the obscure ones — precisely
where parametric knowledge runs out.

So re-running the same pass cannot help: the answer would be empty again.
What those firms do have is a website. This module reads it.

  parametric pass  → recognizable firms  (206 tagged)
  THIS pass        → obscure firms with a site  (289 candidates)

Why that matters: 444 of 675 firms carry no sector or stage tag, and a
filtered search — the way the site tells founders to browse — can only
reach the 224 that carry both. In persona testing, 7 of 15 founder cells
shortlisted *every* firm the filter returned, so shortlist size was
tracking tag coverage rather than the map's real breadth.

Grounding and the evidence gate
-------------------------------
The prompt supplies the firm's own page text and forbids outside
knowledge. The model must return a verbatim ``evidence`` quote from that
text supporting the classification, and :func:`_evidence_supported`
checks the quote actually appears in the fetched text before anything is
merged. A confident answer whose quote is absent is a fabrication and is
dropped. This is the main reason to prefer this pass over parametric
guessing on the same firms.

Merged tags carry ``inferred: true`` (so the existing "AI-inferred" pill
and "Verified only" filter keep working) plus ``inference_source:
"website"``, which distinguishes a page-grounded tag from a parametric
one for any later UI or audit that wants to treat them differently.

Shared sites
------------
Several SEC entities file under one brand and share a website (Icon
Ventures, AI Fund, Allegis…). Page text is cached per host, so a shared
site is fetched once and every entity on it classifies off identical
text.

Auth
----
``ZHIPU_API_KEY`` from the environment (gitignored ``.env``; never commit).

Run
---
    ZHIPU_API_KEY=... python -m scraper.glm_website_tag --limit 20   # pilot
    ZHIPU_API_KEY=... python -m scraper.glm_website_tag              # full

Writes ``data/firms.json`` and the ``site/`` copy in place. Never rebuilds
from seed — a bare ``scraper.build`` would drop the SEC lite firms.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import httpx

from scraper.llm_enrich import SECTORS, STAGES
from scraper.useragent import build_user_agent
from scraper.website_enrich import (
    PER_HOST_INTERVAL,
    PER_PAGE_TEXT_CAP,
    PAGE_TIMEOUT,
    _html_to_text,
    _is_scrapable_url,
)

log = logging.getLogger(__name__)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_OUT = REPO_ROOT / "data" / "firms.json"
SITE_OUT = REPO_ROOT / "site" / "firms.json"
CACHE = REPO_ROOT / "data" / ".glm-website-tag-cache-v2.json"

BASE = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = "glm-4.6"
THRESHOLD = 0.7      # min self-confidence to merge
MAX_SECTORS = 3
CONCURRENCY = 2      # Zhipu rate cap trips above this
BACKOFF = [8, 25, 60, 90]

# Investment-thesis content lives on more paths than partner bios do, so
# this list leans toward "what do you invest in" pages.
CANDIDATE_PATHS = ("/", "/about", "/portfolio", "/investments", "/thesis", "/focus")
MAX_PAGES = 4
MIN_TEXT_CHARS = 240   # below this a page is a splash screen, not content

PROMPT = """You are classifying a venture capital firm by the SECTORS and STAGES it invests in, using ONLY the text from its own website supplied below.

Firm name: {name}
Pages read: {urls}

--- BEGIN WEBSITE TEXT ---
{text}
--- END WEBSITE TEXT ---

Rules:
- Use ONLY the text above. Do NOT use outside knowledge about this firm or any similarly-named firm.
- Each sector you list MUST carry its OWN verbatim quote from the text above that names or describes THAT sector. One quote cannot justify three sectors. If the text supports only one sector, list only that one.
- A quote about stage, team, geography, or values does NOT support a sector. Do not infer adjacent sectors: a firm quoted as investing in "AI" is ai_infra, not also fintech.
- At most {max_sectors} sectors. Use "generalist" only where the text explicitly says the firm invests broadly with no specialisation.
- stages: only those the text names or clearly implies, with their own quote. If the text never indicates stage, return an empty stages list.
- If the text is a splash page, a login wall, a cookie notice, or otherwise says nothing about what the firm invests in, return empty lists with confidence 0.
- Every quote MUST be copied EXACTLY from the website text above (10-200 chars).
- sectors and stages MUST come ONLY from these allowed lists:
  SECTORS = {sectors}
  STAGES = {stages}

Return ONLY JSON (no fences):
{{"sectors":[{{"sector":"<one allowed sector>","quote":"<verbatim quote naming THAT sector>"}}],"stages":[...],"stage_evidence":"<verbatim quote about stage, or empty>","confidence":0.0-1.0}}"""


def _parse(content: str) -> dict | None:
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _norm(s: str) -> str:
    """Collapse whitespace and case so quote matching survives reflowing."""
    return re.sub(r"\s+", " ", s or "").strip().lower()


# Phrases that actually assert a broad, sector-agnostic mandate.
_BREADTH = (
    "generalist", "sector agnostic", "sector-agnostic", "any sector",
    "all sectors", "across sectors", "industry agnostic", "industry-agnostic",
    "broad range of", "wide range of", "regardless of sector", "sector focus",
)


def _claims_breadth(evidence: str) -> bool:
    """True when the quote really claims a generalist mandate.

    'across sectors' inside 'lived context across sectors' still trips this,
    so it is a floor rather than a guarantee — but it removes the tags backed
    by quotes that never mention breadth at all.
    """
    ev = _norm(evidence)
    return any(p in ev for p in _BREADTH)


def _evidence_supported(evidence: str, text: str) -> bool:
    """True when the model's quote really occurs in the page text.

    The model is told to copy verbatim; in practice it sometimes trims or
    re-spaces. Normalising whitespace/case tolerates that while still
    catching invented quotes, which is the failure mode that matters.
    """
    ev = _norm(evidence)
    if len(ev) < 10:
        return False
    return ev in _norm(text)


class WebsiteTagger:
    def __init__(self, cache_path: pathlib.Path = CACHE):
        key = os.environ.get("ZHIPU_API_KEY")
        if not key:
            raise SystemExit("ZHIPU_API_KEY not set (put it in a gitignored .env)")
        self._key = key
        self._cache_path = cache_path
        self._cache = self._load(cache_path)
        self._lock = threading.Lock()
        self._host_last: dict[str, float] = {}
        self._host_text: dict[str, tuple[list[str], str]] = {}
        self._page_client = httpx.Client(
            follow_redirects=True,
            timeout=PAGE_TIMEOUT,
            headers={"User-Agent": build_user_agent()},
        )

    @staticmethod
    def _load(path: pathlib.Path) -> dict:
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self) -> None:
        with self._lock:
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache))
            tmp.replace(self._cache_path)

    # ----- fetching ------------------------------------------------------

    def _throttle(self, host: str) -> None:
        with self._lock:
            last = self._host_last.get(host, 0.0)
            wait = PER_HOST_INTERVAL - (time.monotonic() - last)
            self._host_last[host] = time.monotonic() + max(0.0, wait)
        if wait > 0:
            time.sleep(wait)

    def fetch_site(self, website: str) -> tuple[list[str], str]:
        """Return (urls_read, combined_text). Cached per host."""
        website = website.strip()
        host = (urlparse(website).hostname or "").lower()
        with self._lock:
            if host in self._host_text:
                return self._host_text[host]

        urls: list[str] = []
        chunks: list[str] = []
        for path in CANDIDATE_PATHS:
            if len(chunks) >= MAX_PAGES:
                break
            url = urljoin(website, path)
            resp = None
            # One retry: a chunk of the pilot's "no usable text" results were
            # transient connect errors that succeed immediately on a second try.
            for attempt in range(2):
                self._throttle(host)
                try:
                    resp = self._page_client.get(url)
                    break
                except (httpx.TimeoutException, httpx.TransportError,
                        httpx.InvalidURL) as e:
                    if attempt == 0:
                        time.sleep(1.5)
                        continue
                    log.info("  fetch fail %s: %s", url, type(e).__name__)
                except Exception as e:  # noqa: BLE001 — one bad URL must not kill the run
                    log.info("  fetch error %s: %s", url, type(e).__name__)
                    break
            if resp is None:
                continue
            if resp.status_code >= 400:
                continue
            if "text/html" not in resp.headers.get("content-type", "").lower():
                continue
            text = _html_to_text(resp.text)
            if len(text) < MIN_TEXT_CHARS:
                continue
            urls.append(str(resp.url))
            chunks.append(text[:PER_PAGE_TEXT_CAP])

        result = (urls, "\n\n".join(chunks))
        with self._lock:
            self._host_text[host] = result
        return result

    # ----- classification -------------------------------------------------

    def _chat(self, content: str) -> dict:
        body = {"model": MODEL, "temperature": 0.0, "thinking": {"type": "disabled"},
                "messages": [{"role": "user", "content": content}]}
        last = "unknown"
        for attempt in range(5):
            try:
                r = httpx.post(
                    BASE,
                    headers={"Authorization": f"Bearer {self._key}",
                             "Content-Type": "application/json"},
                    json=body,
                    timeout=httpx.Timeout(90.0, connect=10.0),
                )
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"HTTP {r.status_code}"
                    time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                    continue
                if r.status_code != 200:
                    return {"error": f"HTTP {r.status_code}"}
                parsed = _parse(r.json()["choices"][0]["message"]["content"])
                # An unparseable body is a failure, not "no sectors" — without
                # the error key it would cache as a good empty and never retry.
                return parsed if parsed is not None else {"error": "unparseable_response"}
            except Exception as e:  # noqa: BLE001 — retry any transport error
                last = type(e).__name__
                time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
        return {"error": f"retries_exhausted:{last}"}

    def classify(self, firm: dict) -> dict:
        """Fetch + classify one firm. Result is cached by firm id."""
        fid = firm["id"]
        with self._lock:
            hit = self._cache.get(fid)
        if hit is not None and "error" not in hit:
            return hit

        urls, text = self.fetch_site(firm["website"])
        if not text:
            res = {"error": "no_usable_page_text"}
        else:
            res = self._chat(PROMPT.format(
                name=firm["name"],
                urls=", ".join(urls) or "(none)",
                text=text[:24000],
                max_sectors=MAX_SECTORS,
                sectors=list(SECTORS),
                stages=list(STAGES),
            ))
            if "error" not in res:
                res["urls"] = urls
                # Validate EVERY sector against its OWN quote. The v1 gate
                # accepted one quote for a whole list, so a firm quoted only
                # on "AI" still picked up fintech and healthcare; roughly half
                # the secondary tags were unsupported. Each sector now has to
                # carry its own verifiable evidence or it is dropped.
                verified = []
                for item in (res.get("sectors") or []):
                    if not isinstance(item, dict):
                        continue          # old/!malformed shape — no evidence to check
                    sector = item.get("sector")
                    quote = item.get("quote", "")
                    if sector in SECTORS and _evidence_supported(quote, text):
                        verified.append({"sector": sector, "quote": quote})
                # One quote cannot justify several sectors. When the model
                # pastes the same sentence under two or three of them, only
                # the first keeps it — the rest have no distinct evidence.
                seen_quotes: set[str] = set()
                deduped = []
                for v in verified:
                    key = _norm(v["quote"])
                    if key in seen_quotes:
                        continue
                    seen_quotes.add(key)
                    deduped.append(v)
                res["verified_sectors"] = deduped
                res["shared_quote_dropped"] = len(verified) - len(deduped)
                res["claimed_sector_count"] = len(res.get("sectors") or [])
                res["stage_evidence_ok"] = _evidence_supported(
                    res.get("stage_evidence", ""), text)
        with self._lock:
            self._cache[fid] = res
        return res

    def close(self) -> None:
        self._page_client.close()


# Social hosts website_enrich does not know about. A firm whose "website"
# is one of these has a junk value in the SEC data, not a site to read.
EXTRA_SOCIAL = ("threads.net", "threads.com", "t.me", "discord.gg", "notion.site")


def _usable_site(url: str) -> bool:
    if not _is_scrapable_url(url):
        return False
    host = (urlparse(url).hostname or "").lower()
    return not any(host == b or host.endswith("." + b) for b in EXTRA_SOCIAL)


def candidates(firms: list[dict]) -> list[dict]:
    """Lite firms with no sectors and a website worth fetching."""
    return [
        f for f in firms
        if f.get("tier") == "lite"
        and not f.get("sectors")
        and f.get("website")
        and _usable_site(f["website"].strip().lower())
    ]


def merge(firms: list[dict], tagger: WebsiteTagger) -> dict[str, int]:
    """Apply cached results. Returns a breakdown of what happened."""
    stats = {"attempted": 0, "merged": 0, "no_sectors": 0, "low_confidence": 0,
             "errors": 0, "stages_only_dropped": 0, "generalist_dropped": 0,
             "unsupported_sectors_dropped": 0, "shared_quote_dropped": 0}
    for f in candidates(firms):
        res = tagger._cache.get(f["id"])
        if res is None:
            continue          # never attempted — not a result, don't count it
        stats["attempted"] += 1
        if "error" in res:
            stats["errors"] += 1
            continue
        verified = res.get("verified_sectors") or []
        secs = [v["sector"] for v in verified]
        dropped = (res.get("claimed_sector_count") or 0) - len(secs)
        if dropped > 0:
            stats["unsupported_sectors_dropped"] += dropped
        stats["shared_quote_dropped"] += res.get("shared_quote_dropped") or 0
        # "generalist" is where the pilot's weak tags concentrated — it got
        # attached to vague quotes ("lived context across sectors") that say
        # nothing about what the firm actually backs. It is also the least
        # useful tag to a founder filtering by their own sector, so require
        # the quote to actually claim breadth before keeping it.
        # Check generalist against ITS OWN quote, and drop it from `verified`
        # too so the recorded basis never cites a sector that was discarded.
        if any(v["sector"] == "generalist" and not _claims_breadth(v["quote"])
               for v in verified):
            verified = [v for v in verified if v["sector"] != "generalist"]
            secs = [v["sector"] for v in verified]
            stats["generalist_dropped"] += 1
        if not secs:
            stats["no_sectors"] += 1
            continue
        if (res.get("confidence") or 0) < THRESHOLD:
            stats["low_confidence"] += 1
            continue

        # Never overwrite tags another pass already established.
        if f.get("sectors"):
            continue
        f["sectors"] = secs[:MAX_SECTORS]
        # Stages ride along only with their own supporting quote.
        stgs = [s for s in (res.get("stages") or []) if s in STAGES]
        if stgs and res.get("stage_evidence_ok"):
            f["stages"] = stgs
        elif stgs:
            stats["stages_only_dropped"] += 1
        f["inferred"] = True
        f["inference_confidence"] = res.get("confidence")
        # Basis records the quote behind each kept sector, so the claim on the
        # page can always be traced to the sentence that justified it.
        f["inference_basis"] = " | ".join(
            f'{v["sector"]}: "{v["quote"][:110]}"' for v in verified)[:400]
        f["inference_evidence"] = verified
        f["inference_model"] = MODEL
        f["inference_source"] = "website"
        stats["merged"] += 1
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, help="classify only the first N candidates (pilot)")
    ap.add_argument("--dry-run", action="store_true", help="classify but do not write firms.json")
    args = ap.parse_args()

    payload = json.loads(DATA_OUT.read_text())
    firms = payload["firms"]
    pool = candidates(firms)
    if args.limit:
        pool = pool[: args.limit]
    log.info("candidates: %d lite firms with no sectors and a scrapable site", len(pool))

    tagger = WebsiteTagger()
    try:
        done = 0
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            futs = {ex.submit(tagger.classify, f): f for f in pool}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:  # noqa: BLE001 — one firm must not kill the run
                    log.warning("  %s failed: %s", futs[fut]["name"], e)
                done += 1
                if done % 25 == 0:
                    tagger.save()
                    log.info("  %d/%d classified", done, len(pool))
        tagger.save()
    finally:
        tagger.close()

    stats = merge(firms, tagger)
    log.info("\nresults: %s", json.dumps(stats, indent=2))

    if args.dry_run:
        log.info("dry run — firms.json not written")
        return

    tagged = sum(1 for f in firms if f.get("sectors"))
    enr = set(payload.get("generated_with_enrichers", []))
    enr.add("glm_website_tags")
    payload["generated_with_enrichers"] = sorted(enr)
    DATA_OUT.write_text(json.dumps(payload, indent=2))
    SITE_OUT.write_text(DATA_OUT.read_text())
    log.info("wrote firms.json — %d/%d firms now carry sector tags", tagged, len(firms))


if __name__ == "__main__":
    main()
