"""build_event_tier_list output dir must be cwd-INDEPENDENT (2026-06-04 fix).

Claude Desktop spawns the MCP server with an arbitrary cwd (e.g. Program Files);
a relative "outputs" there is unwritable → PermissionError (WinError 5). The base
must derive from the package location, not cwd. EVENT_INTEL_OUTPUT_DIR overrides.
"""
from __future__ import annotations

from event_intel.tools.build_event_tier_list import _outputs_base, _resolve_output_dir


def test_outputs_base_is_absolute_repo_path():
    base = _outputs_base()
    assert base.is_absolute()
    assert base.name == "outputs"


def test_output_dir_is_cwd_independent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # simulate the server's foreign cwd
    out = _resolve_output_dir("smoke", "evt")
    assert out.is_absolute()
    # must NOT resolve under the foreign cwd (that was the WinError 5 bug)
    assert tmp_path not in out.parents
    assert "outputs" in out.parts and "smoke" in out.parts


def test_output_dir_honors_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_OUTPUT_DIR", str(tmp_path / "custom"))
    out = _resolve_output_dir("smoke", "evt")
    assert str(tmp_path / "custom") in str(out)
