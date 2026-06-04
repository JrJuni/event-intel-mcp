"""mcpb/manifest.json structure guard (Phase 18T.2).

Locks the zero-friction install contract: no repo_path / PYTHONPATH (editable
install makes them redundant), python_path pre-filled via ${HOME}, API keys
optional (they can come from .env).
"""
from __future__ import annotations

import json


def _manifest(repo_root) -> dict:
    return json.loads((repo_root / "mcpb" / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_is_valid_json_with_version(repo_root):
    m = _manifest(repo_root)
    assert m["version"]
    assert m["manifest_version"] == "0.2"


def test_no_repo_path_or_pythonpath(repo_root):
    m = _manifest(repo_root)
    assert "repo_path" not in m["user_config"], "repo_path is redundant under editable install"
    env = m["server"]["mcp_config"]["env"]
    assert "PYTHONPATH" not in env, "PYTHONPATH not needed when event_intel is editable-installed"


def test_python_path_has_home_default(repo_root):
    m = _manifest(repo_root)
    pp = m["user_config"]["python_path"]
    assert "default" in pp and "${HOME}" in pp["default"]


def test_api_keys_are_optional(repo_root):
    m = _manifest(repo_root)
    assert m["user_config"]["brave_api_key"].get("required") is False
    assert m["user_config"]["anthropic_api_key"].get("required") is False
