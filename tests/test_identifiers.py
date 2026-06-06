"""S6 — sanitize_slug / validate_slug / suggest_slug edge cases.

Slug grammar: ^[a-zA-Z0-9_-]{1,64}$. Every entry point (MCP tools, CLI,
preflight) MUST gate on this. The R3-#3 hint surface includes a
`suggested_slug` so users can copy-paste a fix.
"""
from __future__ import annotations

import pytest

from event_intel.errors import ErrorCode, MCPError, Stage
from event_intel.storage.identifiers import sanitize_slug, suggest_slug, validate_slug

# ---------- validate_slug ----------


@pytest.mark.parametrize(
    "good",
    [
        "default", "a", "ABCxyz", "0", "snake_case", "kebab-case",
        "mix_of_123", "A" * 64,
    ],
)
def test_validate_slug_accepts_legal_inputs(good):
    assert validate_slug(good) is True


@pytest.mark.parametrize(
    "bad",
    [
        "", " ", "../bad", "a/b", "a b", "a.b", "한글", "café",
        "A" * 65, "name with space", "tab\there",
    ],
)
def test_validate_slug_rejects_illegal_inputs(bad):
    assert validate_slug(bad) is False


def test_validate_slug_rejects_non_strings():
    assert validate_slug(None) is False  # type: ignore[arg-type]
    assert validate_slug(123) is False   # type: ignore[arg-type]


# ---------- suggest_slug ----------


def test_suggest_slug_strips_punctuation_and_lowercases():
    assert suggest_slug("Sample Expo!") == "sample-expo"


def test_suggest_slug_folds_latin_diacritics():
    assert suggest_slug("café résumé") == "cafe-resume"


def test_suggest_slug_handles_path_separators():
    # `a/b` is a path-traversal attempt; we replace `/` with hyphen, not drop.
    assert suggest_slug("a/b") == "a-b"


def test_suggest_slug_preserves_ascii_inside_korean():
    # "서울 ITS 2026" — Hangul drops, ASCII survives, lowercased + hyphenated.
    result = suggest_slug("서울 ITS 2026")
    assert validate_slug(result), f"suggested slug {result!r} is itself invalid"
    assert "its" in result
    assert "2026" in result


def test_suggest_slug_falls_back_to_hash_for_all_non_ascii():
    # Pure Korean — no ASCII to recover. Must produce a valid hash-suffix slug.
    result = suggest_slug("한글만")
    assert validate_slug(result), f"fallback slug {result!r} is invalid"
    assert result.startswith("event-")
    assert len(result) == len("event-") + 8


def test_suggest_slug_is_deterministic_across_calls():
    # Same input → same suggestion. Important: re-running a build with the
    # same Korean event_name should hit the same Chroma collection.
    a = suggest_slug("한글만")
    b = suggest_slug("한글만")
    assert a == b


def test_suggest_slug_truncates_to_64_chars():
    long = "A" * 100
    result = suggest_slug(long)
    assert validate_slug(result)
    assert len(result) == 64


def test_suggest_slug_handles_empty_input():
    # Even empty string yields *some* valid slug (hash of empty bytes).
    result = suggest_slug("")
    assert validate_slug(result)
    assert result.startswith("event-")


def test_suggest_slug_collapses_consecutive_separators():
    assert suggest_slug("a   b") == "a-b"
    assert suggest_slug("a!!!b") == "a-b"


def test_suggest_slug_strips_leading_and_trailing_separators():
    assert suggest_slug("---hello---") == "hello"


# ---------- sanitize_slug (raise + hint contract) ----------


def test_sanitize_slug_passthrough_when_valid():
    assert sanitize_slug("default") == "default"
    assert sanitize_slug("ws-123_v2") == "ws-123_v2"


def test_sanitize_slug_raises_invalid_input_with_suggested_slug_for_korean():
    with pytest.raises(MCPError) as ei:
        sanitize_slug("서울 ITS 2026", field_name="event_slug")
    err = ei.value
    assert err.error_code == ErrorCode.INVALID_INPUT
    assert err.stage == Stage.PREFLIGHT
    assert err.retryable is False
    assert isinstance(err.hint, dict)
    assert err.hint["field"] == "event_slug"
    assert err.hint["rule"] == "^[a-zA-Z0-9_-]{1,64}$"
    suggested = err.hint["suggested_slug"]
    assert validate_slug(suggested), f"suggested slug {suggested!r} must itself be valid"


def test_sanitize_slug_raises_for_path_traversal_attempt():
    with pytest.raises(MCPError) as ei:
        sanitize_slug("../etc/passwd", field_name="workspace_id")
    assert ei.value.error_code == ErrorCode.INVALID_INPUT
    suggested = ei.value.hint["suggested_slug"]
    assert "/" not in suggested
    assert ".." not in suggested


def test_sanitize_slug_raises_for_empty():
    with pytest.raises(MCPError) as ei:
        sanitize_slug("", field_name="workspace_id")
    assert ei.value.error_code == ErrorCode.INVALID_INPUT
    # Empty input still gets a valid suggested slug (hash fallback).
    assert validate_slug(ei.value.hint["suggested_slug"])


def test_sanitize_slug_raises_for_oversize_input():
    with pytest.raises(MCPError) as ei:
        sanitize_slug("A" * 65)
    suggested = ei.value.hint["suggested_slug"]
    assert validate_slug(suggested)
    assert len(suggested) <= 64


def test_sanitize_slug_envelope_renders_full_hint():
    """The MCP tool boundary calls .to_envelope() — make sure the hint
    survives that round-trip with the suggested_slug field intact."""
    try:
        sanitize_slug("a/b/c")
    except MCPError as err:
        env = err.to_envelope()
        assert env["ok"] is False
        assert env["error_code"] == "INVALID_INPUT"
        assert env["stage"] == "preflight"
        assert "suggested_slug" in env["hint"]
        assert validate_slug(env["hint"]["suggested_slug"])
    else:
        pytest.fail("sanitize_slug should have raised")
