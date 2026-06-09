"""get_job MCP tool — Y2.1c. Poll a background job's status/result.

Returns the job manifest (status ∈ running/done/failed/interrupted, plus
result_artifact_ids on done). Workspace-scoped. A job left running by a prior
server process reads as `interrupted` (compute is not resumed).
"""
from __future__ import annotations

from event_intel.errors import ErrorCode, MCPError, Stage, envelope_from_exception
from event_intel.runtime import job_store as _job_store


def get_job(job_id: str = "", workspace_id: str = "default") -> dict:
    """Return a background job's status. INVALID_INPUT if job_id missing/unknown."""
    try:
        if not job_id:
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT, stage=Stage.PREFLIGHT,
                message="job_id is required", hint={"field": "job_id"},
            )
        manifest = _job_store.get_job(workspace_id=workspace_id, job_id=job_id)
        if manifest is None:
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT, stage=Stage.PREFLIGHT,
                message="job not found in workspace (unknown or invalid job_id)",
                hint={"field": "job_id", "job_id": job_id},
            )
        return {
            "ok": True,
            "job_id": manifest["job_id"],
            "tool": manifest.get("tool"),
            "status": manifest["status"],
            "result_artifact_ids": manifest.get("result_artifact_ids", []),
            "error": manifest.get("error"),
        }
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.PREFLIGHT)
