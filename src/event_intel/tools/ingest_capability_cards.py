"""ingest_capability_cards MCP tool handler.

Note the file name vs. tool name: the MCP tool surface is `ingest_product_context`
(it ingests cards INTO the product context collection), but the underlying
ingest module + this handler are scoped to cards-as-input. Future ingestion
surfaces (e.g. raw whitepapers) can land alongside without renaming.

Module-reference imports for monkeypatch safety. Cold-start safe at module top.
"""
from __future__ import annotations

from event_intel.cards import ingest as _ingest
from event_intel.cards import validator as _validator
from event_intel.errors import Stage, envelope_from_exception
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

        # Lightweight preflight: bge-m3 cached + chroma writable + key + config.
        # NOT product_context (we're about to create it).
        _preflight.run_preflight(
            workspace_id,
            require_product_context=False,
        )

        cards = _validator.load_and_validate(cards_path)

        result = _ingest.ingest_cards(
            cards=cards,
            workspace_id=workspace_id,
            embedding_provider=_embedding.BgeM3Provider(),
            vectorstore_provider=_vectorstore.ChromaProvider(),
        )
        return result
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.INGEST)
