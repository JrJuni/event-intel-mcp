"""S1 — MCPError envelope shape snapshot.

Locks the envelope shape so downstream tooling (Claude Desktop client, CLI JSON
output, future LSP-style integrations) can rely on it without dance.

See plan v0.5 Contract #19: 10 error_codes × 6 stages.
"""
from __future__ import annotations

from event_intel.errors import ErrorCode, MCPError, Stage, envelope_from_exception


# Expected snapshot. If this drifts, downstream consumers must be updated too.
EXPECTED_ERROR_CODES = {
    "INVALID_INPUT",
    "MODEL_NOT_READY",
    "SCHEMA_ERROR",
    "RATE_LIMITED",
    "UPSTREAM_ERROR",
    "IO_ERROR",
    "INTERNAL",
    "PRODUCT_CONTEXT_MISSING",
    "SOURCE_CAPTURE_FAILED",
    "CONFIG_ERROR",
}

EXPECTED_STAGES = {
    "preflight",
    "extraction",
    "enrichment",
    "scoring",
    "report",
    "ingest",
}


def test_error_code_enum_matches_expected_snapshot():
    """Adding/removing an ErrorCode without bumping this snapshot is a regression."""
    actual = {str(c) for c in ErrorCode}
    assert actual == EXPECTED_ERROR_CODES, (
        f"ErrorCode enum drifted from snapshot. "
        f"added={actual - EXPECTED_ERROR_CODES}, removed={EXPECTED_ERROR_CODES - actual}"
    )


def test_stage_enum_matches_expected_snapshot():
    actual = {str(s) for s in Stage}
    assert actual == EXPECTED_STAGES


def test_envelope_shape_is_stable_across_codes_and_stages():
    """For every (code, stage) combination, envelope must have the same 6 keys."""
    expected_keys = {"ok", "error_code", "stage", "message", "hint", "retryable"}
    for code in ErrorCode:
        for stage in Stage:
            envelope = MCPError(
                error_code=code,
                stage=stage,
                message=f"{code} at {stage}",
                hint={"detail": "x"},
                retryable=False,
            ).to_envelope()
            assert set(envelope.keys()) == expected_keys
            assert envelope["ok"] is False
            assert envelope["error_code"] == str(code)
            assert envelope["stage"] == str(stage)


def test_envelope_from_exception_wraps_arbitrary_error_as_internal():
    envelope = envelope_from_exception(ValueError("boom"), stage=Stage.EXTRACTION)
    assert envelope["ok"] is False
    assert envelope["error_code"] == "INTERNAL"
    assert envelope["stage"] == "extraction"
    assert "ValueError: boom" in envelope["message"]


def test_envelope_from_exception_preserves_mcperror_taxonomy():
    """If the exception is already an MCPError, the taxonomy must survive."""
    err = MCPError(
        error_code=ErrorCode.RATE_LIMITED,
        stage=Stage.ENRICHMENT,
        message="brave 429",
        hint={"retry_after": 30},
        retryable=True,
    )
    envelope = envelope_from_exception(err, stage=Stage.PREFLIGHT)  # stage arg ignored
    assert envelope["error_code"] == "RATE_LIMITED"
    assert envelope["stage"] == "enrichment"
    assert envelope["retryable"] is True
    assert envelope["hint"] == {"retry_after": 30}
