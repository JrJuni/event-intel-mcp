"""analyze_event_page MCP tool handler — Phase 18T T0 stub.

Real implementation lands in T1. This stub returns a structured INTERNAL
envelope so Claude Desktop can discover the tool without crashing.
"""
from __future__ import annotations

from event_intel.errors import ErrorCode, MCPError, Stage


def analyze_event_page(
    url: str = "",
    *,
    lang: str = "en",
    workspace_id: str = "default",
) -> dict:
    """Classify an exhibition site URL and return acquisition hints (T1 stub)."""
    return MCPError(
        error_code=ErrorCode.INTERNAL,
        stage=Stage.ACQUISITION,
        message="analyze_event_page is not implemented yet (Phase 18T T1)",
        hint={"fix": "This tool will be available after Phase 18T T1 ships."},
        retryable=False,
    ).to_envelope()
