"""sync_product_sources MCP tool handler — WSL W2 (10th tool).

Indexes a workspace's raw source library (PDF / MD / TXT / CSV) into the
``product_sources_{workspace_id}`` Chroma collection via ``sources.indexer``.
This collection is SEPARATE from the capability-card collection and is read only
by drafting (W3) + rationale provenance (W4); it never feeds a score.

Module-reference imports for monkeypatch safety. Cold-start safe at module top
(indexer / providers / paths are all cold; heavy deps stay lazy behind the
injected embedding + vectorstore providers).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from event_intel.errors import ErrorCode, MCPError, Stage, envelope_from_exception
from event_intel.providers import embedding as _embedding
from event_intel.providers import vectorstore as _vectorstore
from event_intel.runtime import paths as _paths
from event_intel.runtime import preflight as _preflight
from event_intel.sources import indexer as _indexer
from event_intel.storage.identifiers import sanitize_slug

_VALID_KINDS = ("all", "product", "company")


def _resolve_sources_dir(
    rp: _paths.ResolvedPaths, workspace_id: str, source_dir: str | None, kind: str
) -> Path:
    if source_dir:
        return Path(source_dir).expanduser()
    if kind == "all":
        return rp.sources_root(workspace_id)
    return rp.sources_dir(workspace_id, kind)


def sync_product_sources(
    *,
    workspace_id: str = "default",
    source_dir: str | None = None,
    kind: str = "all",
) -> dict:
    """Incrementally index the workspace source library into product_sources_{ws}.

    ``kind`` selects which subtree to scan: ``all`` (default — both
    sources/product + sources/company), ``product``, or ``company``. ``source_dir``
    overrides the resolved location entirely. Returns the indexer summary
    (file/chunk counts, warnings, partial flag, collection + manifest paths) on
    success, or an MCPError envelope on failure.
    """
    try:
        sanitize_slug(workspace_id, field_name="workspace_id")
        if kind not in _VALID_KINDS:
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT,
                stage=Stage.INGEST,
                message=f"invalid kind {kind!r}",
                hint={"field": "kind", "allowed": list(_VALID_KINDS)},
            )

        config = _preflight.load_config()

        # Lightweight preflight: bge-m3 cached + chroma writable + config. NOT
        # product_context (the source collection is independent of the cards one).
        _preflight.run_preflight(
            workspace_id,
            require_product_context=False,
            config=config,
        )

        rp = _paths.resolve_paths(config)
        sources_dir = _resolve_sources_dir(rp, workspace_id, source_dir, kind)
        manifest_path = rp.source_index_manifest(workspace_id)

        result = _indexer.sync_sources(
            sources_dir=sources_dir,
            workspace_id=workspace_id,
            embedding_provider=_embedding.BgeM3Provider(),
            vectorstore_provider=_vectorstore.ChromaProvider(config=config),
            manifest_path=manifest_path,
            now_iso=datetime.now(UTC).isoformat(),
        )

        result["sources_dir"] = str(sources_dir)
        result["kind"] = kind
        if result.get("total_files", 0) == 0:
            # Empty is a legitimate state (no sources placed yet) — surface where
            # to drop files rather than failing.
            result.setdefault("warnings", []).append(
                f"no indexable PDF/MD/TXT/CSV found under {sources_dir} "
                "(place product source documents there and re-run)"
            )
        return result
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.INGEST)
