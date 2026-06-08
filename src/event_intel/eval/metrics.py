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

from dataclasses import dataclass

_S_A = ("S", "A")

# Benchmark metric status (Y1 CS6). Distinguishes "can't measure" from "too few".
OK = "ok"
NA = "n/a"                 # class/cohort absent — undefined, not a failure
INSUFFICIENT_N = "insufficient_n"  # present but too small to gate on (R3-5/Q1)


@dataclass
class MetricResult:
    """A benchmark metric value plus an eligibility status. `value` is None unless
    status == OK. `n` is the denominator size (for audit / gate decisions).
    """
    value: float | None
    status: str
    n: int = 0


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

    Competitor is a NEGATIVE class only in customer mode (partner = neutral,
    ecosystem = positive — see the harness mode-policy table), so the harness
    asserts this only for customer. Returns None if no competitors.
    """
    comps = [n for n, lab in labels.items() if lab == "competitor"]
    if not comps:
        return None
    leaked = sum(1 for n in comps if tiers.get(n) in _S_A)
    return leaked / len(comps)


def bad_fit_leakage_rate(
    tiers: dict[str, str], labels: dict[str, str]
) -> float | None:
    """Fraction of bad_fit-labeled companies that leaked into S/A.

    bad_fit is a NEGATIVE class in EVERY mode (a company the product card declares
    unfit shouldn't reach S/A regardless of target_mode), so this is kept separate
    from competitor leakage — merging the two into one denominator would let a
    glut of bad_fit dilute a real competitor leak (review r2 #5). Returns None if
    there are no bad_fit rows.
    """
    bad = [n for n, lab in labels.items() if lab == "bad_fit"]
    if not bad:
        return None
    leaked = sum(1 for n in bad if tiers.get(n) in _S_A)
    return leaked / len(bad)


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


# ============================================================================
# Y1 real-data benchmark metrics (CS6). These take roster_id-keyed inputs after
# the CS2 roster↔run join (the 9-cell synthetic functions above stay name-keyed
# and unchanged — they are a separate scoring regression). All return MetricResult
# so the caller distinguishes ok / n/a / insufficient_n rather than silently 0.
# ============================================================================


def precision_at_k(
    scored: list[tuple[str, float]],
    labels: dict[str, str],
    positive: set[str],
    *,
    k: int = 10,
) -> MetricResult:
    """Fixed-denominator P@k: positives in the top-k / **k** (empty slots = miss).

    Unlike `precision_at_10` (synthetic 9-cell, divides by len(top-k)), the fixed
    denominator means an under-extracting run can't inflate precision by surfacing
    fewer than k companies (review R1#3).
    """
    if k <= 0:
        return MetricResult(None, NA)
    ranked = sorted(scored, key=lambda x: -x[1])[:k]
    hits = sum(1 for n, _ in ranked if labels.get(n) in positive)
    return MetricResult(hits / k, OK, k)


def _members(labels: dict[str, str], klass: str) -> list[str]:
    return [n for n, lab in labels.items() if lab == klass]


def end_to_end_selection_rate(
    tiers: dict[str, str], labels: dict[str, str], *, klass: str
) -> MetricResult:
    """leaked(S/A) / **labeled class total** (diagnostic). Definitionally lowered
    by un-extracted members — pair with coverage; gate on conditional_* instead.
    """
    members = _members(labels, klass)
    if not members:
        return MetricResult(None, NA)
    leaked = sum(1 for n in members if tiers.get(n) in _S_A)
    return MetricResult(leaked / len(members), OK, len(members))


def conditional_leakage_rate(
    tiers: dict[str, str],
    labels: dict[str, str],
    scored_ids: set[str],
    *,
    klass: str,
    min_n: int = 5,
) -> MetricResult:
    """leaked(S/A) / **scored class members** (the GATE metric). Un-extracted
    members are out of the denominator, so extraction failure can't game it
    (review R1#4 / R2). `insufficient_n` when fewer than `min_n` were scored.
    """
    members = _members(labels, klass)
    if not members:
        return MetricResult(None, NA)
    scored_members = [n for n in members if n in scored_ids]
    if len(scored_members) < min_n:
        return MetricResult(None, INSUFFICIENT_N, len(scored_members))
    leaked = sum(1 for n in scored_members if tiers.get(n) in _S_A)
    return MetricResult(leaked / len(scored_members), OK, len(scored_members))


def class_extraction_coverage(
    labels: dict[str, str], scored_ids: set[str], *, klass: str
) -> MetricResult:
    """scored class members / labeled class total — exposes the dilution that
    makes a low-coverage run look safe on end_to_end (R1#4).
    """
    members = _members(labels, klass)
    if not members:
        return MetricResult(None, NA)
    covered = sum(1 for n in members if n in scored_ids)
    return MetricResult(covered / len(members), OK, len(members))


def extraction_coverage(
    matched_ids: set[str], roster_ids: set[str]
) -> MetricResult:
    """Overall |matched ∩ roster| / |roster|. Inputs are roster_id sets — only
    *materialized* (scored) entities count as matched (R3-4).
    """
    roster = set(roster_ids)
    if not roster:
        return MetricResult(None, NA)
    matched = set(matched_ids) & roster
    return MetricResult(len(matched) / len(roster), OK, len(roster))


def mention_coverage(
    materialized_ids: set[str], mentioned_ids: set[str], roster_ids: set[str]
) -> MetricResult:
    """(materialized ∪ mention-only) / roster — weaker diagnostic kept SEPARATE
    from extraction_coverage so 1:N booth mentions don't inflate the real
    extraction number (R3-4).
    """
    roster = set(roster_ids)
    if not roster:
        return MetricResult(None, NA)
    covered = (set(materialized_ids) | set(mentioned_ids)) & roster
    return MetricResult(len(covered) / len(roster), OK, len(roster))


_EVIDENCE_VERDICTS = ("correct", "wrong-company", "wrong-type", "stale")


def evidence_precision(verdicts: list[str], *, min_items: int = 3) -> MetricResult:
    """correct / total evidence items. `insufficient_n` when fewer than
    `min_items` — so a single correct item can't pass the gate at 1.0 (R3-5).
    """
    total = len(verdicts)
    if total == 0:
        return MetricResult(None, NA)
    if total < min_items:
        return MetricResult(None, INSUFFICIENT_N, total)
    correct = sum(1 for v in verdicts if v == "correct")
    return MetricResult(correct / total, OK, total)


def evidence_yield(has_evidence: list[bool]) -> MetricResult:
    """Fraction of the cohort with >=1 credited evidence item — pairs with
    evidence_precision to expose the '1 correct item = 1.0' gaming (R3-5).
    """
    n = len(has_evidence)
    if n == 0:
        return MetricResult(None, NA)
    return MetricResult(sum(1 for f in has_evidence if f) / n, OK, n)
