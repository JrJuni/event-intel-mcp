"""Cold-start regression — see plan v0.5 Contract #13.

Importing event_intel.mcp_server MUST NOT pull heavy ML libraries into sys.modules.
A regression here causes the 4-minute hang Claude Desktop observed in bd-coldcall-agent
(see CLAUDE.md DO NOT entry for Phase 14C-B).
"""
from __future__ import annotations

import importlib
import sys

import pytest

FORBIDDEN_HEAVY = (
    "torch",
    "transformers",
    "sentence_transformers",
    "chromadb",
    "bitsandbytes",
)


@pytest.fixture
def fresh_sys_modules():
    """Snapshot sys.modules and restore on teardown so we can re-import cleanly."""
    snapshot = set(sys.modules.keys())
    yield
    for mod in list(sys.modules.keys()):
        if mod not in snapshot:
            sys.modules.pop(mod, None)


def _purge(prefix: str) -> None:
    for mod in list(sys.modules.keys()):
        if mod == prefix or mod.startswith(prefix + "."):
            sys.modules.pop(mod, None)


def test_import_mcp_server_does_not_load_heavy_ml(fresh_sys_modules):
    # Purge any prior import so the check measures a real cold start.
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.mcp_server")

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, (
        f"Cold-start regression: importing event_intel.mcp_server leaked {leaked} "
        "into sys.modules. Move the offending import inside a function body."
    )


def test_import_providers_package_is_cold(fresh_sys_modules):
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.providers.llm")
    importlib.import_module("event_intel.providers.embedding")
    importlib.import_module("event_intel.providers.vectorstore")
    importlib.import_module("event_intel.providers.search")
    importlib.import_module("event_intel.providers.fetch")

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, (
        f"Provider modules leaked heavy ML imports: {leaked}. "
        "All heavy deps must be imported inside method bodies, not at module top."
    )


def test_tool_stubs_return_not_implemented_envelope(fresh_sys_modules):
    _purge("event_intel")
    server = importlib.import_module("event_intel.mcp_server")

    # Tools still on the S0 stub (real impls land in S2/S3/S4/S5).
    # check_runtime moved to a real handler in S1 and is exercised elsewhere.
    calls = {
        "draft_capability_cards": {"source_content": "stub"},
        "validate_capability_cards": {"cards_path": "stub.yaml"},
        "ingest_product_context": {"cards_path": "stub.yaml"},
        "build_event_tier_list": {
            "event_name": "Stub",
            "event_slug": "stub",
            "source_ref": "stub.html",
        },
    }
    for tool_name, kwargs in calls.items():
        handler = getattr(server, tool_name)
        result = handler(**kwargs)
        assert result["ok"] is False, f"{tool_name} did not return ok=false"
        assert result["error_code"] == "INTERNAL"
        assert "not implemented yet" in result["message"]


def test_check_runtime_tool_returns_envelope(fresh_sys_modules):
    """check_runtime should always return an envelope (ok bool present), never raise.
    Whether ok=True or ok=False depends on the local machine state (bge-m3 cached,
    keys set, product context ingested) so we only assert envelope shape.
    """
    _purge("event_intel")
    server = importlib.import_module("event_intel.mcp_server")
    result = server.check_runtime(workspace_id="default")
    assert isinstance(result, dict)
    assert "ok" in result
    if result["ok"] is False:
        for key in ("error_code", "stage", "message"):
            assert key in result, f"failure envelope missing `{key}`"
