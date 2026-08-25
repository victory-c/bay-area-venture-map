"""Bulk Form ADV scraper: enumerate Bay Area VC firms from SEC FOIA data.

Data source
-----------
The SEC publishes a public FOIA dataset of every Investment Adviser firm —
both Registered Investment Advisers (IAs) and Exempt Reporting Advisers
(ERAs) — as monthly ZIPped CSVs at:

    https://www.sec.gov/foia/docs/invafoia.htm
    -> redirects to /data-research/sec-markets-data/information-about-
       registered-investment-advisers-exempt-reporting-advisers

Each monthly snapshot has two files:

    iaMMDDYY.zip          — Registered Investment Advisers   (~1900 CA firms)
    iaMMDDYY-exempt.zip   — Exempt Reporting Advisers        (~800 CA firms)

The CSV inside is the "FIRM_ROSTER_FOIA_DOWNLOAD" with one row per firm and
~170-450 columns from Form ADV. Both share the same first ~100 columns,
including:

  - "Organization CRD#"                     (firm CRD)
  - "Primary Business Name"                 (DBA)
  - "Main Office Street Address 1/2"
  - "Main Office City" / "...State" / "...Postal Code"
  - "Main Office Telephone Number"
  - "Website Address"                       (firm's primary URL)
  - "Latest ADV Filing Date"
  - "Any VC Funds" / "Any PE Funds" /       ('Y' if Schedule D 7.B.1 has a
    "Any Hedge Funds" / ...                  fund of that type)
  - "Total number of VC funds" / ...        (per-firm fund counts by type)
  - "Count of Private Funds - 7B(1)"        (total fund count, all types)
  - "5F(2)(c)"                              (total regulatory AUM, IA file
                                             only — ERAs do not report it)
  - "Total Gross Assets of Private Funds"   (sum of Schedule D 7.B(1) per-fund
                                             gross asset values, pre-aggregated
                                             by SEC; populated for both IAs and
                                             ERAs — our AUM fallback for ERAs,
                                             which lifts AUM coverage from ~12%
                                             of Bay Area VCs to ~99%)
  - "5A"                                    (employee count, IA file only)

Filtering logic
---------------
Path A (this module):
  1. Download both ZIPs for the latest available month, cache them.
  2. Scan rows where Main Office State == 'CA' AND
     Main Office City is in the 5-county allowlist AND
     ("Any VC Funds" == 'Y' OR Firm Type indicates ERA — many small VCs
     file as ERAs and don't bother itemising fund types).
  3. Map each city -> county via a hardcoded ~150-entry CITY_TO_COUNTY dict.

Cache semantics
---------------
The default cache path is ``data/.sec-bulk-cache-v2.csv``. To reduce SEC load,
the cache stores the joined+filtered intermediate (CA-only rows from both
files) along with a ``# fetched: <isodate>`` header line. Honors a 7-day TTL.
The ``-v2`` suffix bumps when the cache columns change so old caches don't
get read with the new schema; old ``.sec-bulk-cache.csv`` files are orphaned
and can be deleted by hand.

CLI
---
    python -m scraper.sec_bulk

Prints the firm count and the first 5 entries.
"""

from __future__ import annotations

import csv
import io
import logging
import pathlib
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import httpx

from scraper.useragent import USER_AGENT as _UA

log = logging.getLogger(__name__)

USER_AGENT = _UA
INDEX_URL = "https://www.sec.gov/foia/docs/invafoia.htm"
MIN_INTERVAL_SECONDS = 0.15  # ~6 req/s; same as IapdClient
CACHE_TTL = timedelta(days=7)
DEFAULT_CACHE = pathlib.Path("data/.sec-bulk-cache-v2.csv")

# ---------------------------------------------------------------------------
# County mapping — 5-county core Bay Area
# ---------------------------------------------------------------------------
#
# CSV city values are uppercase (e.g. 'SAN FRANCISCO'). Keys here are uppercase.
# Source: Wikipedia "List of cities in the San Francisco Bay Area" cross-
# referenced with each county's incorporated cities + common CDPs (census-
# designated places that VCs sometimes list as their main office).
CITY_TO_COUNTY: dict[str, str] = {
    # San Francisco County
    "SAN FRANCISCO": "San Francisco",
    "SF": "San Francisco",
    # San Mateo County
    "ATHERTON": "San Mateo",
    "BELMONT": "San Mateo",
    "BRISBANE": "San Mateo",
    "BURLINGAME": "San Mateo",
    "COLMA": "San Mateo",
    "DALY CITY": "San Mateo",
    "EAST PALO ALTO": "San Mateo",
    "EL GRANADA": "San Mateo",
    "FOSTER CITY": "San Mateo",
    "HALF MOON BAY": "San Mateo",
    "HILLSBOROUGH": "San Mateo",
    "LA HONDA": "San Mateo",
    "LADERA": "San Mateo",
    "LOMA MAR": "San Mateo",
    "MENLO PARK": "San Mateo",
    "MILLBRAE": "San Mateo",
    "MONTARA": "San Mateo",
    "MOSS BEACH": "San Mateo",
    "PACIFICA": "San Mateo",
    "PESCADERO": "San Mateo",
    "PORTOLA VALLEY": "San Mateo",
    "REDWOOD CITY": "San Mateo",
    "REDWOOD SHORES": "San Mateo",
    "SAN BRUNO": "San Mateo",
    "SAN CARLOS": "San Mateo",
    "SAN GREGORIO": "San Mateo",
    "SAN MATEO": "San Mateo",
    "SOUTH SAN FRANCISCO": "San Mateo",
    "SOUTH SF": "San Mateo",
    "WOODSIDE": "San Mateo",
    # Santa Clara County
    "ALUM ROCK": "Santa Clara",
    "ALVISO": "Santa Clara",
    "BURBANK": "Santa Clara",
    "CAMBRIAN PARK": "Santa Clara",
    "CAMPBELL": "Santa Clara",
    "CUPERTINO": "Santa Clara",
    "EAST FOOTHILLS": "Santa Clara",
    "EVERGREEN": "Santa Clara",
    "GILROY": "Santa Clara",
    "LEXINGTON HILLS": "Santa Clara",
    "LOS ALTOS": "Santa Clara",
    "LOS ALTOS HILLS": "Santa Clara",
    "LOS GATOS": "Santa Clara",
    "LOYOLA": "Santa Clara",
    "MILPITAS": "Santa Clara",
    "MONTE SERENO": "Santa Clara",
    "MORGAN HILL": "Santa Clara",
    "MOUNTAIN VIEW": "Santa Clara",
    "PALO ALTO": "Santa Clara",
    "SAN JOSE": "Santa Clara",
    "SAN MARTIN": "Santa Clara",
    "SANTA CLARA": "Santa Clara",
    "SARATOGA": "Santa Clara",
    "STANFORD": "Santa Clara",
    "SUNNYVALE": "Santa Clara",
    # Alameda County
    "ALAMEDA": "Alameda",
    "ALBANY": "Alameda",
    "ASHLAND": "Alameda",
    "BERKELEY": "Alameda",
    "CASTRO VALLEY": "Alameda",
    "CHERRYLAND": "Alameda",
    "DUBLIN": "Alameda",
    "EMERYVILLE": "Alameda",
    "FAIRVIEW": "Alameda",
    "FREMONT": "Alameda",
    "HAYWARD": "Alameda",
    "LIVERMORE": "Alameda",
    "MOUNT EDEN": "Alameda",
    "NEWARK": "Alameda",
    "OAKLAND": "Alameda",
    "PIEDMONT": "Alameda",
    "PLEASANTON": "Alameda",
    "RUSSELL CITY": "Alameda",
    "SAN LEANDRO": "Alameda",
    "SAN LORENZO": "Alameda",
    "SUNOL": "Alameda",
    "UNION CITY": "Alameda",
    # Marin County
    "ALMONTE": "Marin",
    "ALTO": "Marin",
    "BELVEDERE": "Marin",
    "BOLINAS": "Marin",
    "CORTE MADERA": "Marin",
    "DILLON BEACH": "Marin",
    "FAIRFAX": "Marin",
    "FOREST KNOLLS": "Marin",
    "GREENBRAE": "Marin",
    "INVERNESS": "Marin",
    "KENTFIELD": "Marin",
    "LAGUNITAS": "Marin",
    "LARKSPUR": "Marin",
    "LUCAS VALLEY": "Marin",
    "MARIN CITY": "Marin",
    "MARINWOOD": "Marin",
    "MILL VALLEY": "Marin",
    "MUIR BEACH": "Marin",
    "NICASIO": "Marin",
    "NOVATO": "Marin",
    "OLEMA": "Marin",
    "POINT REYES STATION": "Marin",
    "ROSS": "Marin",
    "SAN ANSELMO": "Marin",
    "SAN GERONIMO": "Marin",
    "SAN QUENTIN": "Marin",
    "SAN RAFAEL": "Marin",
    "SAUSALITO": "Marin",
    "STINSON BEACH": "Marin",
    "STRAWBERRY": "Marin",
    "TAMALPAIS-HOMESTEAD VALLEY": "Marin",
    "TIBURON": "Marin",
    "TOMALES": "Marin",
    "WOODACRE": "Marin",
}


# Approximate city-centroid coordinates used as a fallback when Nominatim
# is unreachable (the seed pipeline already tolerates Nominatim being offline
# via DEFAULT_COORDINATES; this is the equivalent for scraped firms). Pins
# from the same city will visually overlap, which the user can disambiguate
# by clicking through to the side panel. Coverage focuses on the cities
# that contain the bulk of Bay Area VC offices.
CITY_TO_LATLNG: dict[str, tuple[float, float]] = {
    "SAN FRANCISCO": (37.7749, -122.4194),
    "MENLO PARK": (37.4530, -122.1817),
    "PALO ALTO": (37.4419, -122.1430),
    "MOUNTAIN VIEW": (37.3861, -122.0839),
    "SUNNYVALE": (37.3688, -122.0363),
    "SAN JOSE": (37.3382, -121.8863),
    "SANTA CLARA": (37.3541, -121.9552),
    "CUPERTINO": (37.3230, -122.0322),
    "LOS ALTOS": (37.3852, -122.1141),
    "LOS ALTOS HILLS": (37.3791, -122.1372),
    "LOS GATOS": (37.2358, -121.9624),
    "MILPITAS": (37.4323, -121.8996),
    "CAMPBELL": (37.2872, -121.9500),
    "SARATOGA": (37.2638, -122.0230),
    "STANFORD": (37.4275, -122.1697),
    "MORGAN HILL": (37.1305, -121.6544),
    "REDWOOD CITY": (37.4852, -122.2364),
    "REDWOOD SHORES": (37.5377, -122.2492),
    "SAN MATEO": (37.5630, -122.3255),
    "BURLINGAME": (37.5779, -122.3478),
    "ATHERTON": (37.4593, -122.1986),
    "WOODSIDE": (37.4297, -122.2538),
    "PORTOLA VALLEY": (37.3838, -122.2353),
    "HILLSBOROUGH": (37.5630, -122.3636),
    "BELMONT": (37.5202, -122.2758),
    "FOSTER CITY": (37.5585, -122.2711),
    "SAN CARLOS": (37.5072, -122.2603),
    "MILLBRAE": (37.5985, -122.3872),
    "DALY CITY": (37.6879, -122.4702),
    "SOUTH SAN FRANCISCO": (37.6547, -122.4077),
    "SOUTH SF": (37.6547, -122.4077),
    "EAST PALO ALTO": (37.4688, -122.1411),
    "PACIFICA": (37.6138, -122.4869),
    "SAN BRUNO": (37.6305, -122.4111),
    "BERKELEY": (37.8716, -122.2727),
    "OAKLAND": (37.8044, -122.2712),
    "EMERYVILLE": (37.8313, -122.2852),
    "ALAMEDA": (37.7652, -122.2416),
    "ALBANY": (37.8868, -122.2978),
    "PLEASANTON": (37.6624, -121.8747),
    "LIVERMORE": (37.6819, -121.7681),
    "FREMONT": (37.5485, -121.9886),
    "HAYWARD": (37.6688, -122.0808),
    "DUBLIN": (37.7022, -121.9358),
    "PIEDMONT": (37.8242, -122.2316),
    "SAN RAFAEL": (37.9735, -122.5311),
    "SAUSALITO": (37.8591, -122.4853),
    "MILL VALLEY": (37.9060, -122.5450),
    "TIBURON": (37.8735, -122.4569),
    "LARKSPUR": (37.9341, -122.5353),
    "CORTE MADERA": (37.9255, -122.5275),
    "NOVATO": (38.1074, -122.5697),
    "ROSS": (37.9624, -122.5547),
    "SAN ANSELMO": (37.9746, -122.5616),
    "BELVEDERE": (37.8728, -122.4641),
    "GREENBRAE": (37.9518, -122.5394),
    "KENTFIELD": (37.9518, -122.5589),
    "NICASIO": (38.0641, -122.7036),
    "FAIRFAX": (37.9871, -122.5888),
    "STINSON BEACH": (37.9046, -122.6433),
    "BOLINAS": (37.9088, -122.6862),
    "INVERNESS": (38.1015, -122.8569),
    "POINT REYES STATION": (38.0688, -122.8055),
    "MUIR BEACH": (37.8624, -122.5733),
    "SAN GERONIMO": (38.0124, -122.6722),
    "FOREST KNOLLS": (38.0124, -122.6921),
    "LAGUNITAS": (38.0152, -122.7028),
    "ALMONTE": (37.8848, -122.5419),
    "MARIN CITY": (37.8688, -122.5066),
    "STRAWBERRY": (37.8908, -122.5072),
    "TAMALPAIS-HOMESTEAD VALLEY": (37.8771, -122.5469),
    "OLEMA": (38.0432, -122.7869),
    "DILLON BEACH": (38.2521, -122.9669),
    "TOMALES": (38.2477, -122.9061),
    "WOODACRE": (38.0179, -122.6451),
    "LUCAS VALLEY": (38.0466, -122.5811),
    "MARINWOOD": (38.0479, -122.5800),
    "SAN QUENTIN": (37.9460, -122.4882),
    "EL GRANADA": (37.5099, -122.4683),
    "HALF MOON BAY": (37.4636, -122.4286),
    "MONTARA": (37.5413, -122.5036),
    "MOSS BEACH": (37.5249, -122.5141),
    "PESCADERO": (37.2552, -122.3833),
    "LA HONDA": (37.3197, -122.2747),
    "LOMA MAR": (37.2657, -122.3253),
    "SAN GREGORIO": (37.3268, -122.3872),
    "LADERA": (37.4035, -122.2017),
    "ALVISO": (37.4271, -121.9730),
    "ALUM ROCK": (37.3705, -121.8285),
    "EVERGREEN": (37.3263, -121.8118),
    "EAST FOOTHILLS": (37.3791, -121.8290),
    "CAMBRIAN PARK": (37.2566, -121.9433),
    "MONTE SERENO": (37.2358, -122.0250),
    "LEXINGTON HILLS": (37.1880, -121.9865),
    "GILROY": (37.0058, -121.5683),
    "SAN MARTIN": (37.0860, -121.6105),
    "LOYOLA": (37.3508, -122.1130),
    "ASHLAND": (37.6946, -122.1138),
    "CASTRO VALLEY": (37.6940, -122.0863),
    "CHERRYLAND": (37.6790, -122.0980),
    "FAIRVIEW": (37.6732, -122.0427),
    "MOUNT EDEN": (37.6435, -122.1108),
    "NEWARK": (37.5297, -121.9857),
    "RUSSELL CITY": (37.6285, -122.1397),
    "SAN LEANDRO": (37.7249, -122.1561),
    "SAN LORENZO": (37.6810, -122.1247),
    "SUNOL": (37.5938, -121.8861),
    "UNION CITY": (37.5933, -122.0438),
    "BRISBANE": (37.6810, -122.4000),
    "COLMA": (37.6766, -122.4583),
}


def _city_from_address(address: str) -> Optional[str]:
    """Extract city from a 'street, City, STATE ZIP' formatted address."""
    parts = [p.strip() for p in address.split(",")]
    if len(parts) >= 3:
        return parts[-2].upper()
    return None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _build_client(existing: Optional[httpx.Client] = None) -> httpx.Client:
    if existing is not None:
        return existing
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        timeout=120.0,
        follow_redirects=True,
    )


def _throttled_get(client: httpx.Client, url: str, last_request: list[float]) -> httpx.Response:
    elapsed = time.monotonic() - last_request[0]
    if elapsed < MIN_INTERVAL_SECONDS:
        time.sleep(MIN_INTERVAL_SECONDS - elapsed)
    resp = client.get(url)
    last_request[0] = time.monotonic()
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Discover the latest snapshot
# ---------------------------------------------------------------------------
# Filenames look like:
#   /files/.../ia050126.zip          (registered, dated 2026-05-01)
#   /files/.../ia050126-exempt.zip   (exempt-reporting, dated 2026-05-01)
# Date encoding in the filename is MMDDYY.
_FILENAME_RE = re.compile(
    r'href="(?P<href>[^"]*?ia(?P<mm>\d{2})(?P<dd>\d{2})(?P<yy>\d{2})(?P<exempt>-exempt)?\.zip)"',
    re.IGNORECASE,
)


def _discover_latest_urls(client: httpx.Client, last_request: list[float]) -> tuple[str, str, str]:
    """Return (registered_url, exempt_url, snapshot_iso_date)."""
    resp = _throttled_get(client, INDEX_URL, last_request)
    html = resp.text
    by_date: dict[str, dict[str, str]] = {}
    for m in _FILENAME_RE.finditer(html):
        mm = int(m.group("mm"))
        dd = int(m.group("dd"))
        yy = int(m.group("yy"))
        # SEC has been publishing since 2009; treat 00-79 as 20xx, 80-99 as 19xx.
        year = 2000 + yy if yy < 80 else 1900 + yy
        try:
            iso = datetime(year, mm, dd, tzinfo=timezone.utc).date().isoformat()
        except ValueError:
            continue
        href = m.group("href")
        if not href.startswith("http"):
            href = "https://www.sec.gov" + href
        kind = "exempt" if m.group("exempt") else "registered"
        by_date.setdefault(iso, {})[kind] = href

    # Pick the most recent date that has both files.
    for iso in sorted(by_date.keys(), reverse=True):
        urls = by_date[iso]
        if "registered" in urls and "exempt" in urls:
            return urls["registered"], urls["exempt"], iso

    raise RuntimeError(
        f"No SEC IA bulk snapshot found at {INDEX_URL}. "
        f"Discovered dates: {sorted(by_date.keys())[-5:] if by_date else 'none'}"
    )


# ---------------------------------------------------------------------------
# CSV reading from the ZIPs
# ---------------------------------------------------------------------------
def _iter_csv_from_zip_bytes(blob: bytes) -> Iterable[dict]:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        members = [n for n in zf.namelist() if n.upper().endswith(".CSV")]
        if not members:
            raise RuntimeError(f"No CSV inside ZIP. Members: {zf.namelist()}")
        with zf.open(members[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
            reader = csv.DictReader(text)
            for row in reader:
                yield row


def _format_address(row: dict) -> str:
    line1 = (row.get("Main Office Street Address 1") or "").strip()
    line2 = (row.get("Main Office Street Address 2") or "").strip()
    city = (row.get("Main Office City") or "").strip()
    state = (row.get("Main Office State") or "").strip()
    postal = (row.get("Main Office Postal Code") or "").strip().split("-")[0]
    street = f"{line1} {line2}".strip() if line2 else line1
    parts = [p for p in [street, _titlecase_city(city), f"{state} {postal}".strip()] if p]
    return ", ".join(parts)


def _titlecase_city(city: str) -> str:
    # CSV has uppercase ('SAN FRANCISCO') — output should be human-readable.
    return " ".join(w.capitalize() for w in city.split())


def _parse_aum(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    cleaned = raw.replace(",", "").replace("$", "").strip()
    if not cleaned or cleaned in {".", ".00"}:
        return None
    try:
        n = round(float(cleaned))
    except ValueError:
        return None
    return n if n > 0 else None


def _parse_int(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    cleaned = str(raw).replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def _normalize_website(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip()
    if not s or s.upper() in {"N/A", "NONE", "NULL"}:
        return None
    # SEC entries vary: bare domains, www.x.com, http://x.com, X.COM. Force a
    # scheme so the frontend can render it as an <a href> without rewriting.
    if not re.match(r"^https?://", s, re.IGNORECASE):
        s = "https://" + s.lstrip("/")
    # SEC stores these UPPERCASE. Scheme and host are case-insensitive, so
    # normalise them; the path is not, so leave it alone.
    m = re.match(r"^(https?://[^/]+)(.*)$", s, re.IGNORECASE)
    if m:
        s = m.group(1).lower() + m.group(2)
    return s


def _normalize_phone(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip()
    if not s or s.upper() in {"N/A", "NONE", "NULL"}:
        return None
    return s


def _is_vc_firm(row: dict) -> bool:
    """Liberal VC filter — include any ERA, plus IAs that report VC funds.

    The user prefers over-inclusion to missing firms. ERAs are almost
    exclusively VC/PE shops by statute (Section 203(l)/(m) of the Advisers
    Act exempts firms that *only* advise VC funds or that have <$150M in
    private fund AUM). For Registered IAs we require Schedule D 7.B.1 to
    flag at least one VC fund.
    """
    firm_type = (row.get("Firm Type") or "").strip().upper()
    if "EXEMPT" in firm_type or "ERA" in firm_type:
        return True
    return (row.get("Any VC Funds") or "").strip().upper() == "Y"


# ---------------------------------------------------------------------------
# Cache (intermediate filtered CSV, not the raw 50MB zips)
# ---------------------------------------------------------------------------
_CACHE_FIELDS = [
    "crd",
    "name",
    "addr1",
    "addr2",
    "city",
    "state",
    "postal",
    "firm_type",
    "any_vc",
    "any_pe",
    "any_hedge",
    "any_re",
    "any_securitized",
    "any_other",
    "num_vc",
    "num_pe",
    "num_hedge",
    "fund_count",
    "aum_raw",
    "aum_pf_raw",
    "website",
    "phone",
    "latest_filing",
    "employees",
]


def _cache_is_fresh(cache_path: pathlib.Path) -> bool:
    if not cache_path.exists():
        return False
    try:
        with cache_path.open("r", encoding="utf-8") as f:
            header = f.readline()
    except OSError:
        return False
    m = re.match(r"# fetched: (\S+)", header)
    if not m:
        return False
    try:
        fetched = datetime.fromisoformat(m.group(1))
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched < CACHE_TTL


def _load_cache(cache_path: pathlib.Path) -> list[dict]:
    rows: list[dict] = []
    with cache_path.open("r", encoding="utf-8") as f:
        f.readline()  # discard "# fetched: ..." header
        rows.extend(csv.DictReader(f))
    return rows


def _save_cache(cache_path: pathlib.Path, rows: list[dict]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8", newline="") as f:
        f.write(f"# fetched: {datetime.now(timezone.utc).isoformat()}\n")
        writer = csv.DictWriter(f, fieldnames=_CACHE_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in _CACHE_FIELDS})


def _row_to_cache_dict(row: dict) -> dict:
    # Both REG (448 cols) and ERA (171 cols) files share the column names we
    # read here; the ERA file simply lacks Item 5 columns (`5F(2)(c)`, `5A`),
    # which `row.get` returns None for — caller treats blank == not reported.
    return {
        "crd": (row.get("Organization CRD#") or "").strip(),
        "name": (row.get("Primary Business Name") or row.get("Legal Name") or "").strip(),
        "addr1": (row.get("Main Office Street Address 1") or "").strip(),
        "addr2": (row.get("Main Office Street Address 2") or "").strip(),
        "city": (row.get("Main Office City") or "").strip(),
        "state": (row.get("Main Office State") or "").strip(),
        "postal": (row.get("Main Office Postal Code") or "").strip(),
        "firm_type": (row.get("Firm Type") or "").strip(),
        "any_vc": (row.get("Any VC Funds") or "").strip(),
        "any_pe": (row.get("Any PE Funds") or "").strip(),
        "any_hedge": (row.get("Any Hedge Funds") or "").strip(),
        "any_re": (row.get("Any Real Estate Funds") or "").strip(),
        "any_securitized": (row.get("Any Securitized Funds") or "").strip(),
        "any_other": (row.get("Any Other Funds") or "").strip(),
        "num_vc": (row.get("Total number of VC funds") or "").strip(),
        "num_pe": (row.get("Total number of PE funds") or "").strip(),
        "num_hedge": (row.get("Total number of Hedge funds") or "").strip(),
        "fund_count": (row.get("Count of Private Funds - 7B(1)") or "").strip(),
        "aum_raw": (row.get("5F(2)(c)") or "").strip(),
        "aum_pf_raw": (row.get("Total Gross Assets of Private Funds") or "").strip(),
        "website": (row.get("Website Address") or "").strip(),
        "phone": (row.get("Main Office Telephone Number") or "").strip(),
        "latest_filing": (row.get("Latest ADV Filing Date") or "").strip(),
        "employees": (row.get("5A") or "").strip(),
    }


def _refresh_cache(cache_path: pathlib.Path, client: httpx.Client) -> list[dict]:
    last_request = [0.0]
    reg_url, era_url, snapshot_date = _discover_latest_urls(client, last_request)
    log.info("SEC snapshot %s — fetching:\n  IA : %s\n  ERA: %s", snapshot_date, reg_url, era_url)

    out: list[dict] = []
    for url, label in [(reg_url, "registered"), (era_url, "exempt")]:
        log.info("Downloading %s file...", label)
        resp = _throttled_get(client, url, last_request)
        log.info("  -> %s bytes; parsing CSV", len(resp.content))
        kept = 0
        for row in _iter_csv_from_zip_bytes(resp.content):
            if (row.get("Main Office State") or "").strip().upper() != "CA":
                continue
            if not _is_vc_firm(row):
                continue
            out.append(_row_to_cache_dict(row))
            kept += 1
        log.info("  -> kept %s CA VC rows from %s", kept, label)

    _save_cache(cache_path, out)
    return out


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
def fetch_bay_area_vc_firms(
    *,
    geocoder=None,
    iapd_client=None,
    cache_path: pathlib.Path | None = None,
    max_firms: int | None = None,
) -> list[dict]:
    """Return Bay Area VC firms scraped from SEC Form ADV bulk data.

    See module docstring for data source and filtering details.
    """
    cache_path = cache_path or DEFAULT_CACHE
    if isinstance(cache_path, str):
        cache_path = pathlib.Path(cache_path)

    # Reuse the IapdClient's httpx.Client if provided, else build our own.
    owned_client: Optional[httpx.Client] = None
    client: httpx.Client
    if iapd_client is not None and getattr(iapd_client, "_client", None) is not None:
        client = iapd_client._client  # share connection pool & UA
    else:
        owned_client = _build_client()
        client = owned_client

    try:
        if _cache_is_fresh(cache_path):
            log.info("Using fresh SEC bulk cache: %s", cache_path)
            cached = _load_cache(cache_path)
        else:
            log.info("SEC bulk cache stale or missing; refreshing")
            cached = _refresh_cache(cache_path, client)
    finally:
        if owned_client is not None:
            owned_client.close()

    # Apply Bay Area city filter on cached rows.
    firms: list[dict] = []
    seen_crds: set[str] = set()
    for r in cached:
        crd = (r.get("crd") or "").strip()
        if not crd or crd in seen_crds:
            continue
        city = (r.get("city") or "").strip().upper()
        county = CITY_TO_COUNTY.get(city)
        if county is None:
            continue
        # Bay Area ZIPs are 940xx-951xx (and 949xx for Marin). Some city
        # names collide with non-Bay-Area places (e.g. "BURBANK" exists as a
        # tiny CDP near San Jose AND as the LA-county city); the ZIP gate
        # rejects the latter without losing real Bay Area firms.
        postal = (r.get("postal") or "").strip()[:3]
        if postal and not postal.startswith(("94", "95")):
            continue
        name = (r.get("name") or "").strip()
        if not name:
            continue
        seen_crds.add(crd)

        synthetic_row = {
            "Main Office Street Address 1": r.get("addr1", ""),
            "Main Office Street Address 2": r.get("addr2", ""),
            "Main Office City": r.get("city", ""),
            "Main Office State": r.get("state", ""),
            "Main Office Postal Code": r.get("postal", ""),
        }
        address = _format_address(synthetic_row)
        aum_reg = _parse_aum(r.get("aum_raw"))
        aum_pf = _parse_aum(r.get("aum_pf_raw"))
        # Priority: regulatory AUM (Item 5.F(2)(c)) when populated — it's the
        # canonical SEC figure and includes non-private-fund assets. Fall back
        # to Schedule D 7.B(1) gross asset sum, which is what ERAs report.
        latest_filing = r.get("latest_filing") or None
        if aum_reg is not None:
            aum_usd = aum_reg
            aum_source = "SEC Form ADV Item 5.F(2)(c) regulatory AUM"
        elif aum_pf is not None:
            aum_usd = aum_pf
            aum_source = "SEC Form ADV Schedule D 7.B(1) private-fund gross assets"
        else:
            aum_usd = None
            aum_source = None
        if aum_source and latest_filing:
            aum_source = f"{aum_source} (filed {latest_filing})"

        firm_type_tags: list[str] = []
        for flag, tag in (
            ("any_vc", "vc"),
            ("any_pe", "pe"),
            ("any_hedge", "hedge"),
            ("any_re", "real_estate"),
            ("any_securitized", "securitized"),
            ("any_other", "other"),
        ):
            if (r.get(flag) or "").strip().upper() == "Y":
                firm_type_tags.append(tag)

        firm: dict = {
            "id": f"sec-{crd}",
            "name": _normalize_name(name),
            "address": address,
            "sec_crd": crd,
            "aum_usd": aum_usd,
            "aum_source": aum_source,
            "aum_as_of": latest_filing,
            "county": county,
            "tier": "lite",
            "website": _normalize_website(r.get("website")),
            "phone": _normalize_phone(r.get("phone")),
            "fund_count": _parse_int(r.get("fund_count")),
            "vc_fund_count": _parse_int(r.get("num_vc")),
            "pe_fund_count": _parse_int(r.get("num_pe")),
            "hedge_fund_count": _parse_int(r.get("num_hedge")),
            "firm_type_tags": firm_type_tags,
            "employee_count": _parse_int(r.get("employees")),
            "latest_filing_date": latest_filing,
            # Empty schema invariants so frontend consumers (allStages,
            # allSectors, visibleFirms in site/app.js) can iterate without
            # null guards. SEC bulk records carry no stage/sector taxonomy.
            "stages": [],
            "sectors": [],
            "lat": None,
            "lng": None,
        }
        firms.append(firm)

    firms.sort(key=lambda f: (f["name"].lower(), f["sec_crd"]))

    if max_firms is not None:
        firms = firms[:max_firms]

    if geocoder is not None:
        for firm in firms:
            try:
                coords = geocoder.lookup(firm["address"])
            except Exception as e:  # noqa: BLE001
                log.warning("Geocode failed for %s (%s): %s", firm["name"], firm["address"], e)
                continue
            if coords is not None:
                firm["lat"] = coords.lat
                firm["lng"] = coords.lng

    # City-centroid fallback for any firm that didn't get geocoded.
    fallback_hits = 0
    for firm in firms:
        if firm["lat"] is not None:
            continue
        city = _city_from_address(firm["address"])
        if city and city in CITY_TO_LATLNG:
            lat, lng = CITY_TO_LATLNG[city]
            firm["lat"] = lat
            firm["lng"] = lng
            fallback_hits += 1
    if fallback_hits:
        log.info("Applied city-centroid fallback to %d firms", fallback_hits)

    return firms


def _normalize_name(name: str) -> str:
    """Convert SEC's UPPERCASE legal names to a more readable mixed-case.

    Acronyms (LLC, LP, LLP, LTD, INC, USA, II, III, IV, etc.) are preserved.
    """
    keep_upper = {
        "LLC", "L.L.C.", "LP", "L.P.", "LLP", "LTD", "INC", "INC.",
        "PLC", "USA", "US", "AG", "SA", "UK", "PE", "VC", "BD", "IA",
        "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
        "XI", "XII", "&",
    }
    out: list[str] = []
    for word in name.split():
        if word.upper() in keep_upper or (word.upper().endswith(",") and word.upper().rstrip(",") in keep_upper):
            out.append(word.upper())
        else:
            # Title-case but preserve internal apostrophes/hyphens.
            out.append(word.capitalize())
    return " ".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        firms = fetch_bay_area_vc_firms()
    except Exception as e:  # noqa: BLE001
        log.error("Fetch failed: %s", e)
        return 1
    print(f"\nFetched {len(firms)} Bay Area VC firms from SEC bulk data.\n")
    print("First 5:")
    for f in firms[:5]:
        aum = f"${f['aum_usd']:,}" if f["aum_usd"] is not None else "n/a"
        print(f"  - {f['name']}  [{f['county']}]  CRD={f['sec_crd']}  AUM={aum}")
        print(f"      {f['address']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
