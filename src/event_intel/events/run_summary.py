"""Run-summary emitter — Y1 benchmark CS1.

Every build emits a reproducibility/audit record alongside its outputs. Two
distinct identifiers (design v4 §CS1, review R1#8 / R2-4 / R3-6):

  run_id          — UNIQUE per attempt (uuid). Guarantees a re-run never
                    overwrites a prior run's immutable directory.
  run_fingerprint — DETERMINISTIC hash of inputs/code/config. Same inputs →
                    same fingerprint, so two attempts of an identical setup are
                    detectably equivalent. A deterministic *id* would collide and
                    break immutability — hence the two are separate fields.

`content_fingerprint` of the Chroma collection is produced by the ingest receipt
(CS7) and folded in here as `cards_fingerprint`; a file hash is the CS1 stand-in.

Pure stdlib (hashlib / json / subprocess / uuid) — import-cold, no heavy ML.
Regression-guarded by tests/test_mcp_cold_start.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def sha256_file(path: str | Path | None) -> str | None:
    """sha256 of a file's bytes, or None if the path is missing/unreadable."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _repo_root() -> Path:
    # src/event_intel/events/run_summary.py → parents[3] == <repo>
    return Path(__file__).resolve().parents[3]


def git_commit_sha() -> str:
    """Current HEAD sha, or 'unknown' off-repo / on failure (never raises)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(_repo_root()), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        sha = out.stdout.strip()
        return sha or "unknown"
    except Exception:
        return "unknown"


def new_run_id(*, slug: str, now_iso: str) -> str:
    """Unique attempt id: <slug>-<compact-ts>-<rand>. Collision-free per run."""
    compact = "".join(c for c in now_iso if c.isdigit())[:14]
    return f"{slug}-{compact}-{uuid.uuid4().hex[:8]}"


def config_hash(config: dict[str, Any]) -> str:
    return sha256_text(json.dumps(config, sort_keys=True, ensure_ascii=False, default=str))


def compute_run_fingerprint(
    *,
    git_sha: str,
    cards_fingerprint: str | None,
    config_fp: str,
    source_sha256: str | None,
    caps: dict[str, Any],
    target_mode: str,
    model_ids: dict[str, str],
) -> str:
    """Deterministic fingerprint of everything that decides a run's outputs.

    Excludes run_id, timestamps, and any per-attempt nonce — identical inputs
    must yield an identical fingerprint (R2-4 / R3-6).
    """
    payload = {
        "git_sha": git_sha,
        "cards_fingerprint": cards_fingerprint,
        "config_fp": config_fp,
        "source_sha256": source_sha256,
        "caps": {k: caps[k] for k in sorted(caps)},
        "target_mode": target_mode,
        "model_ids": {k: model_ids[k] for k in sorted(model_ids)},
    }
    return sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str))


@dataclass
class StageStatus:
    stage: str
    ok: bool
    error_code: str | None = None


@dataclass
class CompanyScore:
    name: str
    tier: str
    final_score: float
    evidence_floor: int
    dimensions: dict[str, float]
    tier_reasons: list[str]


@dataclass
class RunSummary:
    run_id: str
    run_fingerprint: str
    git_commit_sha: str
    config_fp: str
    cards_fingerprint: str | None
    source_sha256: str | None
    provider: str
    model_ids: dict[str, str]
    reference_timestamp: str
    target_mode: str
    max_companies: int | None
    max_chunks_per_event: int | None
    refresh: bool
    cache_hits: int
    cache_misses: int
    skipped_from_resume: int
    search_calls: int
    extracted: int
    enriched: int
    scored: int
    extraction_coverage: float | None  # CS2 fills; None until then
    stages: list[StageStatus] = field(default_factory=list)
    companies: list[CompanyScore] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pair: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def write_run_summary(
    summary: RunSummary, run_dir: str | Path, *, allow_overwrite: bool = False
) -> Path:
    """Atomically write run_summary.json into run_dir.

    Refuses to overwrite an existing run_summary.json unless allow_overwrite is
    set — the benchmark runner (CS4) uses a unique run_id directory so the
    default-False guard enforces immutability; the production build hook passes
    allow_overwrite=True for its date-granular output dir.
    """
    d = Path(run_dir)
    d.mkdir(parents=True, exist_ok=True)
    target = d / "run_summary.json"
    if target.exists() and not allow_overwrite:
        raise FileExistsError(f"run_summary.json already exists (immutable run): {target}")
    data = json.dumps(summary.to_dict(), ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".run_summary.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        Path(tmp).replace(target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target
