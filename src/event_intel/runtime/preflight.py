"""Runtime preflight — 5-check orchestrator. See plan v0.5 §S1 + check_runtime tool.

NEVER imports torch / chromadb / sentence_transformers at module top. Heavy work
is deferred to provider methods, which themselves use lazy imports.

The orchestrator can be called with injected fake providers (tests) or with the
defaults wired from `event_intel.providers.*`.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from event_intel.errors import ErrorCode, MCPError, Stage
from event_intel.providers import embedding as _embedding
from event_intel.providers import llm as _llm
from event_intel.providers import search as _search
from event_intel.providers import vectorstore as _vectorstore
from event_intel.storage.identifiers import sanitize_slug

if TYPE_CHECKING:
    from event_intel.providers.embedding import EmbeddingProvider
    from event_intel.providers.llm import LLMProvider
    from event_intel.providers.search import SearchProvider
    from event_intel.providers.vectorstore import VectorStoreProvider


def _validate_workspace_id_minimal(workspace_id: str) -> None:
    """Back-compat shim — delegates to `storage.identifiers.sanitize_slug`.

    Kept so existing call sites (draft / ingest tool handlers) don't need to
    update in lockstep. New code should call `sanitize_slug` directly.
    """
    sanitize_slug(workspace_id, field_name="workspace_id")


# Required nested keys in defaults.yaml. Surfaced as CONFIG_ERROR with a dotted path.
_REQUIRED_CONFIG_KEYS: tuple[tuple[str, ...], ...] = (
    ("schema_version",),
    ("llm", "draft_cards_model"),
    ("extraction", "max_chunks_per_event"),
    ("extraction", "source_snippet_min_chars"),
    ("scoring", "weights", "capability_fit"),
    ("scoring", "tier_rules", "S", "min_final_score"),
    ("paths", "chroma_dir"),
)


def _default_config_path() -> Path:
    env_override = os.environ.get("EVENT_INTEL_CONFIG")
    if env_override:
        return Path(env_override).expanduser()
    # Repo-shipped defaults: <repo_root>/config/defaults.yaml
    return Path(__file__).resolve().parents[3] / "config" / "defaults.yaml"


def load_config(path: Path | None = None) -> dict:
    """Load defaults.yaml and enforce that every required key is present.

    Raises MCPError(CONFIG_ERROR) with a path-localized hint on any miss.
    """
    config_path = (path or _default_config_path()).expanduser()
    try:
        with config_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError as exc:
        raise MCPError(
            error_code=ErrorCode.CONFIG_ERROR,
            stage=Stage.PREFLIGHT,
            message=f"config file not found at {config_path}",
            hint={
                "expected_path": str(config_path),
                "fix": "Restore config/defaults.yaml or set EVENT_INTEL_CONFIG",
            },
            retryable=False,
        ) from exc
    except yaml.YAMLError as exc:
        raise MCPError(
            error_code=ErrorCode.CONFIG_ERROR,
            stage=Stage.PREFLIGHT,
            message=f"config file at {config_path} is not valid YAML: {exc}",
            hint={"expected_path": str(config_path)},
            retryable=False,
        ) from exc

    for key_path in _REQUIRED_CONFIG_KEYS:
        cursor: object = data
        for key in key_path:
            if not isinstance(cursor, dict) or key not in cursor:
                dotted = ".".join(key_path)
                raise MCPError(
                    error_code=ErrorCode.CONFIG_ERROR,
                    stage=Stage.PREFLIGHT,
                    message=f"missing required config key: {dotted}",
                    hint={
                        "expected_path": str(config_path),
                        "missing_key": dotted,
                        "fix": f"Add `{dotted}` to {config_path}",
                    },
                    retryable=False,
                )
            cursor = cursor[key]

    return data


def _product_collection_name(workspace_id: str) -> str:
    return f"product_{workspace_id}"


def run_preflight(
    workspace_id: str = "default",
    *,
    require_product_context: bool = True,
    config: dict | None = None,
    embedding_provider: "EmbeddingProvider | None" = None,
    vectorstore_provider: "VectorStoreProvider | None" = None,
    llm_provider: "LLMProvider | None" = None,
    search_provider: "SearchProvider | None" = None,
) -> dict:
    """Run 5 preflight checks. Returns the success envelope on full pass.

    On the first hard failure, raises MCPError with the most specific error_code.
    Callers at the MCP tool boundary catch and render via `.to_envelope()`.

    Parameters
    ----------
    require_product_context : bool
        True for `check_runtime` and `build_event_tier_list` preflight (must be
        ingested before use). False for `ingest_product_context` preflight (the
        whole point of that call is to create the collection).
    """
    _validate_workspace_id_minimal(workspace_id)

    if config is None:
        config = load_config()

    # Module-reference resolution (NOT `from X import Y`): keeps the test-time
    # monkeypatch of provider classes alive across cold-start fixture purges
    # (see docs/lesson-learned.md S4 class-identity drift entry).
    if embedding_provider is None:
        embedding_provider = _embedding.BgeM3Provider()
    if vectorstore_provider is None:
        vectorstore_provider = _vectorstore.ChromaProvider()
    if llm_provider is None:
        llm_provider = _llm.AnthropicProvider(model=config["llm"]["draft_cards_model"])
    if search_provider is None:
        search_provider = _search.BraveSearchProvider()

    start = time.monotonic()
    checks: dict[str, dict] = {}

    # 1. Embedding model cache
    emb_status = embedding_provider.is_ready()
    if emb_status.get("status") != "ready":
        raise MCPError(
            error_code=ErrorCode.MODEL_NOT_READY,
            stage=Stage.PREFLIGHT,
            message=(
                f"bge-m3 weights not found at {emb_status.get('path', '?')}"
            ),
            hint={
                "fix": "Run `event-intel models prepare` once before using ingest/build tools",
                "detail": emb_status,
            },
            retryable=False,
        )
    checks["embedding_model"] = emb_status

    # 2. Vector store writability
    vs_status = vectorstore_provider.ensure_writable()
    if vs_status.get("status") != "writable":
        raise MCPError(
            error_code=ErrorCode.IO_ERROR,
            stage=Stage.PREFLIGHT,
            message=f"vector store path is not writable: {vs_status.get('path', '?')}",
            hint={"detail": vs_status},
            retryable=False,
        )
    checks["vectorstore"] = vs_status

    # 3. Anthropic key
    llm_status = llm_provider.ping()
    if llm_status.get("status") != "ok":
        raise MCPError(
            error_code=ErrorCode.CONFIG_ERROR,
            stage=Stage.PREFLIGHT,
            message="ANTHROPIC_API_KEY missing or invalid",
            hint={
                "fix": "Set ANTHROPIC_API_KEY in .env (see .env.example)",
                "detail": llm_status,
            },
            retryable=False,
        )
    checks["anthropic_api"] = llm_status

    # 4. Brave key. remaining_quota may be None when Brave omits the header (R3-#4).
    brave_status = search_provider.ping()
    if brave_status.get("status") == "missing_key":
        raise MCPError(
            error_code=ErrorCode.CONFIG_ERROR,
            stage=Stage.PREFLIGHT,
            message="BRAVE_API_KEY missing",
            hint={
                "fix": "Set BRAVE_API_KEY in .env (see .env.example)",
                "detail": brave_status,
            },
            retryable=False,
        )
    if brave_status.get("status") not in {"ok", "missing_key"}:
        # ping() returned error — surface as UPSTREAM_ERROR; quota null still ok.
        raise MCPError(
            error_code=ErrorCode.UPSTREAM_ERROR,
            stage=Stage.PREFLIGHT,
            message=f"Brave search ping failed: {brave_status.get('error', 'unknown')}",
            hint={"detail": brave_status},
            retryable=True,
        )
    checks["brave_api"] = {
        "status": "ok",
        "remaining_quota": brave_status.get("remaining_quota"),
    }

    # 5. Product context collection (R3-#1)
    collection = _product_collection_name(workspace_id)
    pc_info = vectorstore_provider.collection_info(collection)
    pc_ready = bool(pc_info.get("exists")) and int(pc_info.get("count", 0)) >= 1
    if require_product_context and not pc_ready:
        raise MCPError(
            error_code=ErrorCode.PRODUCT_CONTEXT_MISSING,
            stage=Stage.PREFLIGHT,
            message=(
                f"product context collection '{collection}' has not been ingested"
            ),
            hint={
                "fix": (
                    f"event-intel ingest --cards <path> --workspace {workspace_id}"
                ),
                "collection": collection,
            },
            retryable=False,
        )
    checks["product_context"] = {
        "status": "ready" if pc_ready else "missing",
        "collection": collection,
        "chunks": int(pc_info.get("count", 0)),
    }

    elapsed_ms = int((time.monotonic() - start) * 1000)
    return {"ok": True, "checks": checks, "elapsed_ms": elapsed_ms}
