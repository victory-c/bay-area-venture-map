"""Wikipedia enrichment: pull founded year, founders, key people, and AUM
from English Wikipedia infoboxes for VC firms that have an article.

Data flow
---------
For each firm:

  1. ``opensearch`` query by firm name -> top 5 candidate page titles.
  2. Score each candidate by VC-firm signal (page summary contains
     "venture capital" / "private equity" / "investment firm"; category
     suggests venture or investment firm; title disambiguator).
  3. Pull raw wikitext for the best candidate.
  4. Regex out the first ``{{Infobox ...}}`` block and parse ``|key = value``
     pairs. Clean wiki markup ([[link|alias]] -> alias, <ref>...</ref> -> '',
     {{cite ...}} -> '', '&nbsp;' -> ' ', etc.).
  5. Map a curated set of infobox keys to our schema fields.

Coverage expectation: ~50-150 of the 678 firms have a Wikipedia article.
The rest return None and remain untouched. Wikipedia is purely additive —
existing seed values always win.

Throttling: Wikipedia's API has no documented rate limit for read-only
opensearch + parse calls under the standard User-Agent policy, but we
self-throttle to ~5 req/s to be polite.

Cache: ``data/.wikipedia-cache.json``. Keyed by firm name. Stores either
the parsed WikiInfo or an explicit ``null`` for "no match found" so
re-runs don't re-query firms with no article. Bump CACHE_VERSION when
parsing logic changes.
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

from scraper.useragent import USER_AGENT as _UA

log = logging.getLogger(__name__)

# Wikipedia asks for a descriptive User-Agent with contact info.
# https://meta.wikimedia.org/wiki/User-Agent_policy
USER_AGENT = _UA
API_URL = "https://en.wikipedia.org/w/api.php"
SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/"
MIN_INTERVAL_SECONDS = 0.2  # 5 req/s

CACHE_VERSION = 1
DEFAULT_CACHE = pathlib.Path("data/.wikipedia-cache.json")


@dataclass
class WikiInfo:
    title: str
    url: str
    founded: Optional[int] = None
    founders: list[str] = field(default_factory=list)
    key_people: list[str] = field(default_factory=list)
    headquarters: Optional[str] = None
    aum_usd: Optional[int] = None
    industry: Optional[str] = None


# ---------------------------------------------------------------------------
# Wiki-markup cleanup
# ---------------------------------------------------------------------------
_REF_RE = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^/]*/>", re.DOTALL | re.IGNORECASE)
_SELFCLOSE_REF_RE = re.compile(r"<ref[^>]*/>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_TEMPLATE_RE = re.compile(r"\{\{[^{}]+\}\}")
_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_FILE_LINK_RE = re.compile(r"\[\[(?:File|Image):[^\]]+\]\]", re.IGNORECASE)
_NBSP_RE = re.compile(r"&nbsp;|&amp;nbsp;|&#160;")
_AMP_RE = re.compile(r"&amp;")
_WHITESPACE_RE = re.compile(r"\s+")

# Templates whose contents are the actual data we want, not formatting noise.
# We rewrite them to plain text BEFORE the generic template-stripping pass
# in _strip_markup.
#
# `{{US$|56.3 billion}}` -> `$56.3 billion`
# `{{USD|56.3|billion}}` -> `$56.3 billion`
# `{{hlist|A|B|C}}` / `{{plainlist|A|B|C}}` / `{{flatlist|A|B|C}}` -> `A, B, C`
# `{{ubl|A|B}}` / `{{unbulleted list|A|B}}` -> `A, B`
# `{{nowrap|x}}` -> `x`
# `{{small|x}}` -> `x`
# Note: no \b after the name alternation — "US$" ends in a non-word char so
# \b would never match it. The leading \s* and the immediately-following
# pipe-or-close in the body are enough.
_VALUE_TEMPLATES = re.compile(
    r"\{\{\s*(US\$|USD|hlist|plainlist|flatlist|ubl|unbulleted list|nowrap|small)([^{}]*)\}\}",
    re.IGNORECASE,
)


def _split_template_body(body: str) -> list[str]:
    """Split a template body on top-level `|`, respecting `[[...]]` link depth.

    Without depth tracking, `[[Thomas Perkins (businessman)|Thomas Perkins]]`
    splits into two pieces and corrupts both.
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in body:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == "|" and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _expand_value_templates(s: str) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1).lower()
        parts = [p.strip() for p in _split_template_body(m.group(2))
                 if p.strip() and "=" not in p]
        if not parts:
            return ""
        if name in ("us$", "usd"):
            return "$" + " ".join(parts)
        if name in ("hlist", "plainlist", "flatlist", "ubl", "unbulleted list"):
            return ", ".join(parts)
        # nowrap / small / etc. — strip the wrapper, keep the content.
        return " ".join(parts)
    for _ in range(3):
        new = _VALUE_TEMPLATES.sub(repl, s)
        if new == s:
            break
        s = new
    return s


def _strip_markup(value: str) -> str:
    """Convert wikitext fragment to plain text. Idempotent."""
    if not value:
        return ""
    s = value
    # Drop file/image embeds entirely.
    s = _FILE_LINK_RE.sub("", s)
    # Strip <ref>...</ref> and self-closing refs.
    s = _REF_RE.sub("", s)
    s = _SELFCLOSE_REF_RE.sub("", s)
    # Expand value-carrying templates (US$, hlist, etc.) BEFORE the generic
    # template-strip pass, otherwise we lose the actual data inside them.
    s = _expand_value_templates(s)
    # Strip remaining (formatting/citation) templates iteratively.
    for _ in range(4):
        new = _TEMPLATE_RE.sub("", s)
        if new == s:
            break
        s = new
    # [[Real Title|Display]] -> Display; [[Plain]] -> Plain.
    s = _LINK_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), s)
    # Remove remaining HTML tags.
    s = _HTML_TAG_RE.sub(" ", s)
    s = _NBSP_RE.sub(" ", s)
    s = _AMP_RE.sub("&", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    # Trailing commas / stray punctuation are common after refs are stripped.
    return s.strip(" ,;:")


def _parse_infobox(wikitext: str) -> Optional[dict[str, str]]:
    """Extract the first ``{{Infobox ...}}`` block and return its key/value pairs.

    Returns None if no infobox is found. Splits on top-level pipes only
    (pipes inside nested ``[[...]]`` or ``{{...}}`` are preserved).
    """
    # Find "{{Infobox" case-insensitively at any indentation.
    m = re.search(r"\{\{\s*[Ii]nfobox", wikitext)
    if not m:
        return None
    # Walk forward tracking brace depth to find matching close.
    start = m.start()
    i = m.end()
    depth = 2  # we've consumed "{{"
    while i < len(wikitext) and depth > 0:
        if wikitext[i:i + 2] == "{{":
            depth += 2; i += 2
        elif wikitext[i:i + 2] == "}}":
            depth -= 2; i += 2
        else:
            i += 1
    if depth != 0:
        return None
    body = wikitext[start + 2 : i - 2]
    # Body looks like: "Infobox company\n| name = Foo\n| founded = 1972\n..."
    # Split into segments by top-level '|'.
    segments: list[str] = []
    buf: list[str] = []
    depth_b = 0
    depth_p = 0
    for ch in body:
        if ch == "[":
            depth_b += 1
        elif ch == "]":
            depth_b = max(0, depth_b - 1)
        elif ch == "{":
            depth_p += 1
        elif ch == "}":
            depth_p = max(0, depth_p - 1)
        if ch == "|" and depth_b == 0 and depth_p == 0:
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    segments.append("".join(buf))

    fields: dict[str, str] = {}
    # First segment is the template name ("Infobox company"); skip.
    for seg in segments[1:]:
        if "=" not in seg:
            continue
        key, _, value = seg.partition("=")
        fields[key.strip().lower()] = value.strip()
    return fields


def _parse_year(raw: str) -> Optional[int]:
    if not raw:
        return None
    # Search the raw wikitext first so we still find the year inside
    # templates like {{Start date|1965}} or {{Founded in|1972}}, which the
    # generic strip pass would drop entirely.
    m = re.search(r"\b(1[89]\d{2}|20\d{2})\b", raw)
    if m:
        return int(m.group(1))
    return None


# Words that mark a list entry as a role/title rather than a person name.
# Used to filter out the "role" entries that some infoboxes interleave with
# names inside `{{ubl|Name|Role|Name|Role|...}}`.
_ROLE_WORD_RE = re.compile(
    r"\b(CEO|CFO|CIO|COO|CTO|CMO|CCO|Chair(?:man|woman|person)?|President|Vice\s+President|"
    r"VP|Exec(?:utive)?|Managing|Senior|Junior|General|Director|Partner|Founder|"
    r"Officer|Treasurer|Secretary|Co-CEO|Co-Chair|Co-Founder)\b",
    re.IGNORECASE,
)


def _parse_people_list(raw: str) -> list[str]:
    """Infobox people fields use <br>, *, hlist templates, or commas as separators."""
    if not raw:
        return []
    s = raw
    # Strip <ref>...</ref> first — they often contain commas / braces that
    # would split into junk fragments later.
    s = _REF_RE.sub("", s)
    s = _SELFCLOSE_REF_RE.sub("", s)
    # Expand list templates so {{hlist|A|B|C}} -> "A, B, C" before splitting.
    s = _expand_value_templates(s)
    s = re.sub(r"<br\s*/?>|<br>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"\n\s*\*\s*", "\n", s)  # bullet list
    parts = [p for p in re.split(r"[\n,]", s) if p.strip()]
    out: list[str] = []
    for p in parts:
        cleaned = _strip_markup(p)
        # Drop role suffixes that look like job titles ("John Doe (CEO)" -> "John Doe").
        cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", cleaned).strip()
        if not cleaned or len(cleaned) < 3:
            continue
        if cleaned.lower().startswith(("see ", "list of")):
            continue
        # If the entry IS a role word with no other content, it's a title
        # interleaved with names (common in {{ubl|Name|Role|...}}). Drop it.
        if _ROLE_WORD_RE.fullmatch(cleaned) or (
            _ROLE_WORD_RE.search(cleaned) and len(cleaned.split()) <= 3 and
            not any(ch.isupper() for ch in cleaned[1:])  # heuristic: real names have an inner capital
        ):
            # Heuristic: pure role like "CEO" or "Exec. chairman". Skip.
            # But preserve "Joe Smith CEO" by checking there's an actual name.
            words = cleaned.split()
            if len(words) <= 2 and all(_ROLE_WORD_RE.search(w) or w.endswith(".") for w in words):
                continue
        out.append(cleaned)
    # Dedup preserving order.
    seen = set()
    deduped: list[str] = []
    for p in out:
        if p.lower() not in seen:
            seen.add(p.lower()); deduped.append(p)
    return deduped[:10]  # cap; partners list is for a side panel


_AUM_NUM_RE = re.compile(r"(?:US\$|\$|USD)?\s*([\d,.]+)\s*(billion|bn|million|mn|m|b)?", re.IGNORECASE)


def _parse_aum_string(raw: str) -> Optional[int]:
    if not raw:
        return None
    s = _strip_markup(raw)
    # Drop any trailing "(2024)"-style year markers — they confuse the regex
    # by looking like a bare number when the actual $-amount is elsewhere.
    s = re.sub(r"\(\s*(?:19|20)\d{2}\s*\)", "", s).strip()
    # Require either a $ marker or an explicit unit word to consider the
    # match an AUM figure. This avoids "Founded 2024" or "Series A 1972"
    # being read as $2024B / $1972B.
    m = _AUM_NUM_RE.search(s)
    if not m:
        return None
    num_str = m.group(1)
    suffix = (m.group(2) or "").lower()
    has_dollar = bool(re.search(r"(?:US\$|\$|USD)", s, re.IGNORECASE))
    if not (suffix or has_dollar):
        return None
    try:
        n = float(num_str.replace(",", ""))
    except ValueError:
        return None
    if suffix.startswith(("b", "bn")):
        n *= 1_000_000_000
    elif suffix.startswith(("m", "mn")):
        n *= 1_000_000
    elif n < 1000:
        # $-marked small number with no suffix — assume billions in VC context.
        n *= 1_000_000_000
    return round(n)


def info_from_infobox(title: str, infobox: dict[str, str]) -> WikiInfo:
    """Map raw infobox key/value pairs to a WikiInfo dataclass."""
    url = "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
    return WikiInfo(
        title=title,
        url=url,
        founded=_parse_year(infobox.get("founded") or infobox.get("foundation", "")),
        founders=_parse_people_list(infobox.get("founders") or infobox.get("founder", "")),
        key_people=_parse_people_list(infobox.get("key_people", "")),
        headquarters=_strip_markup(infobox.get("hq_location") or infobox.get("headquarters", "")) or None,
        aum_usd=_parse_aum_string(infobox.get("aum") or infobox.get("assets", "")),
        industry=_strip_markup(infobox.get("industry", "")) or None,
    )


# ---------------------------------------------------------------------------
# Candidate scoring — pick the right Wikipedia page from search results
# ---------------------------------------------------------------------------
_VC_SIGNAL_KEYWORDS = (
    "venture capital", "venture-capital", "vc firm",
    "private equity", "investment firm", "investment management",
    "asset management", "growth equity",
)


def _candidate_score(title: str, extract: str, firm_name: str) -> int:
    """Score how likely this page is *the* article for `firm_name`."""
    title_lc = title.lower()
    extract_lc = (extract or "").lower()
    firm_lc = firm_name.lower()
    score = 0
    # VC-domain signal in extract.
    for kw in _VC_SIGNAL_KEYWORDS:
        if kw in extract_lc:
            score += 5
            break
    # Disambiguator suffixes commonly used for firms.
    if any(d in title_lc for d in ("(company)", "(firm)", "(venture capital)", "(investment firm)")):
        score += 3
    # Title contains firm's full name.
    if firm_lc in title_lc or title_lc in firm_lc:
        score += 2
    # Heavy penalty for obvious disambig pages or person pages.
    if "(disambiguation)" in title_lc:
        score -= 10
    if extract_lc.startswith(("he ", "she ", "they ", "this article is about a person")):
        score -= 5
    return score


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class WikipediaClient:
    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        cache_path: Optional[pathlib.Path] = None,
    ) -> None:
        self._client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=20.0,
        )
        self._last_request: float = 0.0
        self._cache_path = cache_path or DEFAULT_CACHE
        self._cache: dict[str, Optional[dict]] = self._load_cache()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_INTERVAL_SECONDS:
            time.sleep(MIN_INTERVAL_SECONDS - elapsed)
        self._last_request = time.monotonic()

    def _load_cache(self) -> dict[str, Optional[dict]]:
        if not self._cache_path.exists():
            return {}
        try:
            blob = json.loads(self._cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(blob, dict) or blob.get("version") != CACHE_VERSION:
            return {}
        entries = blob.get("entries", {})
        return entries if isinstance(entries, dict) else {}

    def _save_cache(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(
            json.dumps({"version": CACHE_VERSION, "entries": self._cache}, indent=2)
        )

    def search(self, query: str, limit: int = 5) -> list[str]:
        """Return up to `limit` page titles ranked by OpenSearch relevance."""
        self._throttle()
        params = {
            "action": "opensearch", "search": query, "limit": str(limit),
            "namespace": "0", "format": "json",
        }
        resp = self._client.get(API_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
        # opensearch returns [query, [titles], [descriptions], [urls]]
        return list(data[1]) if isinstance(data, list) and len(data) >= 2 else []

    def get_summary(self, title: str) -> Optional[dict]:
        """REST summary endpoint — used for fast candidate scoring."""
        self._throttle()
        try:
            resp = self._client.get(SUMMARY_URL + title.replace(" ", "_"))
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        return resp.json()

    def get_wikitext(self, title: str) -> Optional[str]:
        self._throttle()
        params = {
            "action": "parse", "page": title, "prop": "wikitext",
            "format": "json", "formatversion": "2",
        }
        try:
            resp = self._client.get(API_URL, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.debug("wikitext fetch failed for %s: %s", title, e)
            return None
        data = resp.json()
        return data.get("parse", {}).get("wikitext") if "parse" in data else None

    def lookup(self, firm_name: str) -> Optional[WikiInfo]:
        """Find the best matching Wikipedia article for a VC firm and parse it.

        Cached. Returns None for firms with no good match.
        """
        cache_key = firm_name.strip().lower()
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            return WikiInfo(**cached) if cached else None

        info = self._lookup_uncached(firm_name)
        self._cache[cache_key] = dataclasses.asdict(info) if info else None
        self._save_cache()
        return info

    def _lookup_uncached(self, firm_name: str) -> Optional[WikiInfo]:
        titles = self.search(firm_name)
        if not titles:
            return None
        # Score candidates by summary signal. Skip summaries that 404 (often
        # red-links from disambig pages).
        best: Optional[tuple[int, str]] = None
        for title in titles[:5]:
            summary = self.get_summary(title)
            if not summary:
                continue
            extract = summary.get("extract", "")
            score = _candidate_score(title, extract, firm_name)
            if best is None or score > best[0]:
                best = (score, title)
        if best is None or best[0] <= 0:
            return None
        title = best[1]
        wikitext = self.get_wikitext(title)
        if not wikitext:
            return None
        infobox = _parse_infobox(wikitext)
        if not infobox:
            # Page exists but has no infobox — record as no-match so we don't retry.
            return None
        return info_from_infobox(title, infobox)

    def close(self) -> None:
        self._client.close()
