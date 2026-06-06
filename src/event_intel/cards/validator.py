"""Validate capability_cards.yaml → CapabilityCards instance.

Surfaces every pydantic ValidationError as a single MCPError(SCHEMA_ERROR) with a
path-localized hint so the UI / Claude Desktop can point the user at the exact
offending field. YAML parse errors also funnel through SCHEMA_ERROR (same stage)
since both are user-authored content failing the contract.

No heavy imports at module top — safe to wire from MCP tool handlers.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from event_intel.cards.schema import SCHEMA_VERSION, CapabilityCards
from event_intel.errors import ErrorCode, MCPError, Stage


def _loc_to_path(loc: tuple[Any, ...]) -> str:
    """Render a pydantic error location tuple as a JSON-pointer-ish path."""
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        else:
            parts.append(f".{item}" if parts else str(item))
    return "".join(parts) or "<root>"


def _format_pydantic_errors(exc: ValidationError) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for err in exc.errors():
        out.append(
            {
                "path": _loc_to_path(err.get("loc", ())),
                "type": str(err.get("type", "")),
                "msg": str(err.get("msg", "")),
            }
        )
    return out


def validate_dict(data: dict) -> CapabilityCards:
    """Validate a dict against CapabilityCards. Raises MCPError(SCHEMA_ERROR) on failure."""
    try:
        return CapabilityCards.model_validate(data)
    except ValidationError as exc:
        errors = _format_pydantic_errors(exc)
        first = errors[0] if errors else {"path": "<root>", "msg": "validation failed"}
        raise MCPError(
            error_code=ErrorCode.SCHEMA_ERROR,
            stage=Stage.INGEST,
            message=(
                f"capability_cards failed validation at {first['path']}: {first['msg']}"
            ),
            hint={
                "errors": errors,
                "schema_version_expected": SCHEMA_VERSION,
                "fix": (
                    "Fix the listed paths in the yaml file. Use "
                    "`event-intel export-schema` to see the full schema."
                ),
            },
            retryable=False,
        ) from exc


def load_and_validate(path: Path | str) -> CapabilityCards:
    """Read a yaml file and validate it. All failures → MCPError(SCHEMA_ERROR)."""
    p = Path(path).expanduser()
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MCPError(
            error_code=ErrorCode.IO_ERROR,
            stage=Stage.INGEST,
            message=f"capability_cards file not found: {p}",
            hint={"expected_path": str(p)},
            retryable=False,
        ) from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise MCPError(
            error_code=ErrorCode.SCHEMA_ERROR,
            stage=Stage.INGEST,
            message=f"capability_cards file at {p} is not valid YAML: {exc}",
            hint={"expected_path": str(p)},
            retryable=False,
        ) from exc

    if not isinstance(data, dict):
        raise MCPError(
            error_code=ErrorCode.SCHEMA_ERROR,
            stage=Stage.INGEST,
            message=f"capability_cards file at {p} must be a YAML mapping at the root",
            hint={"expected_path": str(p), "got_type": type(data).__name__},
            retryable=False,
        )

    return validate_dict(data)
