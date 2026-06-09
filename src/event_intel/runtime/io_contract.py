"""Tool input contract — Y2.1b (Remote I/O).

A tool that takes a file can now receive it three ways: ``*_path`` (server-local,
**personal-local compatibility only**), ``*_content`` (inline, small), or
``*_artifact_id`` (uploaded via the artifact registry, workspace-scoped). Exactly
ONE must be given — providing two is a silent-client-bug trap, so it's an
explicit ``INVALID_INPUT`` (no priority fallback).

To keep existing path-based readers/validators untouched, content/artifact inputs
are **materialized to a short-lived temp file** server-side and the existing
path code runs against it (the temp is cleaned on exit). The *client-facing*
contract is what matters; the server-side temp is an implementation detail.

stdlib + cold modules only (errors / paths / artifact_registry) → cold-import safe.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from event_intel.errors import ErrorCode, MCPError, Stage
from event_intel.runtime import paths as _paths
from event_intel.storage import artifact_registry as _registry

INLINE_CONTENT_MAX_BYTES = _registry.INLINE_CONTENT_MAX_BYTES


@contextmanager
def materialize_input(
    *,
    workspace_id: str,
    field: str,
    content: str | bytes | None = None,
    artifact_id: str | None = None,
    path: str | None = None,
    suffix: str = "",
    config: dict | None = None,
    stage: Stage = Stage.INGEST,
) -> Iterator[Path]:
    """Yield a concrete filesystem Path for the resolved input.

    Exactly one of ``content`` / ``artifact_id`` / ``path`` (truthy) must be set.
    ``path`` is yielded as-is (no temp, no cleanup — personal-local lane).
    ``content`` (≤ inline cap) / ``artifact_id`` are written to a temp file that
    is removed on context exit. Raises ``INVALID_INPUT`` on 0 / >1 inputs, oversize
    inline content, or a missing artifact.
    """
    provided = [bool(content), bool(artifact_id), bool(path)]
    n = sum(provided)
    opts = f"{field} | {field}_content | {field}_artifact_id"
    if n == 0:
        raise MCPError(
            error_code=ErrorCode.INVALID_INPUT, stage=stage,
            message=f"one of {opts} is required",
            hint={"field": field, "provide_exactly_one": [field, f"{field}_content", f"{field}_artifact_id"]},
        )
    if n > 1:
        raise MCPError(
            error_code=ErrorCode.INVALID_INPUT, stage=stage,
            message=f"provide exactly one of {opts} (got {n})",
            hint={"field": field, "rule": "mutually exclusive — no priority fallback"},
        )

    if path:
        # personal-local compatibility lane — not a remote contract.
        yield Path(path).expanduser()
        return

    if content:
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        if len(data) > INLINE_CONTENT_MAX_BYTES:
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT, stage=stage,
                message=(
                    f"{field}_content is {len(data)} bytes > inline cap "
                    f"{INLINE_CONTENT_MAX_BYTES}; upload via put_artifact and pass "
                    f"{field}_artifact_id"
                ),
                hint={"field": f"{field}_content", "inline_max_bytes": INLINE_CONTENT_MAX_BYTES},
            )
    else:
        data = _registry.get_artifact(
            workspace_id=workspace_id, artifact_id=artifact_id, config=config
        )
        if data is None:
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT, stage=stage,
                message=f"{field}_artifact_id not found in workspace (expired or wrong id)",
                hint={"field": f"{field}_artifact_id", "artifact_id": artifact_id},
            )

    inbox = _paths.resolve_paths(config).cache_dir / "io_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=inbox, suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        yield Path(tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def input_source_label(
    *, content: str | bytes | None = None, artifact_id: str | None = None, path: str | None = None
) -> str:
    """Short provenance label for the resolved input (for response/audit)."""
    if path:
        return "path"
    if content:
        return "content"
    if artifact_id:
        return f"artifact:{artifact_id}"
    return "none"
