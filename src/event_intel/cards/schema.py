"""Capability Cards — Pydantic v2 SSOT (plan v0.5 §Capability Cards Schema v1).

This module is the sole source of truth for the cards schema. Any JSON schema
artifact (e.g. `cli export-schema`) is generated *from* this module, not the
other way around. A drift test (`tests/test_cards_schema_drift.py`) locks the
JSON Schema export against `cards/schema_snapshot.json`; intentional changes
bump `SCHEMA_VERSION` and refresh the snapshot in the same commit.

No heavy imports here — module is cold-start safe and may be imported from
inside MCP tool handlers without violating the no-ML-at-import-time rule.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 2
TargetMode = Literal["customer", "partner", "ecosystem"]


class _StrictBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Capability(_StrictBase):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    keywords: Annotated[list[str], Field(min_length=3, max_length=20)]
    buyer_pains: Annotated[list[str], Field(min_length=1, max_length=10)]
    evidence_queries: Annotated[list[str], Field(min_length=1, max_length=10)]


class IdealCustomer(_StrictBase):
    industries: Annotated[list[str], Field(min_length=1, max_length=20)]
    company_signals: Annotated[list[str], Field(min_length=1, max_length=20)]
    geo: Annotated[list[str], Field(default_factory=list, max_length=20)]


class BuyingTrigger(_StrictBase):
    signal: Annotated[str, Field(min_length=1)]
    weight: Annotated[float, Field(ge=0.0, le=1.0)]


class BadFit(_StrictBase):
    reason: Annotated[str, Field(min_length=1)]
    keywords: Annotated[list[str], Field(default_factory=list, max_length=20)]


class Competitor(_StrictBase):
    name: Annotated[str, Field(min_length=1)]
    keywords: Annotated[list[str], Field(default_factory=list, max_length=20)]


class CapabilityCards(_StrictBase):
    # Literal[1, 2]: v1 cards (no target_mode) are accepted and migrated to v2 —
    # target_mode defaults to "customer" and the stamp is normalized to 2 in the
    # after-validator. Reject anything else loudly (review round-2 #4).
    schema_version: Literal[1, 2] = SCHEMA_VERSION
    product_name: Annotated[str, Field(min_length=1, max_length=100)]
    one_liner: Annotated[str, Field(min_length=1, max_length=200)]
    # target_mode is a CARD-LEVEL DEFAULT only; the effective mode is resolved at
    # build time (build_event arg > user config > card default > "customer").
    target_mode: TargetMode = "customer"
    capabilities: Annotated[list[Capability], Field(min_length=1, max_length=10)]
    ideal_customer: IdealCustomer
    buying_triggers: Annotated[
        list[BuyingTrigger], Field(default_factory=list, max_length=10)
    ]
    bad_fit: Annotated[list[BadFit], Field(default_factory=list, max_length=10)]
    competitors: Annotated[list[Competitor], Field(default_factory=list, max_length=20)]

    @model_validator(mode="after")
    def _migrate_to_current(self) -> CapabilityCards:
        # v1 → v2 migration: target_mode already defaulted to "customer" above;
        # normalize the version stamp so downstream only ever sees the current one.
        if self.schema_version != SCHEMA_VERSION:
            self.schema_version = SCHEMA_VERSION
        return self


__all__ = [
    "SCHEMA_VERSION",
    "TargetMode",
    "Capability",
    "IdealCustomer",
    "BuyingTrigger",
    "BadFit",
    "Competitor",
    "CapabilityCards",
]
