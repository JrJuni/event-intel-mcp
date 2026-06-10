"""Critique aggregation + dashboard — BD critique harness S4.

Validates host critiques (N pairs) and aggregates them into a diagnostic
dashboard: per-pair defensibility, and the human triage list (picks where the
host flagged, a MAJORITY of lenses disagreed, or the judge's blind independent
view differed from the engine). Explicitly SILVER — this is advisory DEV triage,
never a holdout/accuracy gate. NO scoring logic.

stdlib only at import (cold-start safe).
"""
from __future__ import annotations

from typing import Any

from event_intel.eval.critique_packet import EXPECTED_LENSES, parse_critique


def _triage_reasons(pick: dict[str, Any], lenses: tuple[str, ...]) -> tuple[list[str], int]:
    reasons: list[str] = []
    if pick.get("flag"):
        reasons.append("host_flag")
    disagree = sum(
        1 for lk in lenses if (pick["lenses"].get(lk) or {}).get("verdict") == "disagree"
    )
    if disagree > len(lenses) / 2:  # majority of lenses concur it's questionable
        reasons.append("majority_lens_disagree")
    if not pick["independent_first"]["would_place_sa"]:
        reasons.append("judge_would_not_place")
    return reasons, disagree


def aggregate_critiques(
    critiques: list[dict[str, Any]],
    *,
    expected_lenses: tuple[str, ...] = EXPECTED_LENSES,
    validate: bool = True,
) -> dict[str, Any]:
    """Aggregate host critiques into a silver diagnostic dashboard.

    Each critique is (re)validated against the S2 schema unless ``validate=False``.
    A pick becomes a triage candidate when the host flagged it, a majority of
    lenses disagreed, or the judge's blind view would not have placed it S/A.
    """
    pairs_out: list[dict[str, Any]] = []
    triage: list[dict[str, Any]] = []
    judges: set[str] = set()
    total_picks = total_def = 0

    for c in critiques:
        if validate:
            parse_critique(c, expected_lenses=expected_lenses)
        judges.add(str(c.get("judge_model_id", "")))
        picks = c.get("picks", []) or []
        n_def = sum(1 for p in picks if p.get("defensible"))
        n_flagged = 0
        for p in picks:
            reasons, disagree = _triage_reasons(p, expected_lenses)
            if reasons:
                n_flagged += 1
                triage.append({
                    "pair": c.get("pair"),
                    "name": p.get("name"),
                    "reasons": reasons,
                    "lens_disagree_count": disagree,
                    "host_flag": bool(p.get("flag")),
                    "would_place_sa": p["independent_first"]["would_place_sa"],
                })
        pairs_out.append({
            "pair": c.get("pair"),
            "n_picks": len(picks),
            "n_defensible": n_def,
            "defensibility_rate": round(n_def / len(picks), 4) if picks else None,
            "n_flagged": n_flagged,
        })
        total_picks += len(picks)
        total_def += n_def

    return {
        "grade": "silver",
        "advisory": (
            "DEV diagnostic — engine↔judge disagreement surfaced for human "
            "spot-check; NOT a holdout/accuracy gate"
        ),
        "n_pairs": len(critiques),
        "n_picks": total_picks,
        "n_defensible": total_def,
        "overall_defensibility_rate": (
            round(total_def / total_picks, 4) if total_picks else None
        ),
        "n_triage": len(triage),
        "pairs": pairs_out,
        "triage_candidates": triage,
        "judges": sorted(j for j in judges if j),
    }
