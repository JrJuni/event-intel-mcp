"""Central path resolver — the single source of truth for *where things live*.

Before this module, three call sites resolved storage paths independently and
inconsistently:
  - `tools.build_event_tier_list._outputs_base()` — `<repo>/outputs` (+ env)
  - `storage.artifacts._base_dir()`               — `~/.event-intel/artifacts` (+ env)
  - `providers.vectorstore.ChromaProvider`        — `~/.event-intel/chroma` (env ONLY,
    silently ignoring `config.paths.chroma_dir` that preflight *requires*)

`ResolvedPaths` unifies them. It is **stdlib-only** (cold-import safe — see
`tests/test_mcp_cold_start.py`) and side-effect-free: resolution computes paths
and may *read* the filesystem to decide a back-compat fallback, but never writes
or creates anything. Callers `mkdir` when they actually write.

Two roots:
  - **workspace_root** — user-facing artifacts (per-workspace cards / sources /
    event reports). New default ``~/EventIntel``; on an existing checkout the
    legacy ``<repo>/outputs`` is used instead (back-compat fallback) so already
    ingested cards keep resolving until the W5 ``storage migrate`` step.
  - **data_root** — machine-local RAG + cache state (``~/.event-intel``):
    chroma / artifacts / cache / resume / source-index. Default UNCHANGED.

Resolution precedence per leaf (highest first):
  1. existing fine-grained env (``EVENT_INTEL_OUTPUT_DIR`` / ``_ARTIFACTS_DIR`` /
     ``_CHROMA_DIR``)
  2. new coarse env (``EVENT_INTEL_WORKSPACE_DIR`` / ``EVENT_INTEL_DATA_DIR``)
  3. ``config["paths"][...]``
  4. built-in default (``~/EventIntel`` + ``~/.event-intel``, with the legacy
     fallback for workspace_root)

``config.yaml`` / ``chatgpt_auth.json`` / the HuggingFace cache are NOT routed
through here — they keep their fixed well-known locations.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

# Env var names (kept as constants so callers / tests reference one spelling).
ENV_OUTPUT_DIR = "EVENT_INTEL_OUTPUT_DIR"  # legacy alias for the workspace root
ENV_WORKSPACE_DIR = "EVENT_INTEL_WORKSPACE_DIR"
ENV_DATA_DIR = "EVENT_INTEL_DATA_DIR"
ENV_CHROMA_DIR = "EVENT_INTEL_CHROMA_DIR"
ENV_ARTIFACTS_DIR = "EVENT_INTEL_ARTIFACTS_DIR"

_WORKSPACE_DIRNAME = "EventIntel"  # ~/EventIntel
_DATA_DIRNAME = ".event-intel"  # ~/.event-intel
_LEGACY_OUTPUT_DIRNAME = "outputs"  # <repo>/outputs

# Children that don't count as "real data" when deciding the back-compat fallback.
_IGNORED_PAYLOAD_NAMES = frozenset({".gitkeep"})

# Track which deprecated env vars we've already warned about (avoid log spam when
# resolve_paths is called many times in one process).
_warned_legacy_env: set[str] = set()


def _repo_root() -> Path:
    """<repo> derived from this file's location — cwd-INDEPENDENT.

    ``src/event_intel/runtime/paths.py`` → parents[3] == ``<repo>``.
    """
    return Path(__file__).resolve().parents[3]


def _env_path(env: dict[str, str], name: str) -> Path | None:
    val = env.get(name)
    if val and val.strip():
        return Path(val.strip()).expanduser()
    return None


def _cfg_path(config: dict | None, *keys: str) -> Path | None:
    node: object = config or {}
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    if isinstance(node, str) and node.strip():
        return Path(node.strip()).expanduser()
    return None


def _has_payload(p: Path) -> bool:
    """True if ``p`` is a dir holding anything beyond placeholder/hidden files."""
    if not p.is_dir():
        return False
    try:
        for child in p.iterdir():
            if child.name in _IGNORED_PAYLOAD_NAMES or child.name.startswith("."):
                continue
            return True
    except OSError:
        return False
    return False


def _resolve_workspace_root(
    config: dict | None, env: dict[str, str], home: Path, repo_root: Path
) -> tuple[Path, bool]:
    """Return (workspace_root, is_legacy). See module precedence rules."""
    legacy_env = _env_path(env, ENV_OUTPUT_DIR)
    if legacy_env is not None:
        if ENV_OUTPUT_DIR not in _warned_legacy_env:
            _warned_legacy_env.add(ENV_OUTPUT_DIR)
            _log.warning(
                "%s is the legacy workspace-root env; prefer %s. Honoring it for now.",
                ENV_OUTPUT_DIR,
                ENV_WORKSPACE_DIR,
            )
        return legacy_env, False

    new_env = _env_path(env, ENV_WORKSPACE_DIR)
    if new_env is not None:
        return new_env, False

    cfg = _cfg_path(config, "paths", "workspace_dir")
    if cfg is not None:
        return cfg, False

    # Default + back-compat fallback. ``<repo>/outputs`` ships a tracked .gitkeep,
    # so on any checkout it exists as a dir — that is exactly the signal that this
    # is an existing install whose cards live there. A fresh end-user install
    # (e.g. an .mcpb bundle with no outputs/ dir) gets the new ~/EventIntel layout.
    new_default = home / _WORKSPACE_DIRNAME
    legacy_default = repo_root / _LEGACY_OUTPUT_DIRNAME
    if _has_payload(new_default):
        return new_default, False
    if legacy_default.is_dir():
        return legacy_default, True
    return new_default, False


def _resolve_data_root(env: dict[str, str], home: Path) -> Path:
    new_env = _env_path(env, ENV_DATA_DIR)
    if new_env is not None:
        return new_env
    return home / _DATA_DIRNAME


@dataclass(frozen=True)
class ResolvedPaths:
    """Resolved absolute storage roots + per-workspace / per-event accessors."""

    workspace_root: Path
    data_root: Path
    chroma_dir: Path
    artifacts_root: Path
    workspace_root_is_legacy: bool
    legacy_output_root: Path

    # --- workspace_root leaves -------------------------------------------------
    def workspace_dir(self, workspace_id: str) -> Path:
        """Per-workspace dir. Holds capability_cards.yaml + event report dirs
        (flat layout, as the current pipeline expects).
        """
        return self.workspace_root / workspace_id

    def sources_dir(self, workspace_id: str, kind: str = "product") -> Path:
        """Raw source library for a workspace. ``kind`` ∈ {product, company}."""
        return self.workspace_root / workspace_id / "sources" / kind

    def cards_dir(self, workspace_id: str) -> Path:
        return self.workspace_root / workspace_id / "cards"

    def events_dir(self, workspace_id: str) -> Path:
        return self.workspace_root / workspace_id / "events"

    # --- data_root leaves ------------------------------------------------------
    def artifact_dir(self, workspace_id: str, event_slug: str) -> Path:
        return self.artifacts_root / workspace_id / event_slug

    @property
    def cache_dir(self) -> Path:
        return self.data_root / "cache"

    @property
    def resume_dir(self) -> Path:
        return self.data_root / "resume"

    @property
    def source_index_root(self) -> Path:
        return self.data_root / "source-index"

    def source_index_manifest(self, workspace_id: str) -> Path:
        return self.source_index_root / workspace_id / "manifest.json"


def resolve_paths(
    config: dict | None = None,
    *,
    env: dict[str, str] | None = None,
    home: Path | None = None,
    repo_root: Path | None = None,
) -> ResolvedPaths:
    """Resolve all storage roots. Pure aside from read-only fallback probing.

    Parameters are injectable for testing; production callers pass at most
    ``config``.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    repo_root = _repo_root() if repo_root is None else repo_root

    data_root = _resolve_data_root(env, home)
    workspace_root, is_legacy = _resolve_workspace_root(config, env, home, repo_root)

    chroma_dir = (
        _env_path(env, ENV_CHROMA_DIR)
        or _cfg_path(config, "paths", "chroma_dir")
        or data_root / "chroma"
    )
    artifacts_root = (
        _env_path(env, ENV_ARTIFACTS_DIR)
        or _cfg_path(config, "paths", "artifacts_dir")
        or data_root / "artifacts"
    )

    return ResolvedPaths(
        workspace_root=workspace_root,
        data_root=data_root,
        chroma_dir=chroma_dir,
        artifacts_root=artifacts_root,
        workspace_root_is_legacy=is_legacy,
        legacy_output_root=repo_root / _LEGACY_OUTPUT_DIRNAME,
    )
