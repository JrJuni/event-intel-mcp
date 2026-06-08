"""P1.3 — check_runtime surfaces in-app setup status + 12-tool surface.

The setup block (model download + ChatGPT login state) must appear on BOTH the
success and failure envelopes, mirroring the W5 paths block.
"""
from __future__ import annotations

import importlib
import json


def _cr():
    return importlib.import_module("event_intel.tools.check_runtime").check_runtime


def test_setup_block_on_success_envelope(monkeypatch, tmp_path):
    from event_intel.providers import llm as _llm

    # no token file → logged_in False (deterministic, no network)
    monkeypatch.setattr(_llm.ChatGPTOAuthProvider, "_TOKEN_PATH", tmp_path / "no_tok.json")
    monkeypatch.setattr(
        "event_intel.runtime.preflight.run_preflight",
        lambda *a, **kw: {"ok": True, "checks": {}},
    )
    res = _cr()(workspace_id="default")
    assert res["ok"] is True
    setup = res["setup"]
    assert "model_prep" in setup and "model_cached" in setup["model_prep"]
    assert "phase" in setup["model_prep"]
    assert setup["chatgpt_login"]["logged_in"] is False
    assert "job" in setup["chatgpt_login"]


def test_setup_block_on_failure_envelope(monkeypatch, tmp_path):
    from event_intel.errors import ErrorCode, MCPError, Stage
    from event_intel.providers import llm as _llm

    monkeypatch.setattr(_llm.ChatGPTOAuthProvider, "_TOKEN_PATH", tmp_path / "no_tok.json")

    def _boom(*a, **kw):
        raise MCPError(
            error_code=ErrorCode.MODEL_NOT_READY, stage=Stage.PREFLIGHT, message="x"
        )

    monkeypatch.setattr("event_intel.runtime.preflight.run_preflight", _boom)
    res = _cr()(workspace_id="default")
    assert res["ok"] is False
    assert res["error_code"] == "MODEL_NOT_READY"
    # setup (and paths) still present even though preflight failed
    assert "setup" in res and "model_prep" in res["setup"]
    assert "paths" in res


def test_setup_reflects_logged_in_token(monkeypatch, tmp_path):
    import time

    from event_intel.providers import llm as _llm

    tok = tmp_path / "tok.json"
    tok.write_text(
        json.dumps({"access_token": "a", "expires_at": time.time() + 3600}), encoding="utf-8"
    )
    monkeypatch.setattr(_llm.ChatGPTOAuthProvider, "_TOKEN_PATH", tok)
    monkeypatch.setattr(
        "event_intel.runtime.preflight.run_preflight", lambda *a, **kw: {"ok": True}
    )
    res = _cr()(workspace_id="default")
    assert res["setup"]["chatgpt_login"]["logged_in"] is True


def test_manifest_lists_twelve_tools_including_setup(repo_root):
    m = json.loads((repo_root / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    names = {t["name"] for t in m["tools"]}
    assert len(m["tools"]) == 12
    assert {"prepare_models", "login_chatgpt"} <= names
    assert "12 MCP tools" in m["description"]


def test_all_twelve_tools_registered_on_server():
    server = importlib.import_module("event_intel.mcp_server")
    for name in (
        "check_runtime", "draft_capability_cards", "validate_capability_cards",
        "ingest_product_context", "build_event_tier_list", "analyze_event_page",
        "probe_exhibitor_endpoint", "acquire_exhibitor_source", "draft_labels",
        "sync_product_sources", "prepare_models", "login_chatgpt",
    ):
        assert callable(getattr(server, name)), f"{name} not registered"
