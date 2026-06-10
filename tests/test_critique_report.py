"""Critique aggregation + dashboard tests — BD critique harness S4."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from event_intel.cli import app
from event_intel.errors import MCPError
from event_intel.eval.critique_packet import EXPECTED_LENSES
from event_intel.eval.critique_report import aggregate_critiques

runner = CliRunner()


def _pick(name, *, would_sa=True, verdicts=None, defensible=True, flag=False):
    verdicts = verdicts or {lk: "agree" for lk in EXPECTED_LENSES}
    return {
        "name": name,
        "independent_first": {"would_place_sa": would_sa, "reason": "r"},
        "lenses": {lk: {"verdict": v, "reason": "r"} for lk, v in verdicts.items()},
        "defensible": defensible,
        "flag": flag,
    }


def _crit(pair, picks, judge="host:claude"):
    return {"pair": pair, "packet_sha": "sha", "judge_model_id": judge, "picks": picks}


# ---------- aggregation ----------


def test_basic_defensibility_rates():
    crits = [
        _crit("p1", [_pick("A", defensible=True), _pick("B", defensible=False)]),
        _crit("p2", [_pick("C", defensible=True)]),
    ]
    d = aggregate_critiques(crits)
    assert d["n_pairs"] == 2 and d["n_picks"] == 3 and d["n_defensible"] == 2
    assert d["overall_defensibility_rate"] == round(2 / 3, 4)
    p1 = next(p for p in d["pairs"] if p["pair"] == "p1")
    assert p1["defensibility_rate"] == 0.5


def test_host_flag_becomes_triage():
    d = aggregate_critiques([_crit("p1", [_pick("A", flag=True)])])
    assert d["n_triage"] == 1
    assert "host_flag" in d["triage_candidates"][0]["reasons"]


def test_majority_lens_disagree_triages_even_if_not_flagged():
    # 2 of 3 lenses disagree → majority, even with flag=False, defensible=True
    verdicts = {"customer_fit": "disagree", "competitor": "disagree", "buying_signal": "agree"}
    d = aggregate_critiques([_crit("p1", [_pick("A", verdicts=verdicts, flag=False)])])
    assert d["n_triage"] == 1
    t = d["triage_candidates"][0]
    assert "majority_lens_disagree" in t["reasons"] and t["lens_disagree_count"] == 2


def test_single_lens_disagree_is_not_majority():
    verdicts = {"customer_fit": "disagree", "competitor": "agree", "buying_signal": "agree"}
    d = aggregate_critiques([_crit("p1", [_pick("A", verdicts=verdicts, flag=False, would_sa=True)])])
    assert d["n_triage"] == 0


def test_judge_would_not_place_triages():
    d = aggregate_critiques([_crit("p1", [_pick("A", would_sa=False)])])
    assert "judge_would_not_place" in d["triage_candidates"][0]["reasons"]


def test_empty_critiques():
    d = aggregate_critiques([])
    assert d["n_pairs"] == 0 and d["n_picks"] == 0
    assert d["overall_defensibility_rate"] is None
    assert d["triage_candidates"] == [] and d["pairs"] == []


def test_dashboard_is_silver_advisory():
    d = aggregate_critiques([_crit("p1", [_pick("A")])])
    assert d["grade"] == "silver"
    assert "NOT a holdout" in d["advisory"]


def test_judges_provenance_collected():
    d = aggregate_critiques([
        _crit("p1", [_pick("A")], judge="host:claude"),
        _crit("p2", [_pick("B")], judge="host:claude-opus"),
    ])
    assert d["judges"] == ["host:claude", "host:claude-opus"]


def test_validation_rejects_bad_critique():
    bad = _crit("p1", [_pick("A")])
    del bad["picks"][0]["lenses"]["competitor"]  # missing lens
    with pytest.raises(MCPError):
        aggregate_critiques([bad])


# ---------- CLI smoke ----------


def test_cli_critique_stats_smoke(tmp_path):
    c1 = tmp_path / "c1.json"
    c1.write_text(json.dumps(_crit("p1", [_pick("A", flag=True), _pick("B")])), encoding="utf-8")
    c2 = tmp_path / "c2.json"
    c2.write_text(json.dumps(_crit("p2", [_pick("C", would_sa=False)])), encoding="utf-8")
    out = tmp_path / "dashboard.json"

    res = runner.invoke(app, [
        "benchmark", "critique-stats", "--critique", str(c1), "--critique", str(c2),
        "--out", str(out),
    ])
    assert res.exit_code == 0, res.output
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["n_pairs"] == 2 and d["n_triage"] == 2 and d["grade"] == "silver"
