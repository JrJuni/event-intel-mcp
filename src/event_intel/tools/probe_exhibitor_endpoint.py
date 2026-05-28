"""probe_exhibitor_endpoint MCP tool handler — Phase 18T T0 stub.

Real implementation lands in T2.
"""
from __future__ import annotations

from event_intel.errors import ErrorCode, MCPError, Stage


def probe_exhibitor_endpoint(
    url: str = "",
    hints: dict | None = None,
    *,
    lang: str = "en",
) -> dict:
    """Given analyzer hints, probe XHR/embedded-JSON endpoints (T2 stub)."""
    return MCPError(
        error_code=ErrorCode.INTERNAL,
        stage=Stage.ACQUISITION,
        message="probe_exhibitor_endpoint is not implemented yet (Phase 18T T2)",
        hint={"fix": "This tool will be available after Phase 18T T2 ships."},
        retryable=False,
    ).to_envelope()
