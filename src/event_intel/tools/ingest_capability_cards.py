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
from event_intel.runtime import preflight as _preflight


def ingest_product_context(
    *,
    workspace_id: str = "default",
    cards_path: str = "",
    extra_source_paths: list[str] | None = None,  # reserved for v0.4+ whitepapers
) -> dict:
    """Validate + embed + upsert capability_cards.yaml into product_{workspace_id}.

    Preflight runs with `require_product_context=False` — the whole point of
    this call is to create that collection, so requiring it would deadlock.
    """
    try:
        _preflight._validate_workspace_id_minimal(workspace_id)
        if not cards_path:
            raise ValueError("cards_path is required")

        # Load config once so the Chroma persist dir honors config.paths.chroma_dir
        # (must match the dir build/preflight resolve, else the collection we write
        # here lands somewhere build won't read from).
        config = _preflight.load_config()

        # Lightweight preflight: bge-m3 cached + chroma writable + key + config.
        # NOT product_context (we're about to create it).
        _preflight.run_preflight(
            workspace_id,
            require_product_context=False,
            config=config,
        )

        cards = _validator.load_and_validate(cards_path)

        result = _ingest.ingest_cards(
            cards=cards,
            workspace_id=workspace_id,
            embedding_provider=_embedding.BgeM3Provider(),
            vectorstore_provider=_vectorstore.ChromaProvider(config=config),
        )

        # CS7: write the ingest receipt next to the cards it describes, so a later
        # build/measure can fold its content_fingerprint into run_fingerprint and
        # detect collection drift. Auxiliary — a receipt failure must not fail the
        # ingest, whose authoritative output is the (already-written) collection.
        try:
            receipt = _ingest.build_ingest_receipt(
                content_fingerprint=result.get("content_fingerprint", ""),
                cards_sha256=_run_summary.sha256_file(cards_path),
                collection=result["collection"],
                chunk_count=result.get("chunks", 0),
                embedding_model_id=result.get("embedding_model_id", "bge-m3"),
                now_iso=datetime.now(UTC).isoformat(),
            )
            receipt_path = Path(cards_path).expanduser().parent / _ingest.RECEIPT_FILENAME
            _ingest.write_ingest_receipt(receipt, receipt_path)
            result["receipt_path"] = str(receipt_path)
        except Exception:  # noqa: BLE001 — auxiliary, never fail the ingest
            result["receipt_path"] = None

        return result
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.INGEST)
