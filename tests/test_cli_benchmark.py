"""Y1 CS8 — benchmark CLI sub-app: threshold-freeze / run / company-packet /
evidence-packet / measure. Exercises the blind workflow over real files with the
engine monkeypatched, asserting the state-machine ordering the commands enforce."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from event_intel.cli import app
from event_intel.eval import blind as _blind
from event_intel.eval import roster as _roster

runner = CliRunner()


def _write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(path)


_ROSTER = [
    {"roster_id": "r1", "canonical_name": "ACME Robotics", "aliases": [], "label": "target"},
    {"roster_id": "r2", "canonical_name": "Globex", "aliases": [], "label": "target"},
    {"roster_id": "r3", "canonical_name": "RivalCorp", "aliases": [], "label": "competitor"},
]

_RUN_SUMMARY = {
    "run_id": "p1-20260608-abcd1234",
    "run_fingerprint": "fp-1",
    "companies": [
        {"name": "ACME Robotics", "tier": "S", "final_score": 9.0},
        {"name": "Globex", "tier": "A", "final_score": 7.0},
        {"name": "RivalCorp", "tier": "S", "final_score": 8.0},
    ],
}


def test_benchmark_listed_in_help():
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0 and "benchmark" in res.stdout


# ---------- threshold-freeze ----------

def test_threshold_freeze_writes_immutable_manifest(tmp_path):
    out = tmp_path / "thresholds.json"
    res = runner.invoke(app, ["benchmark", "threshold-freeze", "--out", str(out)])
    assert res.exit_code == 0, res.stdout
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["sha"] and "gates" in manifest and "frozen_at" in manifest
    # immutable: second freeze to same path fails (refuses overwrite).
    res2 = runner.invoke(app, ["benchmark", "threshold-freeze", "--out", str(out)])
    assert res2.exit_code != 0


# ---------- run (engine monkeypatched) ----------

def _patch_engine(monkeypatch, tmp_path):
    """Make build_event_tier_list write a run_summary and return its path."""
    rs_path = tmp_path / "run_summary.json"
    rs_path.write_text(json.dumps(_RUN_SUMMARY), encoding="utf-8")
    import event_intel.tools.build_event_tier_list as bt

    monkeypatch.setattr(
        bt, "build_event_tier_list",
        lambda **kw: {"ok": True, "run_summary_path": str(rs_path)},
    )


def test_run_persists_gold_blind_run_result(tmp_path, monkeypatch):
    _patch_engine(monkeypatch, tmp_path)
    runs_root = tmp_path / "runs"
    res = runner.invoke(app, [
        "benchmark", "run", "--pair", "p1", "--runs-root", str(runs_root),
        "--event-name", "Expo", "--event-slug", "expo",
        "--html-file", str(tmp_path / "x.html"),
    ])
    assert res.exit_code == 0, res.stdout
    run_dir = runs_root / "p1" / _RUN_SUMMARY["run_id"]
    record = json.loads((run_dir / "run_result.json").read_text(encoding="utf-8"))
    assert record["run_id"] == _RUN_SUMMARY["run_id"]
    assert "labels" not in record and "gold" not in record  # blind


# ---------- company-packet ----------

def test_company_packet_full_cohort(tmp_path):
    roster = _write(tmp_path / "roster.json", _ROSTER)
    out = tmp_path / "packet.json"
    res = runner.invoke(app, [
        "benchmark", "company-packet", "--pair", "p1",
        "--roster", roster, "--cohort", "full", "--out", str(out),
    ])
    assert res.exit_code == 0, res.stdout
    packet = _blind.packet_from_dict(json.loads(out.read_text(encoding="utf-8")))
    assert len(packet.entries) == 3
    assert all(set(e) == {"index", "name"} for e in packet.entries)  # no engine output


# ---------- evidence-packet ordering (R2-2) ----------

def _seed_run_dir(tmp_path):
    """Persist a run_result with top10_evidence for the evidence-packet commands."""
    from event_intel.eval import benchmark as _bm

    payload = {**_RUN_SUMMARY, "top10_evidence": [
        {"company": "ACME Robotics", "credited_type": "official_url",
         "snippet": "s", "url": "https://acme", "published_at": None},
    ]}
    return _bm.run(pair="p1", build_fn=lambda: payload, runs_root=tmp_path / "runs")


def test_evidence_packet_refuses_without_sealed_labels(tmp_path):
    run_dir = _seed_run_dir(tmp_path)
    res = runner.invoke(app, [
        "benchmark", "evidence-packet", "--pair", "p1", "--run-dir", str(run_dir),
        "--sealed-labels", str(tmp_path / "missing.json"),  # not sealed yet
        "--out", str(tmp_path / "ev.json"),
    ])
    assert res.exit_code != 0  # build_evidence_packet raises (order enforcement)


def test_evidence_packet_built_after_sealing(tmp_path):
    run_dir = _seed_run_dir(tmp_path)
    sealed = _blind.seal_company_labels(
        _blind.build_company_packet(pair="p1", cohort=_blind.FULL,
                                    roster=_roster.load_roster(_write(tmp_path / "r.json", _ROSTER))),
        {"ACME Robotics": "target"},
    )
    sl_path = _write(tmp_path / "sealed.json", _blind.sealed_labels_to_dict(sealed))
    out = tmp_path / "ev.json"
    res = runner.invoke(app, [
        "benchmark", "evidence-packet", "--pair", "p1", "--run-dir", str(run_dir),
        "--sealed-labels", sl_path, "--out", str(out),
    ])
    assert res.exit_code == 0, res.stdout
    ev = _blind.evidence_packet_from_dict(json.loads(out.read_text(encoding="utf-8")))
    assert len(ev.items) == 1 and ev.items[0].company == "ACME Robotics"


# ---------- measure (full join) ----------

def test_measure_joins_and_reports_gates(tmp_path):
    run_dir = _seed_run_dir(tmp_path)
    roster_path = _write(tmp_path / "roster.json", _ROSTER)
    sealed = _blind.seal_company_labels(
        _blind.build_company_packet(pair="p1", cohort=_blind.FULL,
                                    roster=_roster.load_roster(roster_path)),
        {"ACME Robotics": "target", "Globex": "target", "RivalCorp": "competitor"},
    )
    sl_path = _write(tmp_path / "sealed.json", _blind.sealed_labels_to_dict(sealed))
    res = runner.invoke(app, [
        "benchmark", "measure", "--run-dir", str(run_dir),
        "--roster", roster_path, "--sealed-labels", sl_path,
        "--target-mode", "customer",
    ])
    payload = json.loads(res.stdout)
    assert payload["ok"] is True
    # all 3 roster materialized → coverage 1.0
    assert payload["metrics"]["extraction_coverage"]["value"] == 1.0
    assert "gates" in payload


# ---------- CS9 labeling-sheet + seal-labels ----------

_SOURCE_CSV = "name,description,url\nClickHouse,OLAP column store,https://clickhouse.com\nCoreWeave,GPU cloud,https://coreweave.com\nGlobex,nosql db,\n"
_CARD_YAML = (
    "schema_version: 2\nproduct_name: TestDB\none_liner: a database\n"
    "capabilities:\n  - name: vectors\n    keywords: [v]\n    buyer_pains: [p]\n    evidence_queries: [q]\n"
    "ideal_customer:\n  industries: [saas]\n  company_signals: [s]\n"
    "competitors:\n  - name: ClickHouse\nbad_fit:\n  - reason: gpu clouds\n"
)


def test_labeling_sheet_and_seal_roundtrip(tmp_path):
    # packet (full cohort over the 3 source companies)
    roster = _write(tmp_path / "roster.json", [
        {"roster_id": "r1", "canonical_name": "ClickHouse"},
        {"roster_id": "r2", "canonical_name": "CoreWeave"},
        {"roster_id": "r3", "canonical_name": "Globex"},
    ])
    packet = tmp_path / "packet.json"
    runner.invoke(app, ["benchmark", "company-packet", "--pair", "p", "--roster", roster,
                        "--cohort", "full", "--out", str(packet)])
    src = tmp_path / "src.csv"
    src.write_text(_SOURCE_CSV, encoding="utf-8")
    card = tmp_path / "card.yaml"
    card.write_text(_CARD_YAML, encoding="utf-8")
    sheet = tmp_path / "sheet.json"
    wmd = tmp_path / "work.md"

    res = runner.invoke(app, [
        "benchmark", "labeling-sheet", "--pair", "p", "--packet", str(packet),
        "--source", str(src), "--source-format", "csv",
        "--name-key", "name", "--overview-keys", "description", "--url-key", "url",
        "--card", str(card), "--out-json", str(sheet), "--out-md", str(wmd),
    ])
    assert res.exit_code == 0, res.stdout
    rows = json.loads(sheet.read_text(encoding="utf-8"))
    assert len(rows) == 3 and all(r["label"] == "" for r in rows)
    assert {r["name"]: r["overview"] for r in rows}["ClickHouse"] == "OLAP column store"
    assert "TestDB" in wmd.read_text(encoding="utf-8")  # rubric header rendered

    # fill labels and seal
    for r in rows:
        r["label"] = "competitor" if r["name"] == "ClickHouse" else "target"
    sheet.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    sealed = tmp_path / "sealed.json"
    res2 = runner.invoke(app, ["benchmark", "seal-labels", "--sheet", str(sheet),
                               "--packet", str(packet), "--out", str(sealed)])
    assert res2.exit_code == 0, res2.stdout
    sl = json.loads(sealed.read_text(encoding="utf-8"))
    assert sl["labels"]["ClickHouse"] == "competitor" and sl["sha"]


def test_seal_labels_rejects_partial_without_flag(tmp_path):
    roster = _write(tmp_path / "roster.json", [{"roster_id": "r1", "canonical_name": "A"}])
    packet = tmp_path / "packet.json"
    runner.invoke(app, ["benchmark", "company-packet", "--pair", "p", "--roster", roster,
                        "--cohort", "full", "--out", str(packet)])
    sheet = _write(tmp_path / "sheet.json", [{"index": 0, "name": "A", "label": ""}])
    res = runner.invoke(app, ["benchmark", "seal-labels", "--sheet", sheet,
                              "--packet", str(packet), "--out", str(tmp_path / "s.json")])
    assert res.exit_code != 0  # unlabeled row → error


# ---------- L3 cross-vendor + apply-refinements CLI ----------

def _drafted_sheet(tmp_path):
    sheet = tmp_path / "drafted.json"
    rows = [
        {"index": 0, "name": "Acme", "overview": "db vendor", "url": None, "label": "",
         "suggested_label": "competitor", "confidence": 0.9, "source": "gpt_draft", "needs_review": False},
        {"index": 1, "name": "Globex", "overview": "ai startup", "url": None, "label": "",
         "suggested_label": "target", "confidence": 0.5, "source": "gpt_draft", "needs_review": True},
    ]
    _write(sheet, rows)
    return sheet, rows


def test_cli_independent_view_then_cross_vendor(tmp_path):
    sheet, rows = _drafted_sheet(tmp_path)
    view_out = tmp_path / "view.json"
    res = runner.invoke(app, ["benchmark", "independent-view", "--sheet", str(sheet), "--out", str(view_out)])
    assert res.exit_code == 0, res.stdout
    sha = json.loads(res.stdout)["input_sha"]

    claude = _write(tmp_path / "claude.json", {"Acme": "competitor", "Globex": "neutral"})
    cv_out = tmp_path / "cv.json"
    res2 = runner.invoke(app, ["benchmark", "cross-vendor", "--sheet", str(sheet),
                               "--claude-labels", claude, "--input-sha", sha, "--out", str(cv_out)])
    assert res2.exit_code == 0, res2.stdout
    merged = {r["name"]: r for r in json.loads(cv_out.read_text(encoding="utf-8"))}
    assert merged["Acme"]["grade"] == "gold"          # agreed
    assert merged["Globex"]["needs_review"] is True   # disagreed → flagged


def test_cli_apply_refinements(tmp_path):
    sheet, rows = _drafted_sheet(tmp_path)
    refs = _write(tmp_path / "refs.json", {"Globex": {"final_label": "target", "evidence_urls": ["https://g"]}})
    out = tmp_path / "refined.json"
    res = runner.invoke(app, ["benchmark", "apply-refinements", "--sheet", str(sheet),
                              "--refinements", refs, "--out", str(out)])
    assert res.exit_code == 0, res.stdout
    by = {r["name"]: r for r in json.loads(out.read_text(encoding="utf-8"))}
    assert by["Globex"]["grade"] == "gold" and by["Globex"]["source"] == "search_refine"


def test_cli_label_stats(tmp_path):
    sheet = _write(tmp_path / "s.json", [
        {"name": "A", "suggested_label": "target", "final_label": "target", "grade": "silver", "needs_review": False},
        {"name": "B", "suggested_label": "competitor", "final_label": "competitor", "grade": "gold",
         "source": "cross_agree", "needs_review": False},
    ])
    res = runner.invoke(app, ["benchmark", "label-stats", "--sheet", sheet])
    assert res.exit_code == 0, res.stdout
    assert json.loads(res.stdout)["gold_rate"] == 0.5
