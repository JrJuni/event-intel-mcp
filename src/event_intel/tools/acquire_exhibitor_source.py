"""acquire_exhibitor_source MCP tool handler — Phase 18T T3.

Module-reference imports for monkeypatch safety (project DO NOT rule).
Cold-start safe: no heavy ML imports at module top.
"""
from __future__ import annotations

from event_intel.acquisition import acquire as _acquire
from event_intel.errors import Stage, envelope_from_exception


def acquire_exhibitor_source(
    url: str = "",
    *,
    workspace_id: str = "default",
    event_slug: str = "",
    lang: str = "en",
    refetch: bool = False,
) -> dict:
    """Analyze → probe → fetch → artifact → returns (source_kind, source_ref)."""
    try:
        if not url or not url.strip():
            from event_intel.errors import ErrorCode, MCPError
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT,
                stage=Stage.ACQUISITION,
                message="url is required",
                hint={"field": "url"},
                retryable=False,
            )
        if not event_slug or not event_slug.strip():
            from event_intel.errors import ErrorCode, MCPError
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT,
                stage=Stage.ACQUISITION,
                message="event_slug is required",
                hint={"field": "event_slug"},
                retryable=False,
            )

        result = _acquire.acquire_source(
            url=url,
            workspace_id=workspace_id,
            event_slug=event_slug,
            lang=lang,
            refetch=refetch,
        )

        return {
            "ok": True,
            "source_kind": result.source_kind,
            "source_ref": result.source_ref,
            "verdict": result.analysis.get("verdict"),
            "cached": result.analysis.get("cached", False),
            "artifact_path": str(result.artifact_path) if result.artifact_path else None,
            "manifest_path": str(result.manifest_path) if result.manifest_path else None,
            "analysis": result.analysis,
            "probe": result.probe,
        }

    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.ACQUISITION)
