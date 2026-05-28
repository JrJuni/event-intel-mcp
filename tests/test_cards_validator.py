"""S2 — validator path-localized error tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from event_intel.cards import validator as _validator
from event_intel.cards.validator import load_and_validate, validate_dict
from event_intel.errors import ErrorCode, MCPError, Stage


def _good_dict() -> dict:
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


def test_validate_dict_passes_for_valid_cards():
    cards = validate_dict(_good_dict())
    assert cards.product_name == "Mobius"


def test_validate_dict_raises_schema_error_with_path_hint():
    bad = _good_dict()
    bad["capabilities"][0]["keywords"] = ["too", "few"]  # min_length=3
    with pytest.raises(MCPError) as exc:
        validate_dict(bad)
    assert exc.value.error_code == ErrorCode.SCHEMA_ERROR
    assert exc.value.stage == Stage.INGEST
    hint = exc.value.hint
    assert isinstance(hint, dict)
    paths = [e["path"] for e in hint["errors"]]
    # Pydantic surfaces the field as `capabilities[0].keywords`
    assert any("capabilities" in p and "keywords" in p for p in paths)


def test_load_and_validate_missing_file_returns_io_error(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(MCPError) as exc:
        load_and_validate(missing)
    assert exc.value.error_code == ErrorCode.IO_ERROR


def test_load_and_validate_invalid_yaml_returns_schema_error(tmp_path):
    p = tmp_path / "broken.yaml"
    p.write_text("not: valid: yaml: :::", encoding="utf-8")
    with pytest.raises(MCPError) as exc:
        load_and_validate(p)
    assert exc.value.error_code == ErrorCode.SCHEMA_ERROR


def test_load_and_validate_non_mapping_root_returns_schema_error(tmp_path):
    """A list at root is valid YAML but not a valid cards document."""
    p = tmp_path / "list.yaml"
    p.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(MCPError) as exc:
        load_and_validate(p)
    assert exc.value.error_code == ErrorCode.SCHEMA_ERROR
    assert "mapping" in exc.value.message.lower()


def test_load_and_validate_happy_path_with_real_fixture(repo_root: Path):
    cards = load_and_validate(repo_root / "tests" / "fixtures" / "cards" / "sample_cards.yaml")
    assert cards.product_name == "Mobius"
    assert len(cards.capabilities) >= 1
    assert len(cards.competitors) >= 1


def test_validator_module_reference_import_pattern():
    """validator must be importable cold (no heavy ML deps)."""
    # Trivial smoke — import validator and check the module attributes exist.
    assert hasattr(_validator, "load_and_validate")
    assert hasattr(_validator, "validate_dict")
