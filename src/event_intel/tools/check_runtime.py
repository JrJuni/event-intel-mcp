"""check_runtime MCP tool handler. Wraps runtime.preflight.run_preflight in the
MCPError envelope convention.

IMPORTANT: imports the preflight module by reference, not the symbol, so tests
can monkeypatch `_preflight.run_preflight` and have it actually take effect
through the tool call. The preflight module itself has no heavy ML imports
at module top, so this stays cold-start safe.
"""
from __future__ import annotations

import os
from pathlib import Path

from event_intel.errors import Stage, envelope_from_exception
from event_intel.runtime import paths as _paths
from event_intel.runtime import preflight as _preflight


def _path_info(p: Path) -> dict:
    """Best-effort {path, exists, writable}. Writable probes the nearest existing
    ancestor via os.access (read-only; never creates anything). os.access W_OK is
    advisory on Windows but still flags an obviously unwritable location.
    """
    exists = False
    try:
        exists = p.exists()
    except OSError:
        pass
    probe = p
    try:
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        writable = os.access(probe, os.W_OK)
    except OSError:
        writable = False
    return {"path": str(p), "exists": exists, "writable": writable}


def _resolve_paths_block(workspace_id: str) -> dict:
    """Resolved storage paths for the workspace — always returned, even when the
    model/API preflight fails, so the user can see WHERE things will live. Never
    raises (config load is best-effort; falls back to env + defaults).
    """
    config = None
    try:
        config = _preflight.load_config()
    except Exception:  # noqa: BLE001 — paths are best-effort; defaults are fine
        config = None
    try:
        rp = _paths.resolve_paths(config)
        return {
            "workspace_root": _path_info(rp.workspace_root),
            "workspace_dir": _path_info(rp.workspace_dir(workspace_id)),
            "cards": _path_info(rp.workspace_dir(workspace_id) / "capability_cards.yaml"),
            "sources": _path_info(rp.sources_root(workspace_id)),
            "reports": _path_info(rp.workspace_dir(workspace_id)),
            "chroma": _path_info(rp.chroma_dir),
            "artifacts": _path_info(rp.artifacts_root),
            "source_index_manifest": _path_info(rp.source_index_manifest(workspace_id)),
            "workspace_root_is_legacy": rp.workspace_root_is_legacy,
        }
    except Exception as exc:  # noqa: BLE001 — never let path display break the tool
        return {"error": f"path resolution failed: {exc}"}


def _setup_status_block() -> dict:
    """In-app setup state (#14): bge-m3 download + ChatGPT OAuth login, so a
    non-developer can see what still needs doing and poll the async tools.
    Best-effort, never raises; attached to every check_runtime envelope.
    """
    out: dict = {}
    try:  # model download (prepare_models async job + cache snapshot)
        from event_intel.providers import embedding as _embedding
        from event_intel.tools import prepare_models as _pm

        cached = _embedding.BgeM3Provider().is_ready().get("status") == "ready"
        out["model_prep"] = {"model_cached": cached, **_pm._download_job.status()}
    except Exception as exc:  # noqa: BLE001
        out["model_prep"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:  # ChatGPT OAuth login (auth_status + login_chatgpt async job)
        from event_intel.providers import llm as _llm
        from event_intel.tools import login_chatgpt as _lc

        auth = _llm.ChatGPTOAuthProvider().auth_status()
        out["chatgpt_login"] = {
            "logged_in": auth.get("logged_in"),
            "token_path": auth.get("token_path"),
            "job": _lc._login_job.status(),
        }
    except Exception as exc:  # noqa: BLE001
        out["chatgpt_login"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


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

    The response always carries a ``paths`` block (resolved storage locations +
    writability, WSL W5) and a ``setup`` block (bge-m3 download + ChatGPT login
    state, #14), attached to BOTH the success and failure envelopes so a user
    whose model/keys aren't ready yet can still see where data lives and what
    setup remains.
    """
    paths_block = _resolve_paths_block(workspace_id)
    setup_block = _setup_status_block()
    try:
        result = _preflight.run_preflight(
            workspace_id,
            require_product_context=True,
            warm_up=warm_up,
            warm_up_block=warm_up_block,
        )
        if isinstance(result, dict):
            result.setdefault("paths", paths_block)
            result.setdefault("setup", setup_block)
        return result
    except Exception as exc:
        envelope = envelope_from_exception(exc, stage=Stage.PREFLIGHT)
        envelope["paths"] = paths_block
        envelope["setup"] = setup_block
        return envelope
