"""MCPError envelope shape snapshot.

Locks the envelope shape so downstream tooling (Claude Desktop client, CLI JSON
output, future LSP-style integrations) can rely on it without dance.

Phase 18S: 10 error_codes × 6 stages (60 pairs).
Phase 18T: 14 error_codes × 7 stages (98 pairs). +4 codes, +1 stage.
"""
from __future__ import annotations

import pytest

from event_intel.errors import ErrorCode, MCPError, Stage, envelope_from_exception
from event_intel.storage.identifiers import sanitize_slug, validate_slug

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
    # Phase 18T acquisition layer (+4)
    "ACQUISITION_AMBIGUOUS",
    "LOGIN_REQUIRED",
    "OPERATOR_CAPTURE_REQUIRED",
    "ROBOTS_DISALLOWED",
}

EXPECTED_STAGES = {
    "preflight",
    "extraction",
    "enrichment",
    "scoring",
    "report",
    "ingest",
    "acquisition",  # Phase 18T
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


# ---------- 14×7 cartesian (Phase 18T) + INVALID_INPUT hint shape ----------


def test_envelope_cartesian_matrix_covers_14_codes_x_7_stages():
    """All 98 (code, stage) combinations must round-trip cleanly through the
    envelope renderer. Phase 18T expanded from 60 → 98 (14 codes × 7 stages)."""
    expected_keys = {"ok", "error_code", "stage", "message", "hint", "retryable"}
    assert len(ErrorCode) == 14, (
        f"ErrorCode count drifted (expected 14 for Phase 18T, got {len(ErrorCode)}). "
        "Update this snapshot when adding new codes."
    )
    assert len(Stage) == 7, (
        f"Stage count drifted (expected 7 for Phase 18T, got {len(Stage)}). "
        "Update this snapshot when adding new stages."
    )

    pairs_seen: set[tuple[str, str]] = set()
    for code in ErrorCode:
        for stage in Stage:
            envelope = MCPError(
                error_code=code,
                stage=stage,
                message=f"{code}@{stage}",
                hint={"k": "v"},
                retryable=False,
            ).to_envelope()
            assert set(envelope.keys()) == expected_keys, (
                f"envelope schema drift at ({code}, {stage}): {set(envelope.keys())}"
            )
            assert envelope["error_code"] == str(code)
            assert envelope["stage"] == str(stage)
            pairs_seen.add((str(code), str(stage)))
    assert len(pairs_seen) == 98, f"expected 98 unique pairs, got {len(pairs_seen)}"


def test_invalid_input_hint_carries_suggested_slug_via_sanitize_slug():
    """R3-#3 envelope acceptance — sanitize_slug raises MCPError whose envelope
    surfaces `suggested_slug` in `hint`. This is the surface Claude Desktop
    shows to the user, so the field name + shape are part of the contract.

    NOTE: sanitize_slug/validate_slug are imported at module top, NOT inside
    this function — see docs/lesson-learned.md S4 entry on class-identity
    drift across cold-start fixture purges. Function-local imports bind
    against the freshly re-imported module, which has a DIFFERENT MCPError
    class object than the one bound at module top, breaking pytest.raises.
    """
    with pytest.raises(MCPError) as ei:
        sanitize_slug("서울 ITS 2026", field_name="event_slug")
    env = ei.value.to_envelope()
    assert env["ok"] is False
    assert env["error_code"] == "INVALID_INPUT"
    assert env["stage"] == "preflight"
    assert isinstance(env["hint"], dict)
    assert "suggested_slug" in env["hint"]
    assert validate_slug(env["hint"]["suggested_slug"])
    assert env["hint"]["field"] == "event_slug"
    assert env["hint"]["rule"] == "^[a-zA-Z0-9_-]{1,64}$"


def test_invalid_input_envelope_for_each_user_facing_field():
    """The same envelope contract applies regardless of which user-facing slug
    field violated — workspace_id, event_slug, or any future surface."""
    for field in ("workspace_id", "event_slug"):
        with pytest.raises(MCPError) as ei:
            sanitize_slug("../traversal", field_name=field)
        env = ei.value.to_envelope()
        assert env["error_code"] == "INVALID_INPUT"
        assert env["hint"]["field"] == field
        # Suggested slug must not echo the path-traversal bytes back.
        suggested = env["hint"]["suggested_slug"]
        assert "/" not in suggested
        assert ".." not in suggested
