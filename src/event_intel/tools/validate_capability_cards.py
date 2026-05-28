"""validate_capability_cards MCP tool handler.

Module-reference import of cards.validator so tests can monkeypatch through the
boundary. Stage = INGEST because validate is part of the ingest lifecycle.
"""
from __future__ import annotations

from event_intel.cards import validator as _validator
from event_intel.errors import Stage, envelope_from_exception


def validate_capability_cards(cards_path: str) -> dict:
    """Validate a hand-edited capability_cards.yaml file. Returns envelope."""
    try:
        cards = _validator.load_and_validate(cards_path)
        return {
            "ok": True,
            "cards_path": cards_path,
            "schema_version": cards.schema_version,
            "product_name": cards.product_name,
            "capability_count": len(cards.capabilities),
            "competitor_count": len(cards.competitors),
            "bad_fit_count": len(cards.bad_fit),
            "buying_trigger_count": len(cards.buying_triggers),
        }
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.INGEST)
