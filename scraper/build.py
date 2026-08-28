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
from scraper.form_d import FormDClient
from scraper.geocode import Geocoder
from scraper.llm_enrich import (
    SECTORS,
    GeminiEnricher,
    QuotaExceeded,
    merge_into_firm,
)
from scraper.nvca import NvcaClient
from scraper.platforms import annotate_platforms, platform_candidates
from scraper.sec_bulk import DEFAULT_CACHE as SEC_BULK_CACHE_NAME
from scraper.sec_bulk import fetch_bay_area_vc_firms
from scraper.website_enrich import WebsiteEnricher
from scraper.wikipedia import WikipediaClient

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = REPO_ROOT / "data" / "firms.seed.yaml"
OUT_PATH = REPO_ROOT / "data" / "firms.json"
SITE_OUT_PATH = REPO_ROOT / "site" / "firms.json"
GEOCODE_CACHE = REPO_ROOT / "data" / ".geocode-cache.json"
NVCA_CACHE = REPO_ROOT / "data" / ".nvca-cache.json"
FORM_D_CACHE = REPO_ROOT / "data" / ".form-d-cache.json"
WEBSITE_CACHE = REPO_ROOT / "data" / ".website-enrich-cache.json"
# Take the filename from sec_bulk rather than restating it. The "-v2" suffix
# there is a schema-version marker: restating the v1 name here meant a column
# change would have been read back from a stale-schema cache.
SEC_BULK_CACHE = REPO_ROOT / SEC_BULK_CACHE_NAME

VALID_ENRICHERS = {
    "geocode", "edgar", "sec_bulk", "wikipedia",
    "nvca", "form_d", "llm", "website",
}

#: The complete Form D block. Derived wholly from the EFTS query, so
#: ``enrich_form_d`` writes and clears it as a unit.
FORM_D_FIELDS = (
    "form_d_total_filings", "form_d_latest_filing_date", "form_d_distinct_funds",
    "form_d_fund_ciks", "form_d_recent_filings",
)

# Fields that only an *optional* enricher — or an out-of-band pass such as
# ``scraper.glm_enrich`` — ever writes. A build that doesn't run that pass
# has no way to reproduce them.
#
# This list is why ``carry_forward_enrichment`` exists. ``build()`` starts
# from the seed and rebuilds ``firms.json`` wholesale, so the monthly
# ``--enrich sec_bulk`` refresh used to emit a payload missing every one of
# these and overwrite the good file with it. That is not hypothetical: commit
# 6692ed9 ("data: monthly SEC refresh (2026-06-05)") dropped 98,532 lines and
# cut the manifest from six enrichers to one, which is why wikipedia_*,
# nvca_member, llm_* and website_enriched still sit at zero coverage today.
#
# Carry-forward fills gaps from the *immediately* preceding payload, so it
# restores a block the current run couldn't produce. Run form_d against a
# stale file and it could re-add a block that run had deliberately cleared —
# always refresh from the current firms.json.
PRESERVED_FIELDS = (
    # scraper.glm_enrich — sectors/stages tagging + one-line thesis
    "inferred", "inference_confidence", "inference_basis", "inference_model",
    "inferred_thesis", "thesis_confidence",
    # scraper.glm_website_tag — provenance for the website-sourced tags and
    # theses. Written only by that pass and stored only in firms.json, so a
    # partial rebuild cannot regenerate them; without carry-forward a bare
    # `--enrich sec_bulk` silently drops all three.
    "inference_source", "inference_evidence", "thesis_source",
    # --enrich form_d
    *FORM_D_FIELDS,
    # --enrich wikipedia
    "wikipedia_url", "wikipedia_key_people", "wikipedia_industry",
    # --enrich nvca
    "nvca_member",
    # scraper.platforms
    "firm_role", "platform_note",
    # --enrich llm / website
    "llm_enriched", "llm_confidence", "llm_sources", "website_enriched",
    # Written by several passes; only carried when the new build has none.
    "sectors", "stages", "partners", "recent_portfolio_sample", "notes",
    "founded",
)

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


def enrich_nvca(firms: list[dict]) -> None:
    """Flag firms that appear in the NVCA member directory.

    Sets ``nvca_member: true`` on every match (later surfaced as a UI
    credibility badge) and backfills ``website`` and ``sectors`` only when
    the firm currently has no value. Existing values always win — this
    pass is strictly additive.

    Matching is by normalised firm name via ``_dedup_key_from_name``, the
    same key used to dedup seed vs. SEC-bulk firms, so legal suffixes
    (LLC / LP / etc.) and parenthetical aliases don't break matches.

    NVCA membership is ~400 firms nationally; expected Bay Area hit
    count against our firms list is ~30-60.
    """
    client = NvcaClient(cache_path=NVCA_CACHE)
    try:
        members = client.fetch_members()
    finally:
        client.close()

    # Build a key -> member index. If NVCA lists two firms that normalise to
    # the same key (rare, but possible with reused brand names), keep the
    # first one — they're alphabetical so this is stable.
    by_key: dict[str, object] = {}
    for m in members:
        key = _dedup_key_from_name(m.name)
        if not key:
            continue
        by_key.setdefault(key, m)

    hits = 0
    backfilled_website = 0
    backfilled_sectors = 0
    for firm in firms:
        key = _dedup_key_from_name(firm["name"])
        member = by_key.get(key)
        if member is None:
            continue
        hits += 1
        firm["nvca_member"] = True
        if not firm.get("website") and member.website:
            firm["website"] = member.website
            backfilled_website += 1
        # Sector focus isn't exposed by the NVCA directory; nothing to
        # backfill into `sectors` today. The hook stays here so future
        # NVCA schema additions (or a separate sector-inference pass) can
        # populate it without re-touching this function.
        # Validate against the shared vocabulary before writing. `sectors`
        # is rendered as markup by the front end, and every other writer
        # already filters against SECTORS — this was the one unchecked path
        # into that field.
        if not firm.get("sectors") and member.sector_focus:
            if member.sector_focus in SECTORS:
                firm["sectors"] = [member.sector_focus]
                backfilled_sectors += 1
            else:
                log.warning(
                    "NVCA: dropping unrecognised sector_focus %r for %s",
                    member.sector_focus, firm["id"],
                )
    log.info(
        "NVCA: %d / %d firms flagged as members (backfilled %d websites, %d sectors)",
        hits, len(firms), backfilled_website, backfilled_sectors,
    )


def enrich_form_d(firms: list[dict]) -> None:
    """Aggregate SEC Form D filings per firm via EDGAR full-text search.

    Adds (only when at least one filing matches):
        ``form_d_total_filings``         — count of validated filings
        ``form_d_latest_filing_date``    — ISO date of most recent close
        ``form_d_distinct_funds``        — up to 10 fund-entity names
        ``form_d_fund_ciks``             — up to 10 fund-entity CIKs
        ``form_d_recent_filings``        — top 5 most recent filings

    Coverage is heavily skewed toward brand-recognisable firms; obscure
    shell entities return zero and are skipped (and cached as null so
    re-runs don't re-query them).

    Unlike the other enrichers this one is *authoritative* rather than
    purely additive: a firm's Form D block is wholly derived from the EFTS
    query, so when a re-run matches nothing the previous block must be
    removed. Additive-only semantics would have pinned the 15 filings
    wrongly attributed to Founders Fund in place forever, because the
    matcher that produced them is exactly what a re-run corrects.
    """
    client = FormDClient(cache_path=FORM_D_CACHE)
    try:
        hits = 0
        cleared = 0
        for firm in firms:
            info = client.lookup(firm["name"])
            if info is None or info.total_filings == 0:
                if any(k in firm for k in FORM_D_FIELDS):
                    for key in FORM_D_FIELDS:
                        firm.pop(key, None)
                    cleared += 1
                    log.info(
                        "Form D: cleared stale block for %s (no longer matches)",
                        firm["id"],
                    )
                continue
            hits += 1
            firm["form_d_total_filings"] = info.total_filings
            firm["form_d_latest_filing_date"] = info.latest_filing_date
            firm["form_d_distinct_funds"] = info.distinct_funds
            firm["form_d_fund_ciks"] = info.fund_ciks
            firm["form_d_recent_filings"] = [
                {
                    "accession": f.accession,
                    "file_date": f.file_date,
                    "form": f.form,
                    "cik": f.cik,
                    "filer_name": f.filer_name,
                }
                for f in info.recent_filings
            ]
        log.info("Form D: matched %d / %d firms (cleared %d stale)",
                 hits, len(firms), cleared)
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


def enrich_websites(firms: list[dict]) -> None:
    """E-lite: scrape the firm's own website (home + /team + /about etc)
    and extract structured partners / sectors / stages via Vertex AI.

    Targets only lite firms that still have no Tier-D LLM data AND have
    a real (non-social) website. Result is merged through the same
    confidence gate as the LLM pass, so it cleanly augments rather than
    replaces. Resumable: every page-set + Gemini call is cached at
    ``data/.website-enrich-cache.json``.
    """
    enricher = WebsiteEnricher(cache_path=WEBSITE_CACHE)
    try:
        candidates = [
            f for f in firms
            if f.get("tier") == "lite"
            and not f.get("llm_enriched")
            and f.get("website")
        ]
        log.info("Website enrich: %d candidate firms", len(candidates))
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
                # Distinguish website-scrape provenance from Tier-D so the
                # UI can label it differently if it ever wants to.
                firm["website_enriched"] = True
            else:
                skipped_low_conf += 1
        log.info(
            "Website enrich: %d firms got website-sourced data; %d below confidence threshold",
            merged, skipped_low_conf,
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


def load_previous(path: Path | None = None) -> dict:
    """Read the previously-built payload, or ``{}`` if there isn't a usable one.

    ``path`` resolves at call time rather than as a default-argument binding,
    so tests (and any caller) can repoint ``OUT_PATH``.
    """
    path = path if path is not None else OUT_PATH
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning("Previous %s unreadable; building without carry-forward", path)
        return {}
    return payload if isinstance(payload, dict) else {}


def carry_forward_enrichment(firms: list[dict], previous: dict) -> int:
    """Re-attach enrichment the current build had no way to reproduce.

    Every enricher in this pipeline is strictly additive, and each is opt-in
    behind ``--enrich``. That combination means a partial build silently
    *drops* whatever the passes it didn't run had previously contributed —
    so the monthly ``--enrich sec_bulk`` cron rewrote firms.json without any
    of the GLM tags, theses, Form D data or Wikipedia backfill.

    Carrying those fields forward keeps a partial refresh partial: the SEC
    columns get the new scrape's values, and everything else survives.
    Fields the current build *did* populate always win, so a real re-run of
    an enricher still overwrites — this only fills genuine gaps.

    Returns the number of firms that received at least one field.
    """
    prior = previous.get("firms") or []
    if not prior:
        return 0

    by_id = {f["id"]: f for f in prior if f.get("id")}
    by_crd = {str(f["sec_crd"]): f for f in prior if f.get("sec_crd")}

    touched = 0
    for firm in firms:
        old = by_id.get(firm.get("id")) or by_crd.get(str(firm.get("sec_crd") or ""))
        if not old:
            continue
        restored = False
        for field in PRESERVED_FIELDS:
            # `not firm.get(...)` on purpose: an empty list or empty string is
            # as much a gap as a missing key, and no preserved field carries a
            # meaningful falsy value.
            if not firm.get(field) and old.get(field):
                firm[field] = old[field]
                restored = True
        if restored:
            touched += 1
    if touched:
        log.info("Carried forward enrichment for %d / %d firms", touched, len(firms))
    return touched


def build(
    enrichers: Iterable[str],
    only_firm: str | None,
    preserve: bool = True,
) -> dict:
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
    # NVCA flags membership and backfills website/sectors *before* the LLM
    # pass, so LLM credits aren't spent on fields a free, authoritative
    # source already covered. Runs after sec_bulk/wikipedia so the full
    # merged firm list (seed + scraped) gets flagged in one shot.
    if "nvca" in enricher_set:
        enrich_nvca(firms)
    # Form D runs before the LLM pass so the LLM prompt could in
    # principle reference recent fund-raising activity (though today it
    # doesn't). It's also a free, authoritative source — same tier as
    # NVCA — so it goes ahead of the LLM by the same logic.
    if "form_d" in enricher_set:
        enrich_form_d(firms)
        # Mark firms whose filings are third parties' raises, so the UI
        # doesn't read their volume as "deploying". Reports unlisted
        # high-volume filers rather than guessing at them.
        annotate_platforms(firms)
        platform_candidates(firms)
    # LLM runs LAST so it only fills gaps the earlier (free, reliable) sources
    # couldn't. Rich firms (seed YAML) are skipped — they already have
    # hand-curated data we trust more than any model output.
    if "llm" in enricher_set:
        enrich_llm(firms)
    # Website scrape (E-lite): last-mile pass for the ~24 lite firms that
    # still have no partners / sectors / stages AND have a real (non-
    # social) website. Reads the firm's own /team /about pages and asks
    # Vertex AI to extract structured data from the supplied text (no
    # Google Search grounding). Same merge gate as the LLM pass.
    if "website" in enricher_set:
        enrich_websites(firms)

    # Re-attach anything the enrichers we *didn't* run had contributed to the
    # previous build. Must come last, so a pass that did run always wins.
    manifest = set(enricher_set)
    if preserve and not only_firm:
        previous = load_previous()
        if carry_forward_enrichment(firms, previous):
            # The payload now contains those enrichers' output, so the manifest
            # has to say so — otherwise it under-reports provenance and the next
            # build can't tell what is actually in the file.
            manifest |= set(previous.get("generated_with_enrichers") or [])

    output = {
        "generated_with_enrichers": sorted(manifest),
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
    parser.add_argument(
        "--no-preserve",
        action="store_true",
        help="Discard enrichment from the previous firms.json instead of "
             "carrying it forward. Rebuilds exactly what this run produces.",
    )
    args = parser.parse_args()

    enrichers = [e.strip() for e in args.enrich.split(",") if e.strip()]
    unknown = set(enrichers) - VALID_ENRICHERS
    if unknown:
        raise SystemExit(f"Unknown enrichers: {sorted(unknown)}")

    payload = build(enrichers, args.firm, preserve=not args.no_preserve)
    write_outputs(payload)
    log.info(
        "Wrote %s firms → %s (and copy at %s)",
        payload["firm_count"],
        OUT_PATH,
        SITE_OUT_PATH,
    )


if __name__ == "__main__":
    main()
