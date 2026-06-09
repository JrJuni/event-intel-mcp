"""ingest_capability_cards MCP tool handler.

Note the file name vs. tool name: the MCP tool surface is `ingest_product_context`
(it ingests cards INTO the product context collection), but the underlying
ingest module + this handler are scoped to cards-as-input. Future ingestion
surfaces (e.g. raw whitepapers) can land alongside without renaming.

Module-reference imports for monkeypatch safety. Cold-start safe at module top.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from event_intel.cards import ingest as _ingest
from event_intel.cards import validator as _validator
from event_intel.errors import Stage, envelope_from_exception
from event_intel.events import run_summary as _run_summary
from event_intel.providers import embedding as _embedding
from event_intel.providers import vectorstore as _vectorstore
from event_intel.runtime import io_contract as _io
from event_intel.runtime import paths as _paths
from event_intel.runtime import preflight as _preflight
from event_intel.sources import indexer as _src_indexer


def ingest_product_context(
    *,
    workspace_id: str = "default",
    cards_path: str = "",
    cards_content: str | None = None,
    cards_artifact_id: str | None = None,
    extra_source_paths: list[str] | None = None,  # reserved for v0.4+ whitepapers
    sync_sources: bool = False,
    force_source_sync: bool = False,
) -> dict:
    """Validate + embed + upsert capability_cards.yaml into product_{workspace_id}.

    Preflight runs with `require_product_context=False` — the whole point of
    this call is to create that collection, so requiring it would deadlock.

    `sync_sources` (opt-in, default False = unchanged behavior): also index the
    workspace source library into product_sources_{ws} (WSL W4). Order is
    validate cards → sync sources → ingest cards, and a PARTIAL source sync
    aborts before the card collection is touched (the card collection is never
    left half-updated by a source-side failure). `force_source_sync` forces a
    full source re-index.
    """
    try:
        _preflight._validate_workspace_id_minimal(workspace_id)

        # Load config once so the Chroma persist dir honors config.paths.chroma_dir
        # (must match the dir build/preflight resolve, else the collection we write
        # here lands somewhere build won't read from).
        config = _preflight.load_config()

        # Y2.1b: cards as path | content | artifact_id (exactly one). Materialized
        # to a temp file so the existing path-based validator/ingest run unchanged.
        # materialize_input validates the input contract at __enter__, so a bad/
        # missing input fails fast (INVALID_INPUT) BEFORE the heavier preflight.
        with _io.materialize_input(
            workspace_id=workspace_id, field="cards", content=cards_content,
            artifact_id=cards_artifact_id, path=cards_path or None, suffix=".yaml",
            config=config,
        ) as cards_file:
            # Lightweight preflight: bge-m3 cached + chroma writable + key + config.
            # NOT product_context (we're about to create it).
            _preflight.run_preflight(
                workspace_id, require_product_context=False, config=config
            )

            # Validate the cards FIRST — fail fast on bad cards before any sync work.
            cards = _validator.load_and_validate(cards_file)

            # Opt-in source sync, BEFORE the card upsert. A partial source sync leaves
            # the card collection untouched (degraded-but-safe), so the two RAG stores
            # never drift into a half-updated pair from one failed call.
            source_sync = None
            if sync_sources:
                rp = _paths.resolve_paths(config)
                source_sync = _src_indexer.sync_sources(
                    sources_dir=rp.sources_root(workspace_id),
                    workspace_id=workspace_id,
                    embedding_provider=_embedding.BgeM3Provider(),
                    vectorstore_provider=_vectorstore.ChromaProvider(config=config),
                    manifest_path=rp.source_index_manifest(workspace_id),
                    now_iso=datetime.now(UTC).isoformat(),
                    force=force_source_sync,
                )
                if source_sync.get("partial"):
                    return {
                        "ok": True,
                        "card_ingested": False,
                        "reason": "source sync was partial — card collection left unchanged",
                        "source_sync": source_sync,
                    }

            result = _ingest.ingest_cards(
                cards=cards,
                workspace_id=workspace_id,
                embedding_provider=_embedding.BgeM3Provider(),
                vectorstore_provider=_vectorstore.ChromaProvider(config=config),
            )
            if source_sync is not None:
                result["source_sync"] = source_sync
                result["card_ingested"] = True

            # CS7: write the ingest receipt. For a path input it lands next to the
            # cards file (existing behavior); for content/artifact (no stable path)
            # it lands in the workspace dir — exactly where build/measure look for it.
            try:
                receipt_dir = (
                    Path(cards_path).expanduser().parent
                    if cards_path
                    else _paths.resolve_paths(config).workspace_dir(workspace_id)
                )
                receipt = _ingest.build_ingest_receipt(
                    content_fingerprint=result.get("content_fingerprint", ""),
                    cards_sha256=_run_summary.sha256_file(cards_file),
                    collection=result["collection"],
                    chunk_count=result.get("chunks", 0),
                    embedding_model_id=result.get("embedding_model_id", "bge-m3"),
                    now_iso=datetime.now(UTC).isoformat(),
                )
                receipt_path = receipt_dir / _ingest.RECEIPT_FILENAME
                _ingest.write_ingest_receipt(receipt, receipt_path)
                result["receipt_path"] = str(receipt_path)
            except Exception:  # noqa: BLE001 — auxiliary, never fail the ingest
                result["receipt_path"] = None

            result.setdefault(
                "source",
                _io.input_source_label(
                    content=cards_content, artifact_id=cards_artifact_id, path=cards_path or None
                ),
            )
            return result
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.INGEST)
