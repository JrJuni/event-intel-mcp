"""Y1 CS4 — benchmark run/measure split. Asserts the blind boundary is
structural: `run` carries no gold and refuses gold-tainted payloads; `measure`
is the only side that touches sealed gold, and the join is correct."""
from __future__ import annotations

import inspect
import json

import pytest

from event_intel.eval import benchmark as B
from event_intel.eval import blind as BL
from event_intel.eval import roster as R

# ---------- a small run-result payload (the engine's run_summary shape) ----------


def _payload(run_id="p1-run-aaa", **over):
    base = dict(
        run_id=run_id,
        run_fingerprint="fp-deadbeef",
        companies=[
            {"name": "ACME Robotics", "tier": "S", "final_score": 9.1},
            {"name": "Globex", "tier": "A", "final_score": 7.4},
            {"name": "Initech", "tier": "B", "final_score": 4.0},
            {"name": "RivalCorp", "tier": "S", "final_score": 8.8},
        ],
    )
    base.update(over)
    return base


_ROSTER = [
    R.RosterEntry("r1", "ACME Robotics", label="target"),
    R.RosterEntry("r2", "Globex", label="target"),
    R.RosterEntry("r3", "Initech", label="bad_fit"),
    R.RosterEntry("r4", "RivalCorp", label="competitor"),
    R.RosterEntry("r5", "GhostCo", label="competitor"),  # un-extracted competitor
]


def _match():
    return R.match_roster(["ACME Robotics", "Globex", "Initech", "RivalCorp"], _ROSTER)


def _sealed(labels=None):
    p = BL.build_company_packet(pair="p1", cohort=BL.FULL, roster=_ROSTER, seed=1)
    return BL.seal_company_labels(p, labels or {})


# ---------- run: structural gold-blindness ----------

def test_run_signature_has_no_gold_parameter():
    params = set(inspect.signature(B.run).parameters)
    assert not (params & B._GOLD_KEYS)
    assert "gold" not in params and "labels" not in params


def test_run_refuses_gold_tainted_payload():
    """If gold leaks into the run-result payload, run raises (boundary, R1#1)."""
    def build_fn():
        p = _payload()
        p["labels"] = {"ACME Robotics": "target"}  # contamination
        return p

    with pytest.raises(ValueError, match="gold"):
        B.run(pair="p1", build_fn=build_fn, runs_root="unused")


def test_run_persists_immutable_blind_run_result(tmp_path):
    run_dir = B.run(pair="p1", build_fn=_payload, runs_root=tmp_path)
    rr_file = run_dir / "run_result.json"
    assert rr_file.is_file()
    record = json.loads(rr_file.read_text(encoding="utf-8"))
    # no gold fields ever persisted
    assert not (set(record) & B._GOLD_KEYS)
    assert record["run_id"] == "p1-run-aaa"
    # immutable: a second run into the same run_id dir refuses to overwrite
    with pytest.raises(FileExistsError):
        B.run(pair="p1", build_fn=_payload, runs_root=tmp_path)


def test_run_then_load_round_trips(tmp_path):
    run_dir = B.run(pair="p1", build_fn=_payload, runs_root=tmp_path)
    rr = B.load_run_result(run_dir)
    assert rr.run_id == "p1-run-aaa" and rr.run_fingerprint == "fp-deadbeef"
    assert dict(rr.scored)["ACME Robotics"] == 9.1
    assert rr.tiers["RivalCorp"] == "S"


# ---------- measure: requires sealed gold ----------

def test_measure_rejects_unsealed_labels():
    rr = B._run_result_from_payload("p1", _payload())
    with pytest.raises(TypeError, match="SEALED"):
        B.measure(
            run_result=rr, roster=_ROSTER, match=_match(),
            sealed_labels={"ACME Robotics": "target"},  # raw dict, not sealed
        )


# ---------- measure: join correctness ----------

def test_measure_join_projects_onto_roster_and_computes_metrics():
    rr = B._run_result_from_payload("p1", _payload())
    sealed = _sealed({
        "ACME Robotics": "target", "Globex": "target",
        "Initech": "bad_fit", "RivalCorp": "competitor", "GhostCo": "competitor",
    })
    rep = B.measure(
        run_result=rr, roster=_ROSTER, match=_match(), sealed_labels=sealed,
        target_mode="customer",
    )
    m = rep.metrics
    # coverage: 4 of 5 roster materialized
    assert m["extraction_coverage"].value == 4 / 5
    # P@10: 2 targets in top-10 / 10 (fixed denom)
    assert m["precision_at_10"].value == 2 / 10
    # RivalCorp (competitor) leaked into S → end_to_end 1/2, conditional 1/1...
    # but conditional needs min_n=5 scored competitors → insufficient_n here
    assert m["end_to_end_competitor_selection_rate"].value == 1 / 2  # 1 leaked / 2 labeled
    assert m["conditional_competitor_leakage_rate"].status == "insufficient_n"
    # competitor extraction coverage: 1 scored / 2 labeled
    assert m["competitor_extraction_coverage"].value == 1 / 2
    # target extraction coverage: both targets materialized → 2/2
    assert m["target_extraction_coverage"].value == 2 / 2


def test_measure_partner_mode_competitor_is_na():
    rr = B._run_result_from_payload("p1", _payload())
    sealed = _sealed({"RivalCorp": "competitor", "ACME Robotics": "target"})
    rep = B.measure(
        run_result=rr, roster=_ROSTER, match=_match(), sealed_labels=sealed,
        target_mode="partner",
    )
    assert rep.metrics["conditional_competitor_leakage_rate"].status == "n/a"
    assert rep.metrics["end_to_end_competitor_selection_rate"].status == "n/a"


def test_measure_evidence_from_sealed_verdicts():
    rr = B._run_result_from_payload("p1", _payload())
    verdicts = BL.seal_evidence_verdicts(
        BL.EvidencePacket(pair="p1", items=[]),
        ["correct", "correct", "wrong-company"],
    )
    rep = B.measure(
        run_result=rr, roster=_ROSTER, match=_match(), sealed_labels=_sealed(),
        sealed_verdicts=verdicts,
    )
    # 3 items ≥ min_items → gated; 2/3 correct
    assert rep.metrics["evidence_precision"].value == 2 / 3
    assert rep.metrics["evidence_precision"].status == "ok"


# ---------- gates ----------

def test_gate_failure_when_coverage_below_threshold():
    rr = B._run_result_from_payload("p1", _payload())
    # only 1 of 5 extracted → coverage 0.2 < 0.80
    match = R.match_roster(["ACME Robotics"], _ROSTER)
    rep = B.measure(
        run_result=rr, roster=_ROSTER, match=match,
        sealed_labels=_sealed({"ACME Robotics": "target"}),
    )
    fails = {g.name for g in rep.gate_failures()}
    assert "extraction_coverage" in fails
    assert rep.passed() is False
    # target coverage tracks the class, not the cap: ACME scored, Globex not → 1/2
    assert rep.metrics["target_extraction_coverage"].value == 1 / 2


def test_target_coverage_gate_via_custom_manifest_gates():
    """The renegotiated D3 contract gates on target_extraction_coverage; a
    frozen manifest naming that metric must actually bind (a missing metric
    would be silently skipped by _evaluate_gates)."""
    rr = B._run_result_from_payload("p1", _payload())
    match = R.match_roster(["ACME Robotics"], _ROSTER)  # Globex (target) dropped
    gates = (
        ("target_extraction_coverage", ">=", 0.80, B.REQUIRED),
        ("extraction_coverage", ">=", 0.80, B.OPTIONAL),  # advisory under caps
    )
    rep = B.measure(
        run_result=rr, roster=_ROSTER, match=match,
        sealed_labels=_sealed({"ACME Robotics": "target", "Globex": "target"}),
        thresholds=gates,
    )
    by_name = {g.name: g for g in rep.gates}
    assert by_name["target_extraction_coverage"].passed is False
    assert by_name["target_extraction_coverage"].applicability == B.REQUIRED
    assert by_name["extraction_coverage"].applicability == B.OPTIONAL
    # optional coverage failing must not block; required target gate must
    assert rep.passed() is False
    fails = {g.name for g in rep.gate_failures()}
    assert "target_extraction_coverage" in fails
    assert "extraction_coverage" not in fails


def test_gate_na_metric_is_not_a_failure():
    """insufficient_n / N/A gate metrics report passed=None, never fail (Q1)."""
    rr = B._run_result_from_payload("p1", _payload())
    rep = B.measure(
        run_result=rr, roster=_ROSTER, match=_match(),
        sealed_labels=_sealed({"RivalCorp": "competitor"}),
    )
    g = {x.name: x for x in rep.gates}
    # competitor conditional is insufficient_n here → not a failure
    assert g["conditional_competitor_leakage_rate"].passed is None
    assert g["conditional_competitor_leakage_rate"] not in rep.gate_failures()


def test_measure_report_to_dict_serializes():
    rr = B._run_result_from_payload("p1", _payload())
    rep = B.measure(
        run_result=rr, roster=_ROSTER, match=_match(), sealed_labels=_sealed(),
    )
    d = json.loads(json.dumps(rep.to_dict(), ensure_ascii=False))
    assert d["pair"] == "p1" and "metrics" in d and "gates" in d
    assert d["metrics"]["extraction_coverage"]["status"] == "ok"


# ---------- L0: eligibility (pass/fail/ineligible/waived) + applicability ----------

def _full_label_sealed():
    # all 5 roster labeled so competitor (2) / bad_fit (1) classes exist
    return _sealed({
        "ACME Robotics": "target", "Globex": "target", "Initech": "bad_fit",
        "RivalCorp": "competitor", "GhostCo": "competitor",
    })


def test_eligibility_pass_when_all_required_ok():
    rr = B._run_result_from_payload("p1", _payload())
    # gates that are all measurable + satisfied on this tiny fixture
    gates = (("extraction_coverage", ">=", 0.5, B.REQUIRED),
             ("precision_at_10", ">=", 0.1, B.REQUIRED))
    rep = B.measure(run_result=rr, roster=_ROSTER, match=_match(),
                    sealed_labels=_full_label_sealed(), thresholds=gates)
    assert rep.eligibility() == "pass" and rep.passed() is True


def test_eligibility_fail_takes_precedence():
    rr = B._run_result_from_payload("p1", _payload())
    gates = (("extraction_coverage", ">=", 0.99, B.REQUIRED),)  # 0.8 < 0.99 → fail
    rep = B.measure(run_result=rr, roster=_ROSTER, match=_match(),
                    sealed_labels=_full_label_sealed(), thresholds=gates)
    assert rep.eligibility() == "fail" and rep.passed() is False


def test_required_unmeasured_is_ineligible_not_pass():
    """The headline R2#2 bug: a required gate that is insufficient_n must make the
    run ineligible, NOT silently pass."""
    rr = B._run_result_from_payload("p1", _payload())
    # competitor conditional has only 1 scored competitor → insufficient_n
    gates = (("conditional_competitor_leakage_rate", "<=", 0.1, B.REQUIRED),)
    rep = B.measure(run_result=rr, roster=_ROSTER, match=_match(),
                    sealed_labels=_sealed({"RivalCorp": "competitor"}), thresholds=gates)
    assert rep.eligibility() == "ineligible"
    assert rep.passed() is False


def test_not_applicable_gate_does_not_make_ineligible():
    rr = B._run_result_from_payload("p1", _payload())
    gates = (("extraction_coverage", ">=", 0.5, B.REQUIRED),
             ("conditional_competitor_leakage_rate", "<=", 0.1, B.NOT_APPLICABLE))
    rep = B.measure(run_result=rr, roster=_ROSTER, match=_match(),
                    sealed_labels=_sealed({"RivalCorp": "competitor"}), thresholds=gates)
    assert rep.eligibility() == "pass"  # competitor n/a excluded, coverage ok


def test_partner_competitor_gate_auto_not_applicable():
    """Partner mode → competitor gate is not_applicable, never ineligible (R2#2)."""
    rr = B._run_result_from_payload("p1", _payload())
    gates = (("extraction_coverage", ">=", 0.5, B.REQUIRED),
             ("conditional_competitor_leakage_rate", "<=", 0.1, B.REQUIRED))
    rep = B.measure(run_result=rr, roster=_ROSTER, match=_match(),
                    sealed_labels=_sealed({"RivalCorp": "competitor"}),
                    target_mode="partner", thresholds=gates)
    comp = next(g for g in rep.gates if g.name == "conditional_competitor_leakage_rate")
    assert comp.applicability == "not_applicable"
    assert rep.eligibility() == "pass"


def test_waiver_yields_waived_not_pass():
    """A waived required gate must serialize as `waived`, never passed=True (R2#4)."""
    rr = B._run_result_from_payload("p1", _payload())
    gates = (("conditional_competitor_leakage_rate", "<=", 0.1, B.REQUIRED),)
    rep = B.measure(
        run_result=rr, roster=_ROSTER, match=_match(),
        sealed_labels=_sealed({"RivalCorp": "competitor"}), thresholds=gates,
        waivers={"conditional_competitor_leakage_rate": {"reason": "class too small", "by": "tyrical"}},
    )
    assert rep.eligibility() == "waived"
    assert rep.passed() is False
    d = json.loads(json.dumps(rep.to_dict(), ensure_ascii=False))
    g = d["gates"][0]
    assert g["waived"] is True and g["waiver_by"] == "tyrical"
    assert d["passed"] is False and d["eligibility"] == "waived"


def test_freeze_load_roundtrips_applicability(tmp_path):
    out = tmp_path / "thr.json"
    B.freeze_thresholds(
        gates=(("extraction_coverage", ">=", 0.8, B.REQUIRED),
               ("conditional_competitor_leakage_rate", "<=", 0.1, B.NOT_APPLICABLE)),
        now_iso="2026-06-08T00:00:00+00:00", path=out,
    )
    gates, m = B.load_threshold_manifest(out)
    assert gates[0] == ("extraction_coverage", ">=", 0.8, "required")
    assert gates[1][3] == "not_applicable"


def test_old_3tuple_manifest_reads_as_required():
    assert B._normalize_gate(("extraction_coverage", ">=", 0.8)) == (
        "extraction_coverage", ">=", 0.8, "required")
