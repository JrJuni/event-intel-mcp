"""Batch smoke runner tests — BD critique harness S1.

Covers spec loading/validation, output collection, partial-failure isolation,
and tier aggregation. build_fn is faked — no engine, no network.
"""
from __future__ import annotations

import json

import pytest
import yaml

from event_intel.errors import ErrorCode, MCPError
from event_intel.eval.smoke_batch import (
    PairSpec,
    load_pair_specs,
    run_smoke_batch,
)

# ---------- load_pair_specs ----------


def _write_spec(tmp_path, obj):
    p = tmp_path / "spec.yaml"
    p.write_text(yaml.safe_dump(obj), encoding="utf-8")
    return p


def test_load_specs_from_pairs_key(tmp_path):
    p = _write_spec(tmp_path, {"pairs": [
        {"pair": "p1", "workspace": "default", "source_kind": "html_file", "source_ref": "a.html"},
        {"pair": "p2", "source_kind": "csv_file", "source_ref": "b.csv", "lang": "ko"},
    ]})
    specs = load_pair_specs(p)
    assert [s.pair for s in specs] == ["p1", "p2"]
    assert specs[1].lang == "ko" and specs[1].event_slug == "p2"  # defaults to pair


def test_load_specs_bare_list(tmp_path):
    p = _write_spec(tmp_path, [{"pair": "p1", "source_kind": "text", "source_ref": "x"}])
    assert load_pair_specs(p)[0].pair == "p1"


def test_load_specs_empty_raises(tmp_path):
    p = _write_spec(tmp_path, {"pairs": []})
    with pytest.raises(MCPError) as exc:
        load_pair_specs(p)
    assert exc.value.error_code == ErrorCode.INVALID_INPUT


def test_load_specs_missing_pair_id_raises(tmp_path):
    p = _write_spec(tmp_path, {"pairs": [{"source_kind": "html_file"}]})
    with pytest.raises(MCPError) as exc:
        load_pair_specs(p)
    assert exc.value.error_code == ErrorCode.INVALID_INPUT


def test_load_specs_invalid_source_kind_raises(tmp_path):
    p = _write_spec(tmp_path, {"pairs": [{"pair": "p1", "source_kind": "pdf_file"}]})
    with pytest.raises(MCPError) as exc:
        load_pair_specs(p)
    assert "source_kind" in exc.value.message


# ---------- run_smoke_batch ----------


def _fake_build_output(tmp_path, pair, tier_counts):
    d = tmp_path / "engine_out" / pair
    d.mkdir(parents=True, exist_ok=True)
    tl = d / "tier_list.yaml"
    tl.write_text("exhibitors: []\n", encoding="utf-8")
    rs = d / "run_summary.json"
    rs.write_text(json.dumps({"run_id": pair}), encoding="utf-8")
    return {
        "ok": True,
        "tier_list_yaml_path": str(tl),
        "run_summary_path": str(rs),
        "tier_counts": tier_counts,
    }


def test_all_ok_collects_outputs_and_aggregates_tiers(tmp_path):
    outputs = {
        "p1": _fake_build_output(tmp_path, "p1", {"S": 1, "A": 2, "B": 3}),
        "p2": _fake_build_output(tmp_path, "p2", {"S": 0, "A": 1, "B": 5}),
    }
    specs = [PairSpec(pair="p1"), PairSpec(pair="p2")]
    out_root = tmp_path / "smoke"
    summary = run_smoke_batch(
        specs, build_fn=lambda s: outputs[s.pair], out_root=out_root, batch_id="b1"
    )
    assert summary["n"] == 2 and summary["n_ok"] == 2 and summary["n_failed"] == 0
    assert summary["tier_totals"] == {"S": 1, "A": 3, "B": 8}
    assert (out_root / "b1" / "p1" / "tier_list.yaml").is_file()
    assert (out_root / "b1" / "p1" / "run_summary.json").is_file()
    assert (out_root / "b1" / "batch.json").is_file()


def test_partial_failure_is_isolated(tmp_path):
    good = _fake_build_output(tmp_path, "p1", {"S": 1})

    def build_fn(s):
        if s.pair == "boom":
            raise RuntimeError("kaboom")
        return good

    specs = [PairSpec(pair="p1"), PairSpec(pair="boom"), PairSpec(pair="p1b")]
    # p1b reuses good's paths — fine for the test (collection is per-pair dir)
    summary = run_smoke_batch(
        specs, build_fn=lambda s: good if s.pair != "boom" else build_fn(s),
        out_root=tmp_path / "smoke", batch_id="b1",
    )
    assert summary["n_ok"] == 2 and summary["n_failed"] == 1
    boom = next(r for r in summary["results"] if r["pair"] == "boom")
    assert boom["ok"] is False and "kaboom" in boom["error"]


def test_build_ok_false_envelope_recorded_as_failure(tmp_path):
    specs = [PairSpec(pair="p1")]
    summary = run_smoke_batch(
        specs,
        build_fn=lambda s: {"ok": False, "message": "PRODUCT_CONTEXT_MISSING"},
        out_root=tmp_path / "smoke", batch_id="b1",
    )
    assert summary["n_failed"] == 1
    assert "PRODUCT_CONTEXT_MISSING" in summary["results"][0]["error"]


def test_build_ok_but_no_tier_list_is_failure(tmp_path):
    specs = [PairSpec(pair="p1")]
    summary = run_smoke_batch(
        specs,
        build_fn=lambda s: {"ok": True, "tier_counts": {"S": 1}},  # no path
        out_root=tmp_path / "smoke", batch_id="b1",
    )
    assert summary["n_failed"] == 1
    assert "no tier_list" in summary["results"][0]["error"]


def test_missing_run_summary_still_ok_if_tier_list_present(tmp_path):
    out = _fake_build_output(tmp_path, "p1", {"S": 1})
    out["run_summary_path"] = str(tmp_path / "nope.json")  # missing
    summary = run_smoke_batch(
        [PairSpec(pair="p1")], build_fn=lambda s: out,
        out_root=tmp_path / "smoke", batch_id="b1",
    )
    assert summary["n_ok"] == 1
    assert summary["results"][0]["run_summary_path"] is None
