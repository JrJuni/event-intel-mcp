"""acquire_exhibitor_source MCP tool handler — Phase 18T T0 stub.

Real implementation lands in T3.
"""
from __future__ import annotations

from event_intel.errors import ErrorCode, MCPError, Stage


def acquire_exhibitor_source(
    url: str = "",
    *,
    workspace_id: str = "default",
    event_slug: str = "",
    lang: str = "en",
    refetch: bool = False,
) -> dict:
    """Analyze → probe → fetch → artifact → (source_kind, source_ref) (T3 stub)."""
    return MCPError(
        error_code=ErrorCode.INTERNAL,
        stage=Stage.ACQUISITION,
        message="acquire_exhibitor_source is not implemented yet (Phase 18T T3)",
        hint={"fix": "This tool will be available after Phase 18T T3 ships."},
        retryable=False,
    ).to_envelope()
