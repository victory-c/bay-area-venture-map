"""SEC Form D filings enrichment via EDGAR Full-Text Search (EFTS).

What this adds
--------------
Form D is the notice of an exempt securities offering — every time a VC
fund closes (or amends a close), it files one. So aggregating Form D
filings by IA brand name gives us a free, authoritative signal of:

  * how many distinct fund vehicles a firm has stood up,
  * when the most recent close happened,
  * the names + CIKs of those fund vehicles (handy for cross-referencing
    EDGAR later).

This is *purely additive*. Lite firms already carry SEC Form ADV signal
(``fund_count``, ``latest_filing_date``) sourced from the bulk CSV; the
Form D data complements that with the *fundraising* side of the same
firm's activity. Coverage is heavily skewed toward brand-recognisable
firms: Sequoia / Khosla / a16z all return dozens of filings; obscure
shell-entity names ("010118 Management, L.P.") return zero. That's fine.

Data flow
---------
For each firm:

  1. EFTS quoted-phrase search: ``q="{firm_name}"&forms=D``.
  2. Drop hits whose ``display_names[0]`` doesn't contain the firm's
     brand token (first 1-2 words) — catches cross-contamination from
     unrelated filers that happen to share a word.
  3. Aggregate: count, distinct fund entities, latest file_date, top-5
     most-recent filings.

We do NOT fetch each filing's ``primary_doc.xml`` to extract offering
amounts. That'd be ~5x more requests per firm and the metadata layer
already conveys the useful "is this firm actively raising?" signal. A
follow-up pass can drill into primary_doc later if dollars matter.

Rate limiting
-------------
SEC asks for a descriptive User-Agent + ≤10 req/s per host. We self-
throttle to ~6 req/s.

Cache
-----
``data/.form-d-cache.json``. Keyed by lowercase firm name. Stores the
parsed FormDInfo as a dict, or an explicit ``null`` for "no match" so
re-runs don't re-query empty firms. Bump CACHE_VERSION when parsing
logic changes.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "Sand Hill VC Map sandhillmap@example.com"
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
MIN_INTERVAL_SECONDS = 0.17  # ~6 req/s, under SEC's 10 req/s ceiling

CACHE_VERSION = 1
DEFAULT_CACHE = pathlib.Path("data/.form-d-cache.json")

# Cap recent-filings retained per firm — enough for a UI list, small
# enough to keep the cache compact.
MAX_RECENT = 5
MAX_DISTINCT_FUNDS = 10


@dataclass
class FilingMeta:
    accession: str
    file_date: str  # ISO YYYY-MM-DD
    form: str  # "D" or "D/A"
    cik: str
    filer_name: str


@dataclass
class FormDInfo:
    total_filings: int
    latest_filing_date: Optional[str]
    distinct_funds: list[str] = field(default_factory=list)
    fund_ciks: list[str] = field(default_factory=list)
    recent_filings: list[FilingMeta] = field(default_factory=list)


# Suffixes to strip when deriving a firm's "brand token" for the match
# validator. Mirrors build._DEDUP_SUFFIXES but on a smaller scale; we
# only need to recover the prefix that distinguishes the firm.
_SUFFIX_RE = re.compile(
    r"\s+(?:capital|partners|ventures|management|operations|holdings|"
    r"group|associates|advisers|advisors|fund|funds|llc|l\.l\.c\.|"
    r"lp|l\.p\.|inc|inc\.|ltd|corp|corporation)\b.*$",
    re.IGNORECASE,
)


def _brand_token(firm_name: str) -> str:
    """First 1-2 words of the firm name, with corporate suffixes stripped.

    Used as a substring check against EFTS display_names so we drop
    obvious cross-contamination (a quoted EFTS search for "X Capital"
    can still return unrelated filers whose name happens to include
    one of the tokens).
    """
    cleaned = _SUFFIX_RE.sub("", firm_name).strip(" ,.")
    cleaned = re.sub(r"\s*\([^)]*\)\s*", " ", cleaned)  # drop "(a16z)"
    words = cleaned.split()
    if not words:
        return firm_name.lower()
    # Use first 2 words to keep the match specific without being so
    # strict that "Founders Fund" (1 token after suffix strip) loses
    # legitimate hits. For single-word brands, just use that one word.
    return " ".join(words[:2]).lower()


def _parse_hits(firm_name: str, hits: list[dict], total: int) -> FormDInfo:
    brand = _brand_token(firm_name)
    kept: list[FilingMeta] = []
    for hit in hits:
        src = hit.get("_source", {})
        names = src.get("display_names") or []
        if not names:
            continue
        primary = names[0]
        if brand not in primary.lower():
            continue
        ciks = src.get("ciks") or [""]
        kept.append(
            FilingMeta(
                accession=src.get("adsh", ""),
                file_date=src.get("file_date", ""),
                form=src.get("form", ""),
                cik=str(ciks[0]).lstrip("0") or "0",
                filer_name=primary,
            )
        )

    if not kept:
        return FormDInfo(total_filings=0, latest_filing_date=None)

    kept.sort(key=lambda f: f.file_date, reverse=True)

    distinct_names: list[str] = []
    distinct_ciks: list[str] = []
    seen_names: set[str] = set()
    seen_ciks: set[str] = set()
    for f in kept:
        if f.filer_name not in seen_names:
            seen_names.add(f.filer_name); distinct_names.append(f.filer_name)
        if f.cik and f.cik not in seen_ciks:
            seen_ciks.add(f.cik); distinct_ciks.append(f.cik)

    return FormDInfo(
        total_filings=len(kept),
        latest_filing_date=kept[0].file_date,
        distinct_funds=distinct_names[:MAX_DISTINCT_FUNDS],
        fund_ciks=distinct_ciks[:MAX_DISTINCT_FUNDS],
        recent_filings=kept[:MAX_RECENT],
    )


class FormDClient:
    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        cache_path: Optional[pathlib.Path] = None,
    ) -> None:
        self._client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
        )
        self._last_request: float = 0.0
        self._cache_path = cache_path or DEFAULT_CACHE
        self._cache: dict[str, Optional[dict]] = {}
        self._load_cache()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_INTERVAL_SECONDS:
            time.sleep(MIN_INTERVAL_SECONDS - elapsed)
        self._last_request = time.monotonic()

    def _load_cache(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            raw = json.loads(self._cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            log.warning("Form D cache unreadable; starting fresh")
            return
        if raw.get("version") != CACHE_VERSION:
            log.info("Form D cache version mismatch; ignoring stale entries")
            return
        self._cache = raw.get("entries", {})

    def _save_cache(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(
            json.dumps(
                {"version": CACHE_VERSION, "entries": self._cache},
                indent=2,
                sort_keys=True,
            )
        )

    def lookup(self, firm_name: str) -> Optional[FormDInfo]:
        key = firm_name.lower().strip()
        if key in self._cache:
            cached = self._cache[key]
            if cached is None:
                return None
            return _info_from_dict(cached)

        params = {"q": f'"{firm_name}"', "forms": "D"}
        data = self._fetch_with_retry(params, firm_name)
        if data is None:
            # Transient failure after retries — don't cache, so a future
            # re-run picks it up. Caller treats as "no data this round".
            return None
        hits_block = data.get("hits", {})
        hits = hits_block.get("hits", [])
        total = hits_block.get("total", {}).get("value", 0)

        if not hits:
            self._cache[key] = None
            self._save_cache()
            return None

        info = _parse_hits(firm_name, hits, total)
        if info.total_filings == 0:
            self._cache[key] = None
        else:
            self._cache[key] = _info_to_dict(info)
        self._save_cache()
        return info if info.total_filings > 0 else None

    def _fetch_with_retry(self, params: dict, firm_name: str) -> Optional[dict]:
        """GET with retries on 5xx/timeouts. Returns None after final failure.

        EFTS occasionally returns 500 on perfectly-valid queries — once-
        per-thousand-ish in practice. Retrying twice with backoff clears
        nearly all of them; persistent failures are logged and skipped.
        """
        last_err: Optional[Exception] = None
        for attempt in range(3):
            self._throttle()
            try:
                resp = self._client.get(EFTS_URL, params=params)
                if resp.status_code >= 500 or resp.status_code == 429:
                    last_err = httpx.HTTPStatusError(
                        f"{resp.status_code} from EFTS",
                        request=resp.request, response=resp,
                    )
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
                time.sleep(2 ** attempt)
                continue
        log.warning("Form D: giving up on %r after retries (%s)", firm_name, last_err)
        return None

    def close(self) -> None:
        self._client.close()


def _info_to_dict(info: FormDInfo) -> dict:
    return dataclasses.asdict(info)


def _info_from_dict(d: dict) -> FormDInfo:
    filings = [FilingMeta(**f) for f in d.get("recent_filings", [])]
    return FormDInfo(
        total_filings=d.get("total_filings", 0),
        latest_filing_date=d.get("latest_filing_date"),
        distinct_funds=list(d.get("distinct_funds", [])),
        fund_ciks=list(d.get("fund_ciks", [])),
        recent_filings=filings,
    )
