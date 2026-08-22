"""The refresh gate must catch column loss, not just row loss."""
from __future__ import annotations

import json
import sys

from scraper.check_refresh import TOLERANCE, coverage, main


def _payload(n_firms: int, *, enriched: bool) -> dict:
    firms = []
    for i in range(n_firms):
        f = {"id": f"sec-{i}", "name": f"Firm {i}", "lat": 37.4, "lng": -122.2}
        if enriched:
            f |= {"sectors": ["fintech"], "stages": ["seed"], "inferred": True,
                  "inferred_thesis": "Backs fintech.", "form_d_total_filings": 3}
        firms.append(f)
    return {"firm_count": n_firms, "firms": firms}


def _run(tmp_path, new: dict, prev: dict | None, monkeypatch) -> int:
    """Invoke the CLI the way the workflow does, returning its exit code."""
    new_path = tmp_path / "new.json"
    new_path.write_text(json.dumps(new))
    prev_path = tmp_path / "prev.json"
    if prev is not None:
        prev_path.write_text(json.dumps(prev))
    monkeypatch.setattr(sys, "argv", ["check_refresh", str(new_path), str(prev_path)])
    return main()


def test_passes_when_coverage_is_unchanged(tmp_path, monkeypatch) -> None:
    p = _payload(200, enriched=True)
    assert _run(tmp_path, p, p, monkeypatch) == 0


def test_fails_when_enrichment_columns_vanish(tmp_path, monkeypatch) -> None:
    # The 2026-06-05 shape: rows intact, columns gone.
    before = _payload(200, enriched=True)
    after = _payload(200, enriched=False)
    assert _run(tmp_path, after, before, monkeypatch) == 1


def test_fails_when_the_firm_count_collapses(tmp_path, monkeypatch) -> None:
    assert _run(tmp_path, _payload(5, enriched=True), _payload(200, enriched=True), monkeypatch) == 1


def test_tolerates_small_month_to_month_churn(tmp_path, monkeypatch) -> None:
    before = _payload(200, enriched=True)
    after = _payload(200, enriched=True)
    # Drop just under the tolerance: a few firms legitimately lose a tag.
    for f in after["firms"][: int(200 * TOLERANCE) - 1]:
        del f["sectors"]
    assert _run(tmp_path, after, before, monkeypatch) == 0


def test_skips_comparison_when_there_is_no_previous_payload(tmp_path, monkeypatch) -> None:
    assert _run(tmp_path, _payload(200, enriched=False), None, monkeypatch) == 0


def test_coverage_counts_only_populated_fields() -> None:
    firms = [{"sectors": ["a"]}, {"sectors": []}, {}]
    assert coverage(firms)["sectors"] == 1
