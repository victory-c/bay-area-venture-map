"""SEC Investment Adviser Public Disclosure (IAPD) lookup.

Fetches a registered investment adviser's regulatory AUM (Form ADV Item 5.F1)
via the public IAPD search API at api.adviserinfo.sec.gov.

This is the legitimate, free, public path. SEC asks for a descriptive
User-Agent (https://www.sec.gov/os/accessing-edgar-data) and a max of 10
requests per second per host.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from scraper.useragent import USER_AGENT as _UA

log = logging.getLogger(__name__)

USER_AGENT = _UA
SEARCH_URL = "https://api.adviserinfo.sec.gov/search/firm"
MIN_INTERVAL_SECONDS = 0.15  # ~6 req/s; well under SEC's 10 req/s cap


@dataclass
class AdviserInfo:
    crd: str
    legal_name: str
    aum_usd: Optional[int]
    aum_as_of: Optional[str]  # ISO date
    last_filing_date: Optional[str]


class IapdClient:
    def __init__(self, client: Optional[httpx.Client] = None) -> None:
        self._client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=20.0,
        )
        self._last_request: float = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_INTERVAL_SECONDS:
            time.sleep(MIN_INTERVAL_SECONDS - elapsed)
        self._last_request = time.monotonic()

    def search(self, query: str) -> list[dict]:
        """Search for firms by name or CRD. Returns the raw `hits` list."""
        self._throttle()
        params = {
            "query": query,
            "hl": "true",
            "nrows": "12",
            "start": "0",
            "r": "25",
            "type": "Firm",
            "investmentAdvisorType": "IA",
        }
        resp = self._client.get(SEARCH_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("hits", {}).get("hits", [])

    def fetch_by_crd(self, crd: str) -> Optional[AdviserInfo]:
        """Look up a firm by CRD and extract regulatory AUM."""
        hits = self.search(crd)
        for hit in hits:
            source = hit.get("_source", {})
            firm_crd = str(source.get("org_crd") or source.get("ind_source_id") or "")
            if firm_crd == str(crd):
                return self._parse_hit(source)
        log.warning("No IAPD match for CRD %s", crd)
        return None

    def fetch_by_name(self, name: str) -> Optional[AdviserInfo]:
        """Best-effort lookup by name. Returns the top hit or None."""
        hits = self.search(name)
        if not hits:
            return None
        return self._parse_hit(hits[0].get("_source", {}))

    @staticmethod
    def _parse_hit(source: dict) -> AdviserInfo:
        # IAPD field names vary by snapshot; we read defensively.
        crd = str(source.get("org_crd") or source.get("firm_id") or "")
        name = source.get("org_name") or source.get("firm_name") or ""
        aum = source.get("firm_ia_aum")  # regulatory AUM in dollars
        aum_int: Optional[int]
        try:
            aum_int = int(aum) if aum not in (None, "") else None
        except (TypeError, ValueError):
            aum_int = None
        return AdviserInfo(
            crd=crd,
            legal_name=name,
            aum_usd=aum_int,
            aum_as_of=source.get("firm_ia_aum_date"),
            last_filing_date=source.get("firm_latest_adv_filing_date"),
        )

    def close(self) -> None:
        self._client.close()
