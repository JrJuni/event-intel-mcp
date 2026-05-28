"""Explicit bge-m3 model preparation. Heavy import is local to functions here.

NEVER call these from MCP tool handler import path. CLI-only or test-only.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from event_intel.errors import ErrorCode, MCPError, Stage


def _resolve_cache_dir(cache_dir: str | Path | None) -> Path:
    if cache_dir:
        return Path(cache_dir).expanduser()
    env = os.environ.get("HF_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".cache" / "huggingface"


def prepare_bge_m3(*, cache_dir: str | Path | None = None) -> dict[str, Any]:
    """Download bge-m3 weights and run a smoke encode.

    Returns a dict with download status. Raises MCPError on failure.
    Safe-to-call multiple times — sentence_transformers reuses cached weights.
    """
    cache = _resolve_cache_dir(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer("BAAI/bge-m3", cache_folder=str(cache))
        # Smoke encode confirms weights are usable end-to-end.
        _ = model.encode(["hello"], normalize_embeddings=True, show_progress_bar=False)
    except Exception as exc:  # network error, disk full, torch ABI mismatch, ...
        raise MCPError(
            error_code=ErrorCode.MODEL_NOT_READY,
            stage=Stage.PREFLIGHT,
            message=f"Failed to prepare bge-m3: {type(exc).__name__}: {exc}",
            hint={
                "fix": (
                    "Check network connectivity and disk space, then retry. "
                    "If torch import fails, reinstall via the official PyTorch wheel."
                ),
                "cache_dir": str(cache),
            },
            retryable=True,
        ) from exc

    # Re-verify via the same cache-lookup the provider uses at runtime.
    from event_intel.providers.embedding import BgeM3Provider

    status = BgeM3Provider(cache_dir=cache).is_ready()
    return {"ok": True, "model": "BAAI/bge-m3", **status}


def verify_bge_m3(*, cache_dir: str | Path | None = None) -> dict[str, Any]:
    """Check whether bge-m3 weights are cached. Does NOT load the model."""
    from event_intel.providers.embedding import BgeM3Provider

    return BgeM3Provider(cache_dir=_resolve_cache_dir(cache_dir)).is_ready()
