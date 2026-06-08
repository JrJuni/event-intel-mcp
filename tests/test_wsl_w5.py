"""W5 — storage migrate (non-destructive, checksum-verified) + check_runtime
path exposure + MCPB folder config.
"""
from __future__ import annotations

import importlib
import json

from typer.testing import CliRunner

from event_intel.storage import migrate as M


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# plan_migration
# --------------------------------------------------------------------------- #
def test_plan_classifies_new_identical_and_conflict(tmp_path):
    src = tmp_path / "outputs"
    dst = tmp_path / "EventIntel"
    _write(src / "default" / "capability_cards.yaml", "cards-A")
    _write(src / "default" / "report.md", "report")
    _write(src / "default" / ".gitkeep", "")  # ignored
    # identical at dst
    _write(dst / "default" / "report.md", "report")
    # conflict at dst (different content, same path)
    _write(dst / "default" / "capability_cards.yaml", "cards-B-DIFFERENT")

    plan = M.plan_migration(src_root=src, dst_root=dst)
    s = plan.summary()
    rels = {str(r) for r in plan.skipped_identical}
    confs = {c for c in s["conflicts"]}
    assert any("report.md" in r for r in rels)
    assert any("capability_cards.yaml" in c for c in confs)
    assert s["to_copy"] == 0  # both files exist at dst (one identical, one conflict)


def test_plan_copies_new_files(tmp_path):
    src = tmp_path / "outputs"
    dst = tmp_path / "EventIntel"
    _write(src / "default" / "a.yaml", "A")
    _write(src / "default" / "sub" / "b.md", "B")
    plan = M.plan_migration(src_root=src, dst_root=dst)
    assert plan.summary()["to_copy"] == 2
    assert plan.total_copy_bytes == 2  # "A" + "B"


def test_plan_missing_src_is_empty(tmp_path):
    plan = M.plan_migration(src_root=tmp_path / "nope", dst_root=tmp_path / "dst")
    assert plan.summary()["nothing_to_do"] is True


def test_plan_same_src_dst_is_empty(tmp_path):
    d = tmp_path / "x"
    _write(d / "f.txt", "y")
    plan = M.plan_migration(src_root=d, dst_root=d)
    assert plan.summary()["nothing_to_do"] is True


# --------------------------------------------------------------------------- #
# apply_migration
# --------------------------------------------------------------------------- #
def test_apply_copies_verifies_and_preserves_source(tmp_path):
    src = tmp_path / "outputs"
    dst = tmp_path / "EventIntel"
    _write(src / "default" / "a.yaml", "hello cards")
    _write(src / "default" / "evt" / "tier_list.md", "# report")
    plan = M.plan_migration(src_root=src, dst_root=dst)
    res = M.apply_migration(plan)

    assert res["ok"] is True
    assert res["copied"] == 2
    assert res["source_preserved"] is True
    # files exist at destination with identical content
    assert (dst / "default" / "a.yaml").read_text(encoding="utf-8") == "hello cards"
    assert (dst / "default" / "evt" / "tier_list.md").read_text(encoding="utf-8") == "# report"
    # source still present (never deleted)
    assert (src / "default" / "a.yaml").is_file()


def test_apply_does_not_overwrite_conflicts(tmp_path):
    src = tmp_path / "outputs"
    dst = tmp_path / "EventIntel"
    _write(src / "default" / "a.yaml", "NEW")
    _write(dst / "default" / "a.yaml", "OLD-keep-me")
    plan = M.plan_migration(src_root=src, dst_root=dst)
    res = M.apply_migration(plan)
    assert res["copied"] == 0
    assert any("a.yaml" in c for c in res["conflicts"])
    # destination untouched
    assert (dst / "default" / "a.yaml").read_text(encoding="utf-8") == "OLD-keep-me"


# --------------------------------------------------------------------------- #
# default_migration_roots
# --------------------------------------------------------------------------- #
def test_default_roots_use_repo_outputs_and_home_eventintel(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    src, dst = M.default_migration_roots(env={}, home=home, repo_root=repo)
    assert src == repo / "outputs"
    assert dst == home / "EventIntel"


def test_default_roots_honor_workspace_env(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    custom = tmp_path / "custom ws"
    src, dst = M.default_migration_roots(
        env={"EVENT_INTEL_WORKSPACE_DIR": str(custom)}, home=home, repo_root=repo
    )
    assert dst == custom


# --------------------------------------------------------------------------- #
# check_runtime paths exposure
# --------------------------------------------------------------------------- #
def test_check_runtime_exposes_paths_on_failure(monkeypatch, tmp_path):
    from event_intel.errors import ErrorCode, MCPError, Stage

    monkeypatch.setenv("EVENT_INTEL_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("EVENT_INTEL_DATA_DIR", str(tmp_path / "data"))

    def _boom(*a, **kw):
        raise MCPError(
            error_code=ErrorCode.MODEL_NOT_READY, stage=Stage.PREFLIGHT, message="not ready"
        )

    monkeypatch.setattr("event_intel.runtime.preflight.run_preflight", _boom)
    cr = importlib.import_module("event_intel.tools.check_runtime").check_runtime
    res = cr(workspace_id="default")
    assert res["ok"] is False
    assert res["error_code"] == "MODEL_NOT_READY"
    # paths block present even though preflight failed
    assert "paths" in res
    assert res["paths"]["chroma"]["path"].endswith("chroma")
    assert res["paths"]["sources"]["path"].replace("\\", "/").endswith("default/sources")


def test_check_runtime_exposes_paths_on_success(monkeypatch, tmp_path):
    monkeypatch.setenv("EVENT_INTEL_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(
        "event_intel.runtime.preflight.run_preflight",
        lambda *a, **kw: {"ok": True, "checks": {}},
    )
    cr = importlib.import_module("event_intel.tools.check_runtime").check_runtime
    res = cr(workspace_id="default")
    assert res["ok"] is True
    assert "paths" in res
    assert "workspace_root" in res["paths"]


# --------------------------------------------------------------------------- #
# CLI smoke
# --------------------------------------------------------------------------- #
def test_cli_storage_migrate_dry_run(tmp_path, monkeypatch):
    src = tmp_path / "outputs"
    dst = tmp_path / "EventIntel"
    _write(src / "default" / "a.yaml", "A")
    app = importlib.import_module("event_intel.cli").app
    res = CliRunner().invoke(
        app, ["storage", "migrate", "--src", str(src), "--dst", str(dst)]
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["dry_run"] is True
    assert payload["to_copy"] == 1
    # dry-run must NOT write anything
    assert not (dst / "default" / "a.yaml").exists()


def test_cli_storage_migrate_apply(tmp_path):
    src = tmp_path / "outputs"
    dst = tmp_path / "EventIntel"
    _write(src / "default" / "a.yaml", "A")
    app = importlib.import_module("event_intel.cli").app
    res = CliRunner().invoke(
        app, ["storage", "migrate", "--apply", "--src", str(src), "--dst", str(dst)]
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["dry_run"] is False
    assert payload["copied"] == 1
    assert (dst / "default" / "a.yaml").read_text(encoding="utf-8") == "A"


# --------------------------------------------------------------------------- #
# MCPB manifest folder config
# --------------------------------------------------------------------------- #
def test_manifest_has_folder_config_and_env(repo_root):
    m = json.loads((repo_root / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    assert "workspace_dir" in m["user_config"]
    assert "data_dir" in m["user_config"]
    assert m["user_config"]["workspace_dir"]["required"] is False
    env = m["server"]["mcp_config"]["env"]
    assert env["EVENT_INTEL_WORKSPACE_DIR"] == "${user_config.workspace_dir}"
    assert env["EVENT_INTEL_DATA_DIR"] == "${user_config.data_dir}"
