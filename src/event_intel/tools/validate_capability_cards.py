"""validate_capability_cards MCP tool handler.

Module-reference import of cards.validator so tests can monkeypatch through the
boundary. Stage = INGEST because validate is part of the ingest lifecycle.

Input contract (Y2.1b): cards may come as `cards_path` (server-local, personal-
local lane), `cards_content` (inline), or `cards_artifact_id` (uploaded) —
exactly one.
"""
from __future__ import annotations

from event_intel.cards import validator as _validator
from event_intel.errors import Stage, envelope_from_exception
from event_intel.runtime import io_contract as _io


def validate_capability_cards(
    cards_path: str = "",
    cards_content: str | None = None,
    cards_artifact_id: str | None = None,
    workspace_id: str = "default",
) -> dict:
    """Validate a capability_cards.yaml against schema v1. Returns envelope.

    Provide exactly one of cards_path / cards_content / cards_artifact_id.
    """
    try:
        with _io.materialize_input(
            workspace_id=workspace_id, field="cards", content=cards_content,
            artifact_id=cards_artifact_id, path=cards_path or None, suffix=".yaml",
        ) as cards_file:
            cards = _validator.load_and_validate(cards_file)
            return {
                "ok": True,
                "source": _io.input_source_label(
                    content=cards_content, artifact_id=cards_artifact_id, path=cards_path or None
                ),
                "cards_path": cards_path or None,
                "schema_version": cards.schema_version,
                "product_name": cards.product_name,
                "capability_count": len(cards.capabilities),
                "competitor_count": len(cards.competitors),
                "bad_fit_count": len(cards.bad_fit),
                "buying_trigger_count": len(cards.buying_triggers),
            }
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.INGEST)
