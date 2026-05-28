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


def check_runtime(workspace_id: str = "default") -> dict:
    """Run the 5-check preflight and return the success or MCPError envelope."""
    try:
        return _preflight.run_preflight(workspace_id, require_product_context=True)
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.PREFLIGHT)
