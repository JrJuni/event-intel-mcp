"""S2 — CapabilityCards pydantic schema tests."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from event_intel.cards.schema import (
    SCHEMA_VERSION,
    Capability,
    CapabilityCards,
    IdealCustomer,
)


def _minimal_cards_dict() -> dict:
    return {
        "schema_version": 1,
        "product_name": "Mobius",
        "one_liner": "Embedded NPU compiler.",
        "capabilities": [
            {
                "name": "Quantization",
                "keywords": ["INT8", "INT4", "quantization"],
                "buyer_pains": ["model too large"],
                "evidence_queries": ["NPU quantization accuracy"],
            }
        ],
        "ideal_customer": {
            "industries": ["automotive"],
            "company_signals": ["hiring compiler engineers"],
        },
    }


def test_minimal_cards_validates():
    cards = CapabilityCards.model_validate(_minimal_cards_dict())
    assert cards.schema_version == 1
    assert cards.product_name == "Mobius"
    assert len(cards.capabilities) == 1
    # Defaults are empty lists, not None
    assert cards.buying_triggers == []
    assert cards.bad_fit == []
    assert cards.competitors == []


def test_capability_requires_at_least_three_keywords():
    """plan v0.5 constraint: keywords min_length=3."""
    bad = {
        "name": "Q",
        "keywords": ["only", "two"],
        "buyer_pains": ["x"],
        "evidence_queries": ["y"],
    }
    with pytest.raises(ValidationError) as exc:
        Capability.model_validate(bad)
    # Error path should point at .keywords
    assert any("keywords" in str(e["loc"]) for e in exc.value.errors())


def test_schema_version_must_be_one():
    """Literal[1] — schema bumps need an intentional SCHEMA_VERSION change."""
    data = _minimal_cards_dict()
    data["schema_version"] = 2
    with pytest.raises(ValidationError):
        CapabilityCards.model_validate(data)


def test_extra_top_level_keys_rejected():
    """extra='forbid' — typos like `ideal_customers` (plural) must fail loud."""
    data = _minimal_cards_dict()
    data["ideal_customers"] = data.pop("ideal_customer")
    with pytest.raises(ValidationError) as exc:
        CapabilityCards.model_validate(data)
    err_str = str(exc.value)
    assert "ideal_customers" in err_str or "extra" in err_str.lower()


def test_buying_trigger_weight_bounds():
    """Weight is 0..1 by Field(ge=0, le=1)."""
    data = _minimal_cards_dict()
    data["buying_triggers"] = [{"signal": "release", "weight": 1.5}]
    with pytest.raises(ValidationError):
        CapabilityCards.model_validate(data)


def test_ideal_customer_geo_optional():
    """geo defaults to [] — minimal cards don't have to set it."""
    ic = IdealCustomer.model_validate(
        {"industries": ["x"], "company_signals": ["y"]}
    )
    assert ic.geo == []


def test_schema_version_constant_matches_model_default():
    assert SCHEMA_VERSION == 1
    cards = CapabilityCards.model_validate(_minimal_cards_dict())
    assert cards.schema_version == SCHEMA_VERSION
