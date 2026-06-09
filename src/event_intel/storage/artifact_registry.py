"""Workspace-scoped artifact registry — Y2.1a (Remote I/O foundation).

Stores opaque content blobs so tools can take/return data by **id** instead of a
server-local filesystem path — the prerequisite for ever serving this over a
network (Y2.2). Until then it runs entirely locally (stdio); this slice only adds
the storage primitive, no network, no tool exposure.

Design (locked by 2 rounds of packet review):
  - **artifact_id is an opaque random token, NOT a content hash.** A
    content-addressed id doubles as a capability token: guessable, an
    existence-oracle, and a cross-workspace leak. `content_sha256` is kept as
    dedupe/checksum *metadata* only.
  - **workspace-scoped**: get/gc only ever look inside one workspace's dir, so a
    valid id from workspace A cannot read workspace B.
  - **TTL** + `gc()`; a `pinned` flag lets a job keep its result artifact alive
    past TTL (Y2.1c TTL-coupling).
  - **size cap** on upload; the small *inline* cap (for `*_content` tool params)
    is a separate constant used at the tool boundary (Y2.1b).

stdlib-only (cold-import safe). Atomic temp+rename writes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

from event_intel.runtime import paths as _paths

# Upload cap (large-content escape hatch). Mirrors the source-indexer per-file cap.
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024  # 25 MiB
# Inline cap for `*_content` tool params (Y2.1b enforces); larger → put_artifact.
INLINE_CONTENT_MAX_BYTES = 256 * 1024  # 256 KiB

_ID_NBYTES = 24  # secrets.token_urlsafe(24) → ~32 url-safe chars, unguessable
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")  # token_urlsafe alphabet; guards path traversal


class ArtifactTooLarge(Exception):
    """Upload exceeded MAX_ARTIFACT_BYTES."""


def _registry_dir(workspace_id: str, config: dict | None) -> Path:
    return _paths.resolve_paths(config).artifacts_root / "_registry" / workspace_id


def _new_artifact_id() -> str:
    return secrets.token_urlsafe(_ID_NBYTES)


def _valid_id(artifact_id: str) -> bool:
    return bool(artifact_id) and bool(_ID_RE.match(artifact_id))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        Path(tmp).replace(path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def put_artifact(
    *,
    workspace_id: str,
    content: str | bytes,
    config: dict | None = None,
    suffix: str = "",
    ttl_seconds: int | None = None,
    max_bytes: int = MAX_ARTIFACT_BYTES,
    now: float | None = None,
) -> dict:
    """Store ``content`` and return ``{artifact_id, content_sha256, size}``.

    ``artifact_id`` is opaque/random. Raises ``ArtifactTooLarge`` over ``max_bytes``.
    """
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    if len(data) > max_bytes:
        raise ArtifactTooLarge(f"{len(data)} bytes > max_bytes={max_bytes}")
    now = time.time() if now is None else now
    artifact_id = _new_artifact_id()
    sha = hashlib.sha256(data).hexdigest()
    d = _registry_dir(workspace_id, config)
    meta = {
        "artifact_id": artifact_id,
        "content_sha256": sha,
        "size": len(data),
        "suffix": suffix,
        "created_at": now,
        "ttl_seconds": ttl_seconds,
        "pinned": False,
    }
    # Content first, then meta — meta presence = a complete artifact.
    _atomic_write(d / f"{artifact_id}.bin", data)
    _atomic_write(d / f"{artifact_id}.json", json.dumps(meta, ensure_ascii=False).encode("utf-8"))
    return {"artifact_id": artifact_id, "content_sha256": sha, "size": len(data)}


def get_artifact_meta(
    *, workspace_id: str, artifact_id: str, config: dict | None = None
) -> dict | None:
    """Metadata for an artifact, or None if absent/invalid. Workspace-scoped."""
    if not _valid_id(artifact_id):
        return None
    p = _registry_dir(workspace_id, config) / f"{artifact_id}.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _is_expired(meta: dict, now: float) -> bool:
    if meta.get("pinned"):
        return False
    ttl = meta.get("ttl_seconds")
    if not ttl:
        return False
    created = meta.get("created_at") or 0
    return now > created + ttl


def get_artifact(
    *,
    workspace_id: str,
    artifact_id: str,
    config: dict | None = None,
    now: float | None = None,
) -> bytes | None:
    """Return artifact bytes, or None if absent/invalid/expired. Workspace-scoped
    (only this workspace's dir is consulted — a valid id from another workspace
    cannot read here).
    """
    meta = get_artifact_meta(workspace_id=workspace_id, artifact_id=artifact_id, config=config)
    if meta is None:
        return None
    if _is_expired(meta, time.time() if now is None else now):
        return None
    p = _registry_dir(workspace_id, config) / f"{artifact_id}.bin"
    if not p.is_file():
        return None
    try:
        return p.read_bytes()
    except OSError:
        return None


def set_pinned(
    *, workspace_id: str, artifact_id: str, pinned: bool, config: dict | None = None
) -> bool:
    """Pin/unpin an artifact (a job pins its result artifacts to its own TTL —
    Y2.1c TTL-coupling). Returns True if the artifact existed.
    """
    meta = get_artifact_meta(workspace_id=workspace_id, artifact_id=artifact_id, config=config)
    if meta is None:
        return False
    meta["pinned"] = bool(pinned)
    _atomic_write(
        _registry_dir(workspace_id, config) / f"{artifact_id}.json",
        json.dumps(meta, ensure_ascii=False).encode("utf-8"),
    )
    return True


def gc(*, workspace_id: str, config: dict | None = None, now: float | None = None) -> int:
    """Delete expired (unpinned) artifacts. Returns the count removed."""
    d = _registry_dir(workspace_id, config)
    if not d.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for meta_path in sorted(d.glob("*.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _is_expired(meta, now):
            for p in (meta_path, d / f"{meta.get('artifact_id')}.bin"):
                try:
                    p.unlink()
                except OSError:
                    pass
            removed += 1
    return removed
