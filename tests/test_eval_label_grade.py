"""Y1 L2 — grade flows sheet → SealedLabels → measure; holdout rejects non-gold."""
from __future__ import annotations

import pytest

from event_intel.eval import benchmark as B
from event_intel.eval import blind as BL
from event_intel.eval import labeling as L
from event_intel.eval import roster as R

# ---------- flag_for_review ----------

def _drafted(name, sug, conf, needs=False):
    return {"index": 0, "name": name, "overview": "o", "url": None, "label": "",
            "suggested_label": sug, "confidence": conf, "rationale": "r",
            "source": "gpt_draft", "needs_review": needs}


def test_flag_auto_accepts_confident_non_gate_class_as_silver():
    [row] = L.flag_for_review([_drafted("Acme", "target", 0.9)])
    assert row["needs_review"] is False
    assert row["final_label"] == "target" and row["grade"] == "silver"


def test_flag_gate_class_always_reviewed():
    [row] = L.flag_for_review([_drafted("Rival", "competitor", 0.99)])
    assert row["needs_review"] is True
    assert row["final_label"] == "" and row["grade"] == ""  # awaits gold refine


def test_flag_low_confidence_reviewed():
    [row] = L.flag_for_review([_drafted("Acme", "target", 0.5)], min_confidence=0.7)
    assert row["needs_review"] is True


def test_flag_carries_draft_failure():
    [row] = L.flag_for_review([_drafted("Acme", "", 0.0, needs=True)])
    assert row["needs_review"] is True


# ---------- extract_sealed_inputs ----------

def test_extract_uses_final_label_and_grade():
    rows = [
        {"name": "Acme", "final_label": "target", "grade": "silver", "source": "gpt_draft"},
        {"name": "Rival", "final_label": "competitor", "grade": "gold",
         "source": "search_refine", "adjudicators": ["claude"]},
    ]
    labels, grades, prov = L.extract_sealed_inputs(rows)
    assert labels == {"Acme": "target", "Rival": "competitor"}
    assert grades == {"Acme": "silver", "Rival": "gold"}
    assert prov["Rival"]["source"] == "search_refine" and prov["Rival"]["adjudicators"] == ["claude"]


def test_extract_rejects_blank_unless_partial():
    rows = [{"name": "A", "final_label": "target", "grade": "silver"},
            {"name": "B", "final_label": ""}]
    with pytest.raises(ValueError, match="unlabeled"):
        L.extract_sealed_inputs(rows)
    labels, _, _ = L.extract_sealed_inputs(rows, require_all=False)
    assert labels == {"A": "target"}


# ---------- SealedLabels carries grade + serialization back-compat ----------

def _packet(names):
    return BL.CompanyPacket(pair="p", cohort=BL.FULL, seed=0,
                            entries=[{"index": i, "name": n} for i, n in enumerate(names)])


def test_seal_preserves_grade_and_roundtrips():
    pkt = _packet(["Acme", "Rival"])
    sealed = BL.seal_company_labels(
        pkt, {"Acme": "target", "Rival": "competitor"},
        grades={"Acme": "silver", "Rival": "gold"},
        provenance={"Rival": {"source": "search_refine", "adjudicators": ["claude"]}},
    )
    assert sealed.grades["Rival"] == "gold"
    d = BL.sealed_labels_to_dict(sealed)
    back = BL.sealed_labels_from_dict(d)
    assert back.grades == sealed.grades and back.provenance == sealed.provenance


def test_old_sealed_without_grades_reads_back_compat():
    # a pre-L2 sealed file (no grades/provenance keys)
    d = {"pair": "p", "labels": {"Acme": "target"}, "sha": "x", "packet_sha": "y"}
    back = BL.sealed_labels_from_dict(d)
    assert back.labels == {"Acme": "target"} and back.grades == {}


# ---------- measure(holdout=True) rejects non-gold ----------

_ROSTER = [R.RosterEntry("r1", "Acme", label="target"),
           R.RosterEntry("r2", "Rival", label="competitor")]


def _rr():
    return B._run_result_from_payload("p", {
        "run_id": "p-1", "run_fingerprint": "fp",
        "companies": [{"name": "Acme", "tier": "A", "final_score": 7.0},
                      {"name": "Rival", "tier": "S", "final_score": 9.0}],
    })


def test_holdout_rejects_silver_or_ungraded():
    pkt = _packet(["Acme", "Rival"])
    sealed = BL.seal_company_labels(
        pkt, {"Acme": "target", "Rival": "competitor"},
        grades={"Acme": "gold", "Rival": "silver"},  # one silver
    )
    match = R.match_roster(["Acme", "Rival"], _ROSTER)
    with pytest.raises(ValueError, match="all-gold"):
        B.measure(run_result=_rr(), roster=_ROSTER, match=match,
                  sealed_labels=sealed, holdout=True)


def test_holdout_passes_when_all_gold():
    pkt = _packet(["Acme", "Rival"])
    sealed = BL.seal_company_labels(
        pkt, {"Acme": "target", "Rival": "competitor"},
        grades={"Acme": "gold", "Rival": "gold"},
    )
    match = R.match_roster(["Acme", "Rival"], _ROSTER)
    rep = B.measure(run_result=_rr(), roster=_ROSTER, match=match,
                    sealed_labels=sealed, holdout=True)  # no raise
    assert rep.pair == "p"


def test_dev_measure_allows_silver():
    pkt = _packet(["Acme", "Rival"])
    sealed = BL.seal_company_labels(pkt, {"Acme": "target"}, grades={"Acme": "silver"})
    match = R.match_roster(["Acme"], _ROSTER)
    rep = B.measure(run_result=_rr(), roster=_ROSTER, match=match,
                    sealed_labels=sealed, holdout=False)  # silver ok in DEV
    assert rep.pair == "p"
