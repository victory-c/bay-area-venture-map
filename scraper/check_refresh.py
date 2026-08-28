"""Guard a refreshed ``firms.json`` against silent data loss.

The monthly cron used to check one thing — ``firm_count >= 100`` — which
could never catch the failure that actually happened. When commit 6692ed9
rebuilt the file without the optional enrichers it dropped 98,532 lines of
enrichment while the firm count barely moved: the *rows* survived, the
*columns* did not.

So this compares per-field coverage against the previously committed
payload and fails when any tracked field loses more than ``TOLERANCE`` of
its records. Coverage legitimately wobbles a little month to month (a firm
deregisters, the SEC renames an entity), hence a tolerance rather than an
exact floor.

Usage:
    python -m scraper.check_refresh NEW.json PREVIOUS.json
"""
from __future__ import annotations

import json
import pathlib
import sys

MIN_FIRMS = 100

#: Fraction of a field's previous coverage that may disappear before we
#: treat it as data loss rather than churn.
TOLERANCE = 0.10

#: Fields whose disappearance means an enricher's output was dropped.
TRACKED_FIELDS = (
    "sectors",
    "stages",
    "inferred",
    "inferred_thesis",
    "inference_source",
    "inference_evidence",
    "thesis_source",
    "form_d_total_filings",
    "wikipedia_url",
    "nvca_member",
    "llm_enriched",
    "website_enriched",
    "lat",
)


def coverage(firms: list[dict]) -> dict[str, int]:
    return {f: sum(1 for firm in firms if firm.get(f)) for f in TRACKED_FIELDS}


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    new = json.loads(pathlib.Path(sys.argv[1]).read_text())
    new_firms = new.get("firms") or []

    print(f"firm_count: {len(new_firms)}")
    if len(new_firms) < MIN_FIRMS:
        print(f"FAIL: only {len(new_firms)} firms (min {MIN_FIRMS}) — refusing to commit")
        return 1

    prev_path = pathlib.Path(sys.argv[2])
    if not prev_path.exists():
        print("No previous payload to compare against; skipping coverage check.")
        return 0
    try:
        prev_firms = json.loads(prev_path.read_text()).get("firms") or []
    except json.JSONDecodeError:
        print("Previous payload unreadable; skipping coverage check.")
        return 0

    before, after = coverage(prev_firms), coverage(new_firms)
    failures: list[str] = []
    print(f"\n{'field':24} {'before':>8} {'after':>8}  status")
    for field in TRACKED_FIELDS:
        b, a = before[field], after[field]
        floor = int(b * (1 - TOLERANCE))
        if a < floor:
            status = f"LOST {b - a} (floor {floor})"
            failures.append(f"{field}: {b} -> {a}")
        else:
            status = "ok"
        print(f"{field:24} {b:>8} {a:>8}  {status}")

    if failures:
        print(
            "\nFAIL: enrichment coverage collapsed — refusing to commit.\n"
            "  " + "\n  ".join(failures) + "\n\n"
            "A partial build should carry these forward (see "
            "scraper.build.carry_forward_enrichment). If the drop is genuine "
            "and intended, re-run with --no-preserve and update this check."
        )
        return 1

    print("\nCoverage check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
