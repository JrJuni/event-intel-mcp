"""Gold promotion — Y1 L3 / Stage C.

Two ways a flagged (or any) row becomes GOLD, both producing auditable provenance:

  cross-vendor agreement — an independent second vendor (Claude, ≠ the GPT drafter
    and ≠ the engine) labels the SAME companies from a GPT-blind view (name +
    overview only). Where the two vendors AGREE, the label is gold (two
    independent votes); where they disagree, the row is flagged for search-refine.
    Independence is *proven*: merge recomputes the SHA of the GPT-stripped view and
    refuses to promote if the caller's `independent_input_sha` doesn't match — so a
    "second opinion" that actually saw the GPT suggestion can't be passed off as
    independent (review R2#5).

  search refine — the host (Claude app / Claude Code agent) web-searches the
    flagged rows and returns {name: {final_label, evidence_urls, note}}; apply
    merges those into the flagged rows as gold (source=search_refine), with the
    evidence URLs kept as provenance.

Pure stdlib — import-cold (regression-guarded by tests/test_mcp_cold_start.py).
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from event_intel.eval.labeling import GRADE_GOLD, LABEL_VALUES


def independent_input_view(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The GPT-blind view the independent labeler must see: name + overview + url
    ONLY — never the GPT suggested_label / confidence / rationale (review R2#5).
    """
    return [
        {"name": r.get("name", ""), "overview": r.get("overview", ""), "url": r.get("url")}
        for r in rows
    ]


def input_sha(view: list[dict[str, Any]]) -> str:
    """Deterministic SHA of an input view — the independence receipt."""
    return hashlib.sha256(
        json.dumps(view, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def merge_cross_vendor(
    gpt_rows: list[dict[str, Any]],
    claude_labels: dict[str, str],
    *,
    independent_input_sha: str,
    prompt_sha: str,
    model_id: str,
) -> list[dict[str, Any]]:
    """Promote rows to gold where the GPT draft and an INDEPENDENT Claude label
    agree. Refuses unless `independent_input_sha` matches the SHA of the GPT-blind
    view of `gpt_rows` — proving the 2nd vendor didn't see the GPT suggestion.
    Agreement → grade=gold (source=cross_agree); disagreement / missing →
    needs_review (awaits search-refine). Returns NEW row dicts.
    """
    expected = input_sha(independent_input_view(gpt_rows))
    if independent_input_sha != expected:
        raise ValueError(
            "independent labeler input SHA mismatch — the 2nd vendor's view must be "
            "the GPT-blind view (name+overview+url only) to count as independent "
            "(review R2#5). Recompute via independent_input_view()."
        )
    meta = {
        "independent_input_sha": independent_input_sha,
        "prompt_sha": prompt_sha,
        "model_id": model_id,
    }
    out: list[dict[str, Any]] = []
    for r in gpt_rows:
        row = dict(r)
        gpt = row.get("suggested_label", "")
        claude = (claude_labels.get(row.get("name", "")) or "").strip()
        if gpt and gpt in LABEL_VALUES and gpt == claude:
            row["final_label"] = gpt
            row["grade"] = GRADE_GOLD
            row["source"] = "cross_agree"
            row["adjudicators"] = ["gpt_draft", "claude_independent"]
            row["independence"] = meta
            row["needs_review"] = False
        else:  # disagree or missing → flag for search-refine, NOT gold
            row["needs_review"] = True
            row["grade"] = ""
            row.setdefault("final_label", "")
        out.append(row)
    return out


def apply_refinements(
    rows: list[dict[str, Any]],
    refinements: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge host search-refine output into FLAGGED rows only (review R1#1 / R2#6).

    `refinements[name] = {"final_label", "evidence_urls"?, "note"?}`. A refined row
    becomes gold (source=search_refine) with its evidence URLs kept as provenance.
    Non-flagged rows are left untouched; an invalid label raises.
    """
    bad: list[tuple[str, str]] = []
    out: list[dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        ref = refinements.get(row.get("name", ""))
        if ref and row.get("needs_review"):
            label = str(ref.get("final_label", "")).strip()
            if label not in LABEL_VALUES:
                bad.append((row.get("name", ""), label))
            else:
                row["final_label"] = label
                row["grade"] = GRADE_GOLD
                row["source"] = "search_refine"
                row["search_evidence"] = list(ref.get("evidence_urls", []))
                row["adjudicators"] = ["claude_search"]
                if ref.get("note"):
                    row["refine_note"] = str(ref["note"])
                row["needs_review"] = False
        out.append(row)
    if bad:
        raise ValueError(f"invalid refinement labels {bad}; allowed {LABEL_VALUES}")
    return out


def label_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Labeling-process meta-metrics (Y1 L4) — "how much can we trust this gold?".

    Reports grade/source/label distributions plus the rates that matter for
    confidence: gold_rate, cross_agree_rate, flag_rate (still needs_review), and
    flip_rate (final_label changed the GPT draft — how often refine/agreement
    corrected the single-vendor guess). All derived from row provenance.
    """
    n = len(rows)

    def frac(c: int) -> float:
        return c / n if n else 0.0

    grades = Counter((r.get("grade") or "ungraded") for r in rows)
    sources = Counter((r.get("source") or "none") for r in rows)
    final_labels = Counter(r["final_label"] for r in rows if r.get("final_label"))
    flipped = sum(
        1 for r in rows
        if r.get("final_label") and r.get("suggested_label")
        and r["final_label"] != r["suggested_label"]
    )
    flagged = sum(1 for r in rows if r.get("needs_review"))
    return {
        "n": n,
        "by_grade": dict(grades),
        "by_source": dict(sources),
        "by_final_label": dict(final_labels),
        "gold_rate": frac(grades.get(GRADE_GOLD, 0)),
        "cross_agree_rate": frac(sources.get("cross_agree", 0)),
        "flag_rate": frac(flagged),
        "flip_rate": frac(flipped),
    }
