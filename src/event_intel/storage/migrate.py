"""Workspace storage migration — WSL W5.

W0 changed only the **workspace root** default: a fresh install now uses
``~/EventIntel`` while an existing checkout keeps using ``<repo>/outputs`` via a
read-only back-compat fallback. This module copies the legacy workspace tree to
the new location so the fallback can eventually be retired.

Scope: workspace files only (capability cards, drafts, event reports). The data
root (``~/.event-intel``: Chroma / artifacts / cache / resume / source-index) did
NOT move — its default is unchanged — so Chroma collections are NOT touched and
no server shutdown is required.

Safety contract:
  - **copy → checksum-verify → confirm at destination**; the source is NEVER
    deleted (the migration is additive; the user removes the old tree manually
    once satisfied).
  - identical destination file (same sha256) → skipped.
  - destination exists with DIFFERENT content → a conflict; left untouched (never
    overwritten).
  - symlinks are not followed.

stdlib-only (cold-import safe).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from event_intel.runtime import paths as _paths

_IGNORED_NAMES = frozenset({".gitkeep"})


def default_migration_roots(
    *,
    env: dict[str, str] | None = None,
    home: Path | None = None,
    repo_root: Path | None = None,
) -> tuple[Path, Path]:
    """(legacy_src, new_dst) for the default workspace migration.

    src = the legacy ``<repo>/outputs`` tree; dst = the new default
    ``~/EventIntel`` (or the EVENT_INTEL_WORKSPACE_DIR / EVENT_INTEL_OUTPUT_DIR
    override if the user set one — in which case migration is usually a no-op).
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    rp = _paths.resolve_paths(env=env, home=home, repo_root=repo_root)
    src = rp.legacy_output_root
    override = (env.get(_paths.ENV_WORKSPACE_DIR) or env.get(_paths.ENV_OUTPUT_DIR) or "").strip()
    dst = Path(override).expanduser() if override else home / "EventIntel"
    return src, dst


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class MigrationPlan:
    src_root: Path
    dst_root: Path
    copies: list[tuple[Path, Path]] = field(default_factory=list)  # (src, dst)
    skipped_identical: list[Path] = field(default_factory=list)
    conflicts: list[Path] = field(default_factory=list)  # dst differs — NOT overwritten
    total_copy_bytes: int = 0

    def summary(self) -> dict:
        return {
            "src_root": str(self.src_root),
            "dst_root": str(self.dst_root),
            "to_copy": len(self.copies),
            "skipped_identical": len(self.skipped_identical),
            "conflicts": [str(p) for p in self.conflicts],
            "total_copy_bytes": self.total_copy_bytes,
            "nothing_to_do": not self.copies and not self.conflicts,
        }


def plan_migration(*, src_root: Path, dst_root: Path) -> MigrationPlan:
    """Classify every file under ``src_root`` against ``dst_root`` (read-only)."""
    src_root = Path(src_root).expanduser()
    dst_root = Path(dst_root).expanduser()
    plan = MigrationPlan(src_root=src_root, dst_root=dst_root)
    if not src_root.is_dir() or src_root.resolve() == dst_root.resolve():
        return plan
    for root, dirs, files in os.walk(src_root, followlinks=False):
        dirs.sort()
        for fn in sorted(files):
            if fn in _IGNORED_NAMES:
                continue
            src = Path(root) / fn
            if src.is_symlink():
                continue
            rel = src.relative_to(src_root)
            dst = dst_root / rel
            if dst.exists():
                try:
                    same = _sha256(src) == _sha256(dst)
                except OSError:
                    same = False
                if same:
                    plan.skipped_identical.append(rel)
                else:
                    plan.conflicts.append(rel)
            else:
                plan.copies.append((src, dst))
                try:
                    plan.total_copy_bytes += src.stat().st_size
                except OSError:
                    pass
    return plan


def apply_migration(plan: MigrationPlan) -> dict:
    """Execute the copies in ``plan`` (atomic temp+rename per file + sha verify).

    Never deletes the source; never overwrites a conflict. Returns a result dict.
    """
    copied: list[str] = []
    verify_failures: list[str] = []
    for src, dst in plan.copies:
        rel = src.relative_to(plan.src_root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=".migrate.")
        os.close(fd)
        try:
            shutil.copy2(src, tmp)
            if _sha256(Path(tmp)) != _sha256(src):
                verify_failures.append(str(rel))
                os.unlink(tmp)
                continue
            Path(tmp).replace(dst)  # atomic; dst did not exist (planned copy)
            copied.append(str(rel))
        except OSError as exc:
            verify_failures.append(f"{rel}: {exc}")
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return {
        "ok": not verify_failures,
        "src_root": str(plan.src_root),
        "dst_root": str(plan.dst_root),
        "copied": len(copied),
        "skipped_identical": len(plan.skipped_identical),
        "conflicts": [str(p) for p in plan.conflicts],
        "verify_failures": verify_failures,
        "source_preserved": True,  # we never delete the source tree
    }
