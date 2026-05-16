"""NVCA member directory enrichment.

Data source
-----------
The National Venture Capital Association publishes a public member directory
at https://nvca.org/nvca-members/ . The page is a WordPress template that
renders a server-side HTML table (class ``member_table``) with one row per
member firm. Each row exposes:

    <td class="member_name"><a href="<firm-website>">Firm Name</a></td>
    <td class="member_city">San Francisco</td>
    <td class="member_state">CA</td>

The directory used to be at ``/member-directory/`` (now 404). There is no
public JSON endpoint — wpDataTables would expose one to logged-in admins,
but the unauthenticated page emits the whole list inline, so a single HTML
fetch covers every member. As of 2026 the page returns ~430 firms.

Sector / industry focus is NOT exposed on this page. We extract
``{name, website, hq_city, hq_state}`` and leave ``sector_focus`` as None
so the caller can wire LLM-based sector inference later if desired.

Cache
-----
``data/.nvca-cache.json`` with a version marker and a fetched-at timestamp.
TTL: 30 days (NVCA membership doesn't change often). Bump CACHE_VERSION
when parser logic changes so old caches are ignored automatically.

Throttling
----------
A single HTTP GET per refresh, but we self-throttle to ≤5 req/s anyway to
mirror the Wikipedia client. The User-Agent identifies the tool and a
contact email per scraping etiquette.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger(__name__)

# Same identification used by sec_bulk — descriptive UA with contact email.
USER_AGENT = "Sand Hill VC Map (sandhillmap@example.com) - Bay Area VC research tool"
DIRECTORY_URL = "https://nvca.org/nvca-members/"
MIN_INTERVAL_SECONDS = 0.2  # 5 req/s ceiling, polite

CACHE_VERSION = 1
DEFAULT_CACHE = pathlib.Path("data/.nvca-cache.json")
DEFAULT_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days


@dataclass
class NvcaMember:
    name: str
    website: Optional[str] = None
    hq_city: Optional[str] = None
    hq_state: Optional[str] = None
    sector_focus: Optional[str] = None  # not exposed by NVCA directory; reserved


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
_WS_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def _normalize_website(href: str) -> Optional[str]:
    """Trim mailto:/tel: schemes and obvious junk hrefs."""
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    # Some entries link to relative anchors; only keep absolute URLs.
    if not href.startswith(("http://", "https://")):
        return None
    return href


def parse_members(html: str) -> list[NvcaMember]:
    """Extract member rows from the directory HTML.

    Returns a list (preserving page order, which is alphabetical on NVCA's
    end). De-duplicates by lowercased firm name — the page has been seen
    to repeat a handful of names when a firm has multiple office rows.
    """
    tree = HTMLParser(html)
    rows = tree.css("table.member_table tr.member_block")
    members: list[NvcaMember] = []
    seen: set[str] = set()
    for row in rows:
        name_cell = row.css_first("td.member_name")
        if name_cell is None:
            continue  # header row uses <th>, no td.member_name
        link = name_cell.css_first("a")
        name = _clean(link.text() if link else name_cell.text())
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        website = _normalize_website(link.attributes.get("href", "") if link else "")
        city_cell = row.css_first("td.member_city")
        state_cell = row.css_first("td.member_state")
        members.append(
            NvcaMember(
                name=name,
                website=website,
                hq_city=_clean(city_cell.text()) if city_cell else None,
                hq_state=_clean(state_cell.text()) if state_cell else None,
            )
        )
    return members


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class NvcaClient:
    """Fetches and caches the NVCA member directory.

    The whole directory comes back in one HTML page, so the cache stores
    the parsed list rather than individual lookups. ``fetch_members()``
    returns the cached list if fresh; otherwise it hits the network,
    parses, writes the cache, and returns.
    """

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        cache_path: Optional[pathlib.Path] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
            timeout=30.0,
            follow_redirects=True,
        )
        self._cache_path = cache_path or DEFAULT_CACHE
        self._ttl = ttl_seconds
        self._last_request: float = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_INTERVAL_SECONDS:
            time.sleep(MIN_INTERVAL_SECONDS - elapsed)
        self._last_request = time.monotonic()

    def _load_cache(self) -> Optional[list[NvcaMember]]:
        if not self._cache_path.exists():
            return None
        try:
            blob = json.loads(self._cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(blob, dict) or blob.get("version") != CACHE_VERSION:
            return None
        fetched_at = blob.get("fetched_at")
        if not isinstance(fetched_at, str):
            return None
        try:
            ts = datetime.fromisoformat(fetched_at)
        except ValueError:
            return None
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > self._ttl:
            return None
        entries = blob.get("members", [])
        if not isinstance(entries, list):
            return None
        return [NvcaMember(**e) for e in entries]

    def _save_cache(self, members: list[NvcaMember]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_VERSION,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "count": len(members),
            "members": [dataclasses.asdict(m) for m in members],
        }
        self._cache_path.write_text(json.dumps(payload, indent=2))

    def fetch_members(self, force_refresh: bool = False) -> list[NvcaMember]:
        if not force_refresh:
            cached = self._load_cache()
            if cached is not None:
                log.info("NVCA: loaded %d members from cache (%s)", len(cached), self._cache_path)
                return cached
        self._throttle()
        log.info("NVCA: fetching directory from %s", DIRECTORY_URL)
        resp = self._client.get(DIRECTORY_URL)
        resp.raise_for_status()
        members = parse_members(resp.text)
        log.info("NVCA: parsed %d members", len(members))
        self._save_cache(members)
        return members

    def close(self) -> None:
        self._client.close()
