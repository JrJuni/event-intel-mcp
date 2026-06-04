"""check_runtime MCP tool handler. Wraps runtime.preflight.run_preflight in the
MCPError envelope convention.

IMPORTANT: imports the preflight module by reference, not the symbol, so tests
can monkeypatch `_preflight.run_preflight` and have it actually take effect
through the tool call. The preflight module itself has no heavy ML imports
at module top, so this stays cold-start safe.
"""
from __future__ import annotations

from event_intel.errors import Stage, envelope_from_exception
from event_intel.runtime import preflight as _preflight


def check_runtime(workspace_id: str = "default", warm_up: bool = False) -> dict:
    """Run the 5-check preflight and return the success or MCPError envelope.

    When ``warm_up`` is true and all checks pass, the bge-m3 embedding model is
    loaded into the server process cache so the first build_event_tier_list call
    is fast. The load result appears under ``checks.warm_up``.
    """
    try:
        return _preflight.run_preflight(
            workspace_id, require_product_context=True, warm_up=warm_up
        )
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.PREFLIGHT)
