"""Firms whose Form D filings are other people's raises.

The problem
-----------
``form_d_latest_filing_date`` is presented to founders as a "this firm is
actively deploying" signal. That reading holds for a fund — a Form D means
it closed capital — but not for an SPV or crowdfunding platform, where each
filing is a *third party's* deal administered on the firm's infrastructure.

After the matcher fix the two largest filers in the dataset became Vauban
(289) and FundersClub (284), ahead of Sequoia (217). Those counts are
correct, and they mean something completely different.

Why this is a curated list and not a heuristic
----------------------------------------------
Nothing in the data separates the two classes safely:

  * **Volume doesn't.** Sequoia 217, Accel 104 and Eclipse 104 are genuine
    deployers; any threshold low enough to catch Wefunder (27) sweeps up
    most of the real funds.
  * **SPV-shaped names don't.** "Khosla Ventures MM SPV, LLC" and
    "Eclipse SPV XXXV, L.P." are ordinary VCs doing deal-by-deal vehicles.
  * **The two platform signatures don't even resemble each other.** Vauban
    files third-party names against one master LP ("StoneAX Ventures, a
    Series of Vauban Platform LP"); FundersClub files opaque per-deal
    entities ("FundersClub K8A LLC").

A name-substring rule is exactly the mistake that gave Founders Fund
fifteen other firms' filings — searching "forge" for platform candidates
already surfaces Forgepoint Capital, a genuine cybersecurity VC. So
membership is explicit and keyed by CRD, which is stable across renames,
and :func:`platform_candidates` only *reports* firms worth a human look.

Deliberately not listed
-----------------------
``Allocate Management`` (CRD 316814) files its own access and co-investment
vehicles, so "recently raised" is true of it. It is LP-side rather than a
startup investor, which is a different question from the one this module
answers, and is left alone here.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

#: CRD -> what that firm's Form D filings actually represent. Keyed by CRD
#: because names change and substring matching over names is unsafe.
PLATFORM_FIRMS: dict[str, str] = {
    "319358": (  # Vauban Advisers LLC
        "SPV administration platform. These filings are third-party syndicates "
        "raised as series of Vauban Platform LP, not Vauban investing."
    ),
    "166518": (  # FundersClub Inc.
        "Investment platform. Each filing is a separate per-deal vehicle raised "
        "from the platform's members, not a fund FundersClub deployed."
    ),
    "167803": (  # Wefunder
        "Regulation Crowdfunding portal. These filings are raises by companies "
        "and syndicates hosted on Wefunder, not Wefunder investing."
    ),
}

#: Enough filings to be worth a look. Set well above a normal fund's cadence
#: so ``platform_candidates`` stays quiet in routine runs.
CANDIDATE_MIN_FILINGS = 60

_SERIES_RE = re.compile(r"\bseries of\b", re.I)


def annotate_platforms(firms: list[dict]) -> int:
    """Tag known platforms in place. Returns the number tagged.

    Sets ``firm_role: "platform"`` and ``platform_note``. Firms that are not
    on the list have both keys removed, so delisting a firm actually takes
    effect on the next run rather than lingering in the payload.
    """
    tagged = 0
    for firm in firms:
        crd = str(firm.get("sec_crd") or "")
        note = PLATFORM_FIRMS.get(crd)
        if note:
            firm["firm_role"] = "platform"
            firm["platform_note"] = note
            tagged += 1
        elif firm.get("firm_role") == "platform":
            firm.pop("firm_role", None)
            firm.pop("platform_note", None)
    log.info("Platforms: tagged %d / %d firms", tagged, len(firms))
    return tagged


def platform_candidates(firms: list[dict]) -> list[tuple[str, str, int]]:
    """High-volume filers not already listed, for human review.

    Reports rather than tags: the point is that a new platform entering the
    dataset gets noticed, without guessing at membership. Returns
    ``(name, crd, filings)`` sorted by volume.
    """
    out = []
    for firm in firms:
        crd = str(firm.get("sec_crd") or "")
        count = firm.get("form_d_total_filings") or 0
        if crd in PLATFORM_FIRMS or count < CANDIDATE_MIN_FILINGS:
            continue
        names = firm.get("form_d_distinct_funds") or []
        # A platform's vehicles are overwhelmingly series of one master.
        if names and sum(1 for n in names if _SERIES_RE.search(n)) >= len(names) * 0.8:
            out.append((firm["name"], crd, count))
    out.sort(key=lambda r: -r[2])
    for name, crd, count in out:
        log.warning(
            "Possible platform, not currently listed: %s (CRD %s, %d filings) "
            "— review and add to scraper.platforms.PLATFORM_FIRMS if so.",
            name, crd, count,
        )
    return out
