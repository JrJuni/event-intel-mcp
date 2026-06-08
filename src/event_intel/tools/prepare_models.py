"""prepare_models MCP tool — #14 P1.1 (in-app setup parity).

Triggers the one-time bge-m3 (~1.3 GB) download from inside Claude Desktop so a
non-developer never has to drop to a terminal for `event-intel models prepare`.

NON-BLOCKING: the download would blow the client request timeout, so this starts
it in a background job (``runtime.async_job``) and returns immediately. Poll via
``check_runtime`` (or call this again) until status is ``ready``. The terminal
CLI ``models prepare`` keeps its inline/blocking behavior.

Module-reference imports for monkeypatch safety; cold-import safe at module top
(``runtime.models.prepare_bge_m3`` keeps its heavy sentence-transformers import
inside the function body, which runs in the background thread).
"""
from __future__ import annotations

from event_intel.errors import Stage, envelope_from_exception
from event_intel.providers import embedding as _embedding
from event_intel.runtime import async_job as _async_job
from event_intel.runtime import models as _models

# Process-wide download job — one in-flight bge-m3 download per server.
_download_job = _async_job.BackgroundJob("bge-m3-download")


def prepare_models(*, force: bool = False) -> dict:
    """Ensure bge-m3 is downloaded. Returns an envelope with ``status``:

    - ``ready``       — already cached (or just finished); nothing to do.
    - ``downloading`` — download running in the background; poll check_runtime.
    - ``failed``      — the background download failed; ``error`` + retry hint.

    ``force=True`` re-downloads even if cached (resets the job first).
    """
    try:
        emb = _embedding.BgeM3Provider()
        ready = emb.is_ready()
        if ready.get("status") == "ready" and not force:
            return {
                "ok": True,
                "status": "ready",
                "model": "BAAI/bge-m3",
                "path": ready.get("path"),
                "size_mb": ready.get("size_mb"),
                "message": "bge-m3 is already downloaded and ready.",
            }

        if force:
            _download_job.reset()

        job = _download_job.start(_models.prepare_bge_m3, block=False)
        phase = job.get("phase")
        if phase == "done":
            detail = job.get("detail") or {}
            return {
                "ok": True,
                "status": "ready",
                "model": "BAAI/bge-m3",
                "message": "bge-m3 download finished.",
                **{k: detail[k] for k in ("path", "size_mb") if k in detail},
            }
        if phase == "failed":
            return {
                "ok": True,
                "status": "failed",
                "error": job.get("error"),
                "message": "bge-m3 download failed; call prepare_models again to retry.",
            }
        return {
            "ok": True,
            "status": "downloading",
            "elapsed_seconds": job.get("elapsed_seconds"),
            "message": (
                "bge-m3 (~1.3 GB) is downloading in the background. Call "
                "check_runtime to poll — once the model check reads 'ready', "
                "ingest / build will work."
            ),
        }
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.PREFLIGHT)
