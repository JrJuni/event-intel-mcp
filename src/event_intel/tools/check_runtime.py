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


def check_runtime(
    workspace_id: str = "default",
    warm_up: bool = False,
    warm_up_block: bool = False,
) -> dict:
    """Run the 5-check preflight and return the success or MCPError envelope.

    ``checks.warm_up`` always reports the embedding-model warm-up state
    (``not_started`` / ``warming`` / ``ready`` / ``failed``). When ``warm_up`` is
    true it *starts* a background load (non-blocking) so the call can't hit the
    client timeout — poll by calling again until ``warm_up.status == "ready"``.
    ``warm_up_block`` (terminal CLI only) loads inline and waits.
    """
    try:
        return _preflight.run_preflight(
            workspace_id,
            require_product_context=True,
            warm_up=warm_up,
            warm_up_block=warm_up_block,
        )
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.PREFLIGHT)
