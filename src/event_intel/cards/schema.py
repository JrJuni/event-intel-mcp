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

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


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
    schema_version: Literal[1] = 1
    product_name: Annotated[str, Field(min_length=1, max_length=100)]
    one_liner: Annotated[str, Field(min_length=1, max_length=200)]
    capabilities: Annotated[list[Capability], Field(min_length=1, max_length=10)]
    ideal_customer: IdealCustomer
    buying_triggers: Annotated[
        list[BuyingTrigger], Field(default_factory=list, max_length=10)
    ]
    bad_fit: Annotated[list[BadFit], Field(default_factory=list, max_length=10)]
    competitors: Annotated[list[Competitor], Field(default_factory=list, max_length=20)]


__all__ = [
    "SCHEMA_VERSION",
    "Capability",
    "IdealCustomer",
    "BuyingTrigger",
    "BadFit",
    "Competitor",
    "CapabilityCards",
]
