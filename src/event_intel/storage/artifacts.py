"""Artifact store for the Phase 18T acquisition layer.

Each acquired source is stored as a single raw file + a sibling manifest.json
under `~/.event-intel/artifacts/{workspace_id}/{event_slug}/`.

The manifest carries enough metadata to short-circuit re-acquisition:
  {verdict, source_kind, source_ref, fetched_at, sha256, url, content_type,
   status, http_pages}

Cache lookup reads the manifest (not file existence) because the verdict and
source_kind are not inferable from the file extension alone.

`EVENT_INTEL_ARTIFACTS_DIR` env var overrides the base directory.

All file writes use atomic temp+rename to avoid partial-write artifacts.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from event_intel.runtime import paths as _paths


def _base_dir() -> Path:
    """Artifacts root — delegated to the central resolver (see runtime.paths).

    EVENT_INTEL_ARTIFACTS_DIR (env) wins, then config.paths.artifacts_dir, then
    ~/.event-intel/artifacts. (config is not loaded here — the artifact store is
    config-free; an env override or the default is sufficient.)
    """
    return _paths.resolve_paths().artifacts_root


def artifact_dir(*, workspace_id: str, event_slug: str) -> Path:
    """Return (and create) the per-event artifact directory."""
    d = _base_dir() / workspace_id / event_slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_artifact(dir: Path, basename: str, body: str | bytes) -> Path:
    """Atomically write `body` to `dir/basename`. Returns the final path."""
    dir.mkdir(parents=True, exist_ok=True)
    target = dir / basename
    data = body.encode("utf-8") if isinstance(body, str) else body
    fd, tmp = tempfile.mkstemp(dir=dir, prefix=f".{basename}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        Path(tmp).replace(target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def write_manifest(dir: Path, manifest: dict[str, Any]) -> Path:
    """Atomically write manifest.json."""
    return write_artifact(dir, "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))


@dataclass
class ManifestModel:
    verdict: str
    source_kind: str
    source_ref: str
    fetched_at: str
    sha256: str
    url: str
    content_type: str
    status: int
    http_pages: int
    # C7 ladder provenance — optional so pre-ladder manifests still load (M9).
    selected_rung: str | None = None
    winning_request: dict | None = None
    analysis_fp: str = ""
    config_fp: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> ManifestModel:
        return cls(
            verdict=d["verdict"],
            source_kind=d["source_kind"],
            source_ref=d["source_ref"],
            fetched_at=d["fetched_at"],
            sha256=d["sha256"],
            url=d["url"],
            content_type=d.get("content_type", ""),
            status=int(d.get("status", 0)),
            http_pages=int(d.get("http_pages", 1)),
            selected_rung=d.get("selected_rung"),
            winning_request=d.get("winning_request"),
            analysis_fp=d.get("analysis_fp", ""),
            config_fp=d.get("config_fp", ""),
        )


def read_manifest(dir: Path) -> ManifestModel | None:
    """Return the manifest if it exists and is valid JSON; else None."""
    path = dir / "manifest.json"
    if not path.is_file():
        return None
    try:
        return ManifestModel.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_artifact_sha256(path: Path, expected: str) -> bool:
    """Return True if the file exists and its sha256 matches expected."""
    if not path.is_file():
        return False
    return sha256_of(path) == expected


def make_manifest(
    *,
    verdict: str,
    source_kind: str,
    source_ref: str,
    url: str,
    content_type: str,
    status: int,
    http_pages: int,
    artifact_path: Path,
    selected_rung: str | None = None,
    winning_request: dict | None = None,
    analysis_fp: str = "",
    config_fp: str = "",
) -> dict[str, Any]:
    """Build a manifest dict from a freshly-written artifact.

    The C7 provenance fields (selected_rung / winning_request / analysis_fp /
    config_fp) are only emitted when supplied, so a manifest stays minimal when
    written outside the ladder.
    """
    manifest: dict[str, Any] = {
        "verdict": verdict,
        "source_kind": source_kind,
        "source_ref": str(source_ref),
        "fetched_at": datetime.now(UTC).isoformat(),
        "sha256": sha256_of(artifact_path),
        "url": url,
        "content_type": content_type,
        "status": status,
        "http_pages": http_pages,
    }
    if selected_rung is not None:
        manifest["selected_rung"] = selected_rung
    if winning_request is not None:
        manifest["winning_request"] = winning_request
    if analysis_fp:
        manifest["analysis_fp"] = analysis_fp
    if config_fp:
        manifest["config_fp"] = config_fp
    return manifest
