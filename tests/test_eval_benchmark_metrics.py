"""Y1 CS6 — real-data benchmark metrics: fixed-denominator P@k, 3-split leakage,
class coverage, evidence precision abstention. Adversarial cases from plan v4."""
from __future__ import annotations

from event_intel.eval import metrics as m

# ---------- P@k fixed denominator (R1#3) ----------

def test_precision_at_k_uses_fixed_k_not_count():
    """4 results, all target → 0.4 (not 1.0): under-extraction can't inflate."""
    scored = [(f"c{i}", 1.0) for i in range(4)]
    labels = {f"c{i}": "target" for i in range(4)}
    r = m.precision_at_k(scored, labels, {"target"}, k=10)
    assert r.status == m.OK and r.value == 0.4


def test_precision_at_k_full_top10():
    scored = [(f"c{i}", float(10 - i)) for i in range(12)]
    labels = {f"c{i}": ("target" if i < 6 else "neutral") for i in range(12)}
    r = m.precision_at_k(scored, labels, {"target"}, k=10)
    assert r.value == 0.6  # 6 targets in top-10 / 10


# ---------- 3-split leakage (R1#4 / R2) ----------

def _leak_setup(n_competitors, n_scored, n_leaked):
    labels = {f"k{i}": "competitor" for i in range(n_competitors)}
    scored_ids = {f"k{i}" for i in range(n_scored)}
    tiers = {f"k{i}": ("A" if i < n_leaked else "B") for i in range(n_scored)}
    return tiers, labels, scored_ids


def test_conditional_not_diluted_by_unextracted_but_end_to_end_is():
    """The fix for the round-1/round-2 contradiction: un-extracted competitors
    leave conditional_leakage unchanged but lower end_to_end_selection."""
    # 10 competitors labeled, 5 scored, 1 of the scored leaked into A.
    tiers, labels, scored = _leak_setup(10, 5, 1)
    cond = m.conditional_leakage_rate(tiers, labels, scored, klass="competitor", min_n=5)
    e2e = m.end_to_end_selection_rate(tiers, labels, klass="competitor")
    assert cond.value == 0.2          # 1/5 scored
    assert e2e.value == 0.1           # 1/10 labeled

    # Add 100 un-extracted competitors → conditional UNCHANGED, end_to_end drops.
    for i in range(100):
        labels[f"x{i}"] = "competitor"
    cond2 = m.conditional_leakage_rate(tiers, labels, scored, klass="competitor", min_n=5)
    e2e2 = m.end_to_end_selection_rate(tiers, labels, klass="competitor")
    assert cond2.value == 0.2                 # not diluted
    assert e2e2.value < e2e.value             # diluted by definition (1/110)


def test_conditional_insufficient_n():
    tiers, labels, scored = _leak_setup(10, 3, 1)  # only 3 scored < min_n 5
    r = m.conditional_leakage_rate(tiers, labels, scored, klass="competitor", min_n=5)
    assert r.status == m.INSUFFICIENT_N and r.value is None and r.n == 3


def test_leakage_na_when_class_absent():
    """partner pair: no competitor labels → N/A (P6 competitor gate skipped)."""
    labels = {"a": "target", "b": "bad_fit"}
    assert m.conditional_leakage_rate({}, labels, set(), klass="competitor").status == m.NA
    assert m.end_to_end_selection_rate({}, labels, klass="competitor").status == m.NA


def test_class_extraction_coverage():
    _, labels, scored = _leak_setup(10, 4, 0)
    r = m.class_extraction_coverage(labels, scored, klass="competitor")
    assert r.value == 0.4 and r.n == 10


# ---------- overall + mention coverage (R3-4) ----------

def test_extraction_coverage_materialized_only():
    roster = {f"r{i}" for i in range(10)}
    matched = {"r0", "r1", "r2", "r3"}  # 4 materialized scoring entities
    assert m.extraction_coverage(matched, roster).value == 0.4


def test_mention_coverage_separate_from_extraction():
    """1:N booth mentions count for mention_coverage but NOT extraction_coverage."""
    roster = {f"r{i}" for i in range(10)}
    materialized = {"r0", "r1"}
    mentioned = {"r2", "r3", "r4"}  # named in a booth but not scored
    assert m.extraction_coverage(materialized, roster).value == 0.2
    assert m.mention_coverage(materialized, mentioned, roster).value == 0.5


# ---------- evidence precision abstention (R3-5) ----------

def test_evidence_precision_single_item_insufficient_n():
    """1 correct item must NOT pass as precision 1.0."""
    r = m.evidence_precision(["correct"], min_items=3)
    assert r.status == m.INSUFFICIENT_N and r.value is None


def test_evidence_precision_enough_items():
    r = m.evidence_precision(["correct", "correct", "wrong-type", "stale"], min_items=3)
    assert r.status == m.OK and r.value == 0.5


def test_evidence_precision_zero_is_na():
    assert m.evidence_precision([], min_items=3).status == m.NA


def test_evidence_yield():
    assert m.evidence_yield([True, True, False, False]).value == 0.5
    assert m.evidence_yield([]).status == m.NA
