"""MCP error envelope. See plan v0.5 Contract #19."""
from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    MODEL_NOT_READY = "MODEL_NOT_READY"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    IO_ERROR = "IO_ERROR"
    INTERNAL = "INTERNAL"
    PRODUCT_CONTEXT_MISSING = "PRODUCT_CONTEXT_MISSING"
    SOURCE_CAPTURE_FAILED = "SOURCE_CAPTURE_FAILED"
    CONFIG_ERROR = "CONFIG_ERROR"
    # Phase 18T acquisition layer (4 new codes)
    ACQUISITION_AMBIGUOUS = "ACQUISITION_AMBIGUOUS"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    OPERATOR_CAPTURE_REQUIRED = "OPERATOR_CAPTURE_REQUIRED"
    ROBOTS_DISALLOWED = "ROBOTS_DISALLOWED"


class Stage(StrEnum):
    PREFLIGHT = "preflight"
    EXTRACTION = "extraction"
    ENRICHMENT = "enrichment"
    SCORING = "scoring"
    REPORT = "report"
    INGEST = "ingest"
    ACQUISITION = "acquisition"  # Phase 18T


class MCPError(Exception):
    """Structured error raised inside tool handlers and rendered to envelope at the boundary."""

    def __init__(
        self,
        *,
        error_code: ErrorCode,
        stage: Stage,
        message: str,
        hint: str | dict | None = None,
        retryable: bool = False,
    ) -> None:
        self.error_code = error_code
        self.stage = stage
        self.message = message
        self.hint = hint
        self.retryable = retryable
        super().__init__(message)

    def to_envelope(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": str(self.error_code),
            "stage": str(self.stage),
            "message": self.message,
            "hint": self.hint,
            "retryable": self.retryable,
        }


def envelope_from_exception(exc: Exception, *, stage: Stage) -> dict[str, Any]:
    """Wrap any uncaught exception in an INTERNAL envelope. Tool boundary fallback."""
    if isinstance(exc, MCPError):
        return exc.to_envelope()
    return MCPError(
        error_code=ErrorCode.INTERNAL,
        stage=stage,
        message=f"{type(exc).__name__}: {exc}",
        retryable=False,
    ).to_envelope()
