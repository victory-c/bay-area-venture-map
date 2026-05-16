"""Orchestrator: read firms.seed.yaml, enrich, write firms.json.

Usage:
    uv run python -m scraper.build                       # default: seed-only
    uv run python -m scraper.build --enrich geocode      # add lat/lng
    uv run python -m scraper.build --enrich geocode,edgar
    uv run python -m scraper.build --firm sequoia        # rebuild one firm

Enrichment is opt-in because it makes live network calls. The seed file
already contains hand-curated baseline data, so the build always succeeds
even with no enrichers.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Iterable

import yaml

from scraper.edgar import IapdClient
from scraper.geocode import Geocoder
from scraper.llm_enrich import GeminiEnricher, QuotaExceeded, merge_into_firm
from scraper.sec_bulk import fetch_bay_area_vc_firms
from scraper.wikipedia import WikipediaClient

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = REPO_ROOT / "data" / "firms.seed.yaml"
OUT_PATH = REPO_ROOT / "data" / "firms.json"
SITE_OUT_PATH = REPO_ROOT / "site" / "firms.json"
GEOCODE_CACHE = REPO_ROOT / "data" / ".geocode-cache.json"
SEC_BULK_CACHE = REPO_ROOT / "data" / ".sec-bulk-cache.csv"

VALID_ENRICHERS = {"geocode", "edgar", "sec_bulk", "wikipedia", "llm"}

# Hand-verified coordinates for the seed firms. Used as a fallback when the
# geocoder is offline / blocked. Approximate to the building, sourced by
# cross-referencing addresses against public maps. Refresh via:
#     uv run python -m scraper.build --enrich geocode
DEFAULT_COORDINATES: dict[str, tuple[float, float]] = {
    "sequoia": (37.42175, -122.20140),
    "kleiner-perkins": (37.42200, -122.20170),
    "nea": (37.42150, -122.20100),
    "greylock": (37.42140, -122.20290),
    "a16z": (37.42160, -122.20080),
    "khosla": (37.41810, -122.20100),
    "lightspeed": (37.41890, -122.20240),
    "mayfield": (37.42100, -122.20270),
    "ivp": (37.42280, -122.19980),
    "redpoint": (37.42280, -122.19980),
    "dcm": (37.42040, -122.20280),
    "battery": (37.42240, -122.20020),
    "threshold": (37.42220, -122.20010),
    "crv": (37.42280, -122.19980),
    "foundation": (37.45200, -122.18180),
    "menlo": (37.45200, -122.18180),
    "norwest": (37.44750, -122.16440),
    "bessemer": (37.44350, -122.19140),
    "versant": (37.79050, -122.40170),
    "tcv": (37.45200, -122.18180),
    "trinity": (37.42280, -122.19980),
    "usvp": (37.42210, -122.20180),
    "accel": (37.44750, -122.16400),
    "felicis": (37.44740, -122.16170),
    "pear": (37.41780, -122.15560),
}


def load_seed() -> list[dict]:
    raw = yaml.safe_load(SEED_PATH.read_text())
    firms = raw["firms"]
    for firm in firms:
        if firm.get("lat") is None and firm["id"] in DEFAULT_COORDINATES:
            lat, lng = DEFAULT_COORDINATES[firm["id"]]
            firm["lat"] = lat
            firm["lng"] = lng
        firm.setdefault("tier", "rich")
    return firms


_DEDUP_SUFFIXES = (
    # Long-form legal suffixes (must come before their shorter prefixes).
    " a delaware limited liability company",
    " a california limited liability company",
    " a delaware limited partnership",
    " a delaware corporation",
    " limited liability company", " limited partnership",
    # Generic descriptors.
    " capital partners", " venture partners", " ventures", " capital",
    " partners", " management", " operations", " holdings", " group",
    " associates", " advisers", " advisors", " fund", " funds",
    " corporation", " corp", " company", " co",
    " llc", " l.l.c.", " lp", " l.p.", " inc", " inc.", " ltd",
)


def _dedup_key_from_name(name: str) -> str:
    s = name.lower().replace(".", "")
    s = re.sub(r"\s*\([^)]*\)\s*", " ", s)  # drop parenthetical aliases like "(a16z)"
    s = s.strip(" ,.")
    while True:
        stripped = s
        for suffix in _DEDUP_SUFFIXES:
            if stripped.endswith(suffix):
                stripped = stripped[: -len(suffix)].rstrip(" ,.")
                break
        if stripped == s:
            break
        s = stripped
    return "".join(ch for ch in s if ch.isalnum())


def merge_firms(seed: list[dict], scraped: list[dict]) -> list[dict]:
    """Concatenate seed (rich) + scraped (lite). Drop scraped duplicates.

    Dedup by sec_crd first, then by a normalised-name fallback for seed
    firms lacking a sec_crd. Seed records always win.
    """
    seed_crds = {str(f["sec_crd"]) for f in seed if f.get("sec_crd")}
    seed_name_keys = {_dedup_key_from_name(f["name"]) for f in seed}

    out = list(seed)
    for firm in scraped:
        crd = str(firm.get("sec_crd") or "")
        if crd and crd in seed_crds:
            continue
        if _dedup_key_from_name(firm["name"]) in seed_name_keys:
            continue
        out.append(firm)
    return out


def enrich_geocode(firms: list[dict]) -> None:
    geocoder = Geocoder(cache_path=GEOCODE_CACHE)
    try:
        for firm in firms:
            if firm.get("lat") is not None and firm.get("lng") is not None:
                continue
            coords = geocoder.lookup(firm["address"])
            if coords:
                firm["lat"] = coords.lat
                firm["lng"] = coords.lng
            else:
                log.warning("Failed to geocode %s — %s", firm["id"], firm["address"])
    finally:
        geocoder.close()


def enrich_edgar(firms: list[dict]) -> None:
    iapd = IapdClient()
    try:
        for firm in firms:
            crd = firm.get("sec_crd")
            info = None
            if crd:
                info = iapd.fetch_by_crd(str(crd))
            if info is None:
                info = iapd.fetch_by_name(firm["name"])
            if info is None or info.aum_usd is None:
                log.info("No EDGAR AUM for %s; keeping seed value", firm["id"])
                continue
            firm["aum_usd"] = info.aum_usd
            firm["aum_source"] = (
                f"SEC Form ADV (CRD {info.crd}, as of {info.aum_as_of or 'n/a'})"
            )
            if info.aum_as_of:
                firm["aum_as_of"] = info.aum_as_of
    finally:
        iapd.close()


def enrich_wikipedia(firms: list[dict]) -> None:
    """Pull founded/founders/key_people/AUM from Wikipedia infoboxes when an
    article exists. Purely additive — never overwrites a non-null seed value.

    Adds two new fields on a hit: ``wikipedia_url`` and ``wikipedia_key_people``
    (founders + key people, deduped against existing partners). Backfills
    ``founded`` and ``aum_usd`` only when they are currently missing.
    Coverage is typically 10-25% of firms — only well-known shops have articles.
    """
    client = WikipediaClient()
    try:
        hits = 0
        for firm in firms:
            info = client.lookup(firm["name"])
            if info is None:
                continue
            hits += 1
            firm["wikipedia_url"] = info.url
            if firm.get("founded") is None and info.founded:
                firm["founded"] = info.founded
            if not firm.get("aum_usd") and info.aum_usd:
                firm["aum_usd"] = info.aum_usd
                firm["aum_source"] = f"Wikipedia ({info.title})"
            wiki_people: list[str] = []
            seen = {
                p["name"].lower()
                for p in firm.get("partners", []) or []
                if isinstance(p, dict) and "name" in p
            }
            for name in info.founders + info.key_people:
                if name.lower() not in seen:
                    seen.add(name.lower()); wiki_people.append(name)
            if wiki_people:
                firm["wikipedia_key_people"] = wiki_people
            if info.industry and not firm.get("wikipedia_industry"):
                firm["wikipedia_industry"] = info.industry
        log.info("Wikipedia: matched %d / %d firms", hits, len(firms))
    finally:
        client.close()


def enrich_llm(firms: list[dict]) -> None:
    """Fill missing partners/stages/sectors/recent_investments via Gemini
    2.5 Flash with native Google Search. Only touches lite firms — rich
    firms are seed-curated and we trust those over any model output.

    Resumable: every successful query is cached, so if you stop the run
    (Ctrl-C or quota exhausted), re-running picks up exactly where it left
    off. Cache lives at ``data/.llm-enrich-cache.json``.

    Gates: only enriches firms missing partners/stages/sectors AND with a
    fund_count > 0 (skip obvious shell entities). Quota-exceeded errors
    are surfaced cleanly so the user knows to retry tomorrow.
    """
    enricher = GeminiEnricher()
    try:
        candidates = [
            f for f in firms
            if f.get("tier") == "lite"
            and not (f.get("partners") or f.get("stages") or f.get("sectors"))
            and (f.get("fund_count") or 0) > 0
        ]
        log.info(
            "LLM enrich: %d candidate firms (skipping %d rich + %d already-enriched)",
            len(candidates),
            sum(1 for f in firms if f.get("tier") == "rich"),
            len(firms) - len(candidates) - sum(1 for f in firms if f.get("tier") == "rich"),
        )
        merged = 0
        skipped_low_conf = 0
        for i, firm in enumerate(candidates, 1):
            try:
                info = enricher.enrich(firm)
            except QuotaExceeded as e:
                log.warning(
                    "Stopped at firm %d/%d — %s. Re-run later to resume.",
                    i, len(candidates), e,
                )
                break
            if info is None:
                continue
            if merge_into_firm(firm, info):
                merged += 1
            else:
                skipped_low_conf += 1
            if i % 25 == 0:
                log.info("LLM enrich: %d/%d done, %d merged, %d low-conf",
                         i, len(candidates), merged, skipped_low_conf)
        log.info(
            "LLM enrich: %d firms got LLM-sourced data; %d firms returned with confidence < %.2f",
            merged, skipped_low_conf,
            __import__("scraper.llm_enrich", fromlist=["MIN_CONFIDENCE_TO_MERGE"]).MIN_CONFIDENCE_TO_MERGE,
        )
    finally:
        enricher.close()


def enrich_sec_bulk(seed_firms: list[dict], use_nominatim: bool = False) -> list[dict]:
    """Fetch the SEC bulk Form ADV scrape and merge with the seed firms.

    Nominatim is opt-in (``--enrich geocode,sec_bulk`` enables it via the
    geocode pass on the merged list). Without Nominatim the scraped firms
    fall back to per-city centroid coordinates from CITY_TO_LATLNG —
    accurate enough for a Bay-Area-wide map.
    """
    geocoder: Geocoder | None = None
    if use_nominatim:
        geocoder = Geocoder(cache_path=GEOCODE_CACHE)
    try:
        scraped = fetch_bay_area_vc_firms(
            geocoder=geocoder,
            cache_path=SEC_BULK_CACHE,
        )
    finally:
        if geocoder is not None:
            geocoder.close()
    log.info("Scraped %d Bay Area VC firms from SEC bulk data", len(scraped))
    merged = merge_firms(seed_firms, scraped)
    log.info(
        "Merged: %d seed + %d scraped (after dedup) = %d total",
        len(seed_firms), len(merged) - len(seed_firms), len(merged),
    )
    return merged


def build(enrichers: Iterable[str], only_firm: str | None) -> dict:
    firms = load_seed()
    if only_firm:
        firms = [f for f in firms if f["id"] == only_firm]
        if not firms:
            raise SystemExit(f"No firm with id={only_firm!r} in {SEED_PATH}")

    enricher_set = set(enrichers)
    if "geocode" in enricher_set:
        enrich_geocode(firms)
    if "edgar" in enricher_set:
        enrich_edgar(firms)
    if "sec_bulk" in enricher_set and not only_firm:
        # If the user also asked for geocode, we already ran it on seed
        # firms above; flip use_nominatim so scraped firms are geocoded too.
        firms = enrich_sec_bulk(firms, use_nominatim="geocode" in enricher_set)
    # Wikipedia runs after SEC bulk so it sees the merged seed+scraped list
    # and can backfill founded/AUM for firms that came in via the bulk scrape.
    if "wikipedia" in enricher_set:
        enrich_wikipedia(firms)
    # LLM runs LAST so it only fills gaps the earlier (free, reliable) sources
    # couldn't. Rich firms (seed YAML) are skipped — they already have
    # hand-curated data we trust more than any model output.
    if "llm" in enricher_set:
        enrich_llm(firms)

    output = {
        "generated_with_enrichers": sorted(enricher_set),
        "firm_count": len(firms),
        "firms": firms,
    }
    return output


def write_outputs(payload: dict) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=False))
    SITE_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SITE_OUT_PATH.write_text(OUT_PATH.read_text())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--enrich",
        default="",
        help=f"Comma-separated enrichers to run. Available: {sorted(VALID_ENRICHERS)}",
    )
    parser.add_argument("--firm", default=None, help="Build only this firm id")
    args = parser.parse_args()

    enrichers = [e.strip() for e in args.enrich.split(",") if e.strip()]
    unknown = set(enrichers) - VALID_ENRICHERS
    if unknown:
        raise SystemExit(f"Unknown enrichers: {sorted(unknown)}")

    payload = build(enrichers, args.firm)
    write_outputs(payload)
    log.info(
        "Wrote %s firms → %s (and copy at %s)",
        payload["firm_count"],
        OUT_PATH,
        SITE_OUT_PATH,
    )


if __name__ == "__main__":
    main()
