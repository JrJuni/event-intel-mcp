"""File-backed job store — Y2.1c (Remote I/O + Job model).

Long-running tools (build / acquire / ingest / sync) can't block a remote request
for minutes. The pattern: start the work in the background, return a ``job_id``
at once, and let the client poll ``get_job``. This module is the persistent
state + the background runner; tool rewiring to actually return job_ids is opt-in
/ follow-up (this slice ships the foundation, like the artifact registry did).

Persistence is a per-job JSON manifest under ``data_root/jobs/{workspace}/``.
The live compute lives in an in-memory ``async_job.BackgroundJob`` (one per job);
**on server restart that in-memory state is gone but the manifest survives** — so
a job left ``running`` by a prior process is detected via a per-process boot id
and transitioned to ``interrupted`` (compute is NOT resumed — in-memory only).

Result artifacts are **pinned** in the registry on completion so they survive
their own TTL while the job exists (job-manifest GC → unpin is a follow-up).

stdlib + cold modules (paths / artifact_registry / async_job) → cold-import safe.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from event_intel.runtime import async_job as _async_job
from event_intel.runtime import paths as _paths
from event_intel.storage import artifact_registry as _registry

# New per OS process. A manifest whose boot_id != this was created by a prior
# process — if it's still "running", that compute died with that process.
_BOOT_ID = secrets.token_hex(8)

RUNNING = "running"
DONE = "done"
FAILED = "failed"
INTERRUPTED = "interrupted"

_ID_NBYTES = 18
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# Keep live BackgroundJob handles alive for the process lifetime (the manifest is
# the source of truth; this just prevents GC of running threads' state).
_live: dict[str, _async_job.BackgroundJob] = {}


def _jobs_dir(workspace_id: str, config: dict | None) -> Path:
    return _paths.resolve_paths(config).data_root / "jobs" / workspace_id


def _valid_id(job_id: str) -> bool:
    return bool(job_id) and bool(_ID_RE.match(job_id))


def _write_manifest(workspace_id: str, manifest: dict, config: dict | None) -> None:
    d = _jobs_dir(workspace_id, config)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{manifest['job_id']}.json"
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{manifest['job_id']}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(manifest, ensure_ascii=False, indent=2))
        Path(tmp).replace(path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_manifest(workspace_id: str, job_id: str, config: dict | None) -> dict | None:
    if not _valid_id(job_id):
        return None
    p = _jobs_dir(workspace_id, config) / f"{job_id}.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def create_job(
    *, workspace_id: str, tool: str, config: dict | None = None,
    ttl_seconds: int | None = None, now: float | None = None,
) -> dict:
    """Create a RUNNING job manifest and return it (opaque job_id)."""
    now = time.time() if now is None else now
    manifest = {
        "job_id": secrets.token_urlsafe(_ID_NBYTES),
        "tool": tool,
        "status": RUNNING,
        "boot_id": _BOOT_ID,
        "created_at": now,
        "ttl_seconds": ttl_seconds,
        "result_artifact_ids": [],
        "error": None,
    }
    _write_manifest(workspace_id, manifest, config)
    return manifest


def complete_job(
    *, workspace_id: str, job_id: str, result_artifact_ids: list[str],
    config: dict | None = None,
) -> dict | None:
    """Mark DONE + record (and PIN) result artifacts so they outlive their TTL
    while the job exists.
    """
    manifest = _read_manifest(workspace_id, job_id, config)
    if manifest is None:
        return None
    for aid in result_artifact_ids:
        _registry.set_pinned(workspace_id=workspace_id, artifact_id=aid, pinned=True, config=config)
    manifest.update(status=DONE, result_artifact_ids=list(result_artifact_ids))
    _write_manifest(workspace_id, manifest, config)
    return manifest


def fail_job(
    *, workspace_id: str, job_id: str, error: str, config: dict | None = None
) -> dict | None:
    manifest = _read_manifest(workspace_id, job_id, config)
    if manifest is None:
        return None
    manifest.update(status=FAILED, error=error)
    _write_manifest(workspace_id, manifest, config)
    return manifest


def get_job(*, workspace_id: str, job_id: str, config: dict | None = None) -> dict | None:
    """Return the job manifest (workspace-scoped), or None if absent/invalid.

    A job still ``running`` but created by a PRIOR process (boot_id mismatch) is
    transitioned to ``interrupted`` and persisted — compute is not resumed.
    """
    manifest = _read_manifest(workspace_id, job_id, config)
    if manifest is None:
        return None
    if manifest.get("status") == RUNNING and manifest.get("boot_id") != _BOOT_ID:
        manifest["status"] = INTERRUPTED
        manifest["error"] = "server restarted while job was running (compute not resumed)"
        _write_manifest(workspace_id, manifest, config)
    return manifest


def run_as_job(
    *, workspace_id: str, tool: str, fn: Callable[[], list[str]],
    config: dict | None = None, ttl_seconds: int | None = None,
) -> dict:
    """Start ``fn`` in the background and return ``{job_id, status: running}`` at
    once. ``fn`` must return the list of result artifact ids; on success the job
    is DONE with those (pinned), on exception FAILED with the error.
    """
    manifest = create_job(
        workspace_id=workspace_id, tool=tool, config=config, ttl_seconds=ttl_seconds
    )
    job_id = manifest["job_id"]

    def _runner() -> dict:
        try:
            result_ids = fn() or []
            complete_job(
                workspace_id=workspace_id, job_id=job_id,
                result_artifact_ids=list(result_ids), config=config,
            )
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001 — recorded on the manifest, never crashes
            fail_job(
                workspace_id=workspace_id, job_id=job_id,
                error=f"{type(exc).__name__}: {exc}", config=config,
            )
            return {"ok": False}

    bg = _async_job.BackgroundJob(f"job:{job_id}")
    _live[job_id] = bg
    bg.start(_runner, block=False)
    return {"job_id": job_id, "status": RUNNING}
