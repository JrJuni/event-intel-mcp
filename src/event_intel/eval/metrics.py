"""Eval metrics — pure functions over (scored rows, gold labels).

All metrics are stdlib-only and side-effect free so the eval package stays
import-cold (regression-guarded by tests/test_mcp_cold_start.py).

Vocabulary:
    scored        list[(name, final_score)] — one per company
    tiers         dict[name -> "S"|"A"|"B"|"C"]
    labels        dict[name -> "target"|"competitor"|"bad_fit"|"neutral"]
    positive      set of labels treated as positive (mode-aware, see harness)
    present_ev    dict[name -> set(evidence_type)]   credited by the engine
    expected_ev   dict[name -> set(evidence_type)]   gold

A metric returns `None` when it is undefined for the cell (e.g. AUC needs at
least one target AND one bad_fit). Callers skip `None` rather than treating it
as 0 — a silently-zero metric reads as "failed" when it is really "N/A".
"""
from __future__ import annotations

_S_A = ("S", "A")


def ranking_accuracy_auc(
    scored: list[tuple[str, float]], labels: dict[str, str]
) -> float | None:
    """Normalized pairwise ranking accuracy of target vs bad_fit.

    Fraction of (target, bad_fit) pairs where the target outscores the bad_fit
    (ties count 0.5). Equivalent to ROC-AUC with target=positive, bad_fit=negative.
    Size-invariant — replaces the Phase-18U absolute median-rank gap, which was
    not comparable across cells of different size.

    Returns None if either class is empty.
    """
    pos = [s for n, s in scored if labels.get(n) == "target"]
    neg = [s for n, s in scored if labels.get(n) == "bad_fit"]
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for q in neg:
            if p > q:
                wins += 1.0
            elif p == q:
                wins += 0.5
    return wins / (len(pos) * len(neg))


def precision_at_10(
    scored: list[tuple[str, float]], labels: dict[str, str], positive: set[str]
) -> float | None:
    """Fraction of the top-10 (by final_score) whose label is in `positive`.

    `positive` is mode-aware (customer → {target}; partner/ecosystem widen it),
    so the same metric means "good BD picks" under every target_mode.
    Returns None for an empty cell.
    """
    ranked = sorted(scored, key=lambda x: -x[1])[:10]
    if not ranked:
        return None
    hits = sum(1 for n, _ in ranked if labels.get(n) in positive)
    return hits / len(ranked)


def competitor_leakage_rate(
    tiers: dict[str, str], labels: dict[str, str]
) -> float | None:
    """Fraction of competitor-labeled companies that leaked into S/A.

    Customer-mode guard (target_mode partner/ecosystem redefine the expectation
    — the harness selects whether to assert this). Returns None if no competitors.
    """
    comps = [n for n, lab in labels.items() if lab == "competitor"]
    if not comps:
        return None
    leaked = sum(1 for n in comps if tiers.get(n) in _S_A)
    return leaked / len(comps)


def evidence_false_positive_rate(
    present_ev: dict[str, set[str]], expected_ev: dict[str, set[str]]
) -> float:
    """Fraction of credited evidence types that the gold says are absent.

    Guards item-1 evidence expansion against inflating the floor with evidence
    types the company does not actually have. Denominator is total credited
    types across all companies; 0.0 when nothing was credited.
    """
    total = 0
    fp = 0
    for name, present in present_ev.items():
        expected = expected_ev.get(name, set())
        for t in present:
            total += 1
            if t not in expected:
                fp += 1
    return fp / total if total else 0.0
