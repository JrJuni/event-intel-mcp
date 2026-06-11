"""Tests for events.extraction — chunked LLM extraction + cap + snippet floor + lang norm."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from event_intel.errors import ErrorCode, MCPError
from event_intel.events import extraction as _extraction
from event_intel.events.extraction import (
    ExhibitorCandidate,
    _normalize_name,
    _split_chunks,
    extract_exhibitors,
)
from event_intel.events.source_capture import SourceCapture, capture_source

# ---------- Fake LLM provider ----------


@dataclass
class _LLMResp:
    text: str
    usage: dict[str, int]
    model: str = "fake-claude"
    stop_reason: str | None = None


class FakeLLM:
    """Records each chunk it sees and returns canned JSON arrays. Tests can
    set `responses` (list of JSON strings, one per call) before calling."""

    def __init__(self, responses: list[str] | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def chat_once(self, *, system: str, user: str, max_tokens: int, temperature: float):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        if self.responses:
            text = self.responses.pop(0)
        else:
            text = "[]"
        return _LLMResp(text=text, usage={"input_tokens": 10, "output_tokens": 5})

    # Methods unused by extractor — present to satisfy the LLMProvider protocol
    # for any future stricter typing.
    def chat_cached(self, **_):  # pragma: no cover
        raise NotImplementedError

    def ping(self):  # pragma: no cover
        return {"status": "ok"}


def _config(**overrides):
    cfg = {
        "extraction": {
            "max_chars_per_chunk": 200,  # small so we get multiple chunks fast in tests
            "max_chunks_per_event": 12,
            "source_snippet_min_chars": 20,
            "extraction_confidence_min": 0.6,
        },
        "llm": {"extract_max_tokens": 1024},
    }
    cfg["extraction"].update(overrides)
    return cfg


# ---------- _normalize_name ----------


def test_normalize_name_strips_legal_suffixes():
    assert _normalize_name("Mobius Labs Inc.", lang="en") == "mobius labs"
    assert _normalize_name("EdgeVision Co., Ltd.", lang="en") == "edgevision"
    assert _normalize_name("Synaptik Robotics", lang="en") == "synaptik robotics"


def test_normalize_name_strips_korean_prefixes():
    assert _normalize_name("㈜모비우스랩", lang="ko") == "모비우스랩"
    assert _normalize_name("주식회사 뉴로드라이브", lang="ko") == "뉴로드라이브"
    assert _normalize_name("엣지비전 주식회사", lang="ko") == "엣지비전"


# ---------- _split_chunks ----------


def test_split_chunks_respects_paragraph_boundary():
    text = "AAA\n\nBBB\n\nCCC\n\nDDD"
    chunks = _split_chunks(text, max_chars=10)
    # Each AAA-style chunk is 3 chars, so 2 fit per ≤10-char chunk (≈ "AAA\n\nBBB")
    assert len(chunks) >= 2
    assert all(len(c) <= 12 for c in chunks)  # ≤ max + sep slack


# ---------- happy path: english html ----------


def test_extract_happy_path_english_html(repo_root):
    capture = capture_source(
        source_kind="html_file",
        source_ref=str(repo_root / "tests" / "fixtures" / "events" / "sample_exhibitors.html"),
    )
    # One canned response covers all chunks if they merge to a single chunk.
    canned = json.dumps([
        {"name": "Mobius Labs", "source_snippet": "On-device NPU compiler stack for edge AI."},
        {"name": "NeuroDrive Inc.", "source_snippet": "Autonomous driving perception stack."},
        {"name": "EdgeVision Co., Ltd.", "source_snippet": "Computer vision SDK for smart-city traffic cameras."},
    ])
    llm = FakeLLM(responses=[canned] * 20)  # plenty of chunks worth
    result = extract_exhibitors(
        capture=capture, lang="en", llm_provider=llm, config=_config(max_chars_per_chunk=8000)
    )
    names = {c.name for c in result.candidates}
    assert "Mobius Labs" in names
    assert "NeuroDrive Inc." in names
    assert "EdgeVision Co., Ltd." in names
    assert result.dropped_low_snippet == 0
    # Sanity: usage accumulates across calls.
    assert result.usage["input_tokens"] >= 10


# ---------- snippet floor ----------


def test_short_snippet_rows_are_dropped():
    capture = SourceCapture(text="A" * 500, kind="text", source_ref="<x>")
    canned = json.dumps([
        {"name": "Good Co.", "source_snippet": "this snippet is long enough to pass."},
        {"name": "Bad Co.",  "source_snippet": "too short"},  # 9 chars < 20
    ])
    llm = FakeLLM(responses=[canned])
    result = extract_exhibitors(
        capture=capture, lang="en", llm_provider=llm, config=_config(max_chars_per_chunk=10000)
    )
    names = {c.name for c in result.candidates}
    assert "Good Co." in names
    assert "Bad Co." not in names
    assert result.dropped_low_snippet == 1


# ---------- chunk cap ----------


def test_chunk_cap_triggers_warning_and_truncates_head(repo_root):
    capture = capture_source(
        source_kind="html_file",
        source_ref=str(repo_root / "tests" / "fixtures" / "events" / "large_exhibitor_page.html"),
    )
    llm = FakeLLM(responses=["[]"] * 50)
    cfg = _config(max_chars_per_chunk=8000)  # produces > 12 chunks
    result = extract_exhibitors(capture=capture, lang="en", llm_provider=llm, config=cfg)
    assert result.chunks_total > result.chunks_processed
    assert result.chunks_processed == 12
    assert any("head-truncating" in w or "max_chunks_per_event" in w for w in result.warnings)
    # Exactly 12 LLM calls (one per processed chunk).
    assert len(llm.calls) == 12


# ---------- ko normalization merge ----------


def test_korean_name_merge_collapses_legal_prefix():
    capture = SourceCapture(text="P" * 500, kind="text", source_ref="<x>")
    canned1 = json.dumps([
        {"name": "㈜모비우스랩", "source_snippet": "엣지 AI 를 위한 온디바이스 NPU 컴파일러", "url": "https://m.example.kr"},
    ])
    canned2 = json.dumps([
        {"name": "모비우스랩", "source_snippet": "엣지 AI 를 위한 온디바이스 NPU 컴파일러 (재언급)"},
    ])
    llm = FakeLLM(responses=[canned1, canned2])
    cfg = _config(max_chars_per_chunk=200)  # forces ≥ 2 chunks
    result = extract_exhibitors(capture=capture, lang="ko", llm_provider=llm, config=cfg)
    assert len(result.candidates) == 1, [(c.name, c.source_snippet) for c in result.candidates]
    cand = result.candidates[0]
    # First-seen name survives; url merged from first record.
    assert cand.name == "㈜모비우스랩"
    assert cand.url == "https://m.example.kr"
    assert len(cand.chunk_indices) == 2


# ---------- low extraction_confidence routes to needs_review ----------


def test_low_confidence_routes_to_needs_review():
    capture = SourceCapture(text="Q" * 500, kind="text", source_ref="<x>")
    canned = json.dumps([
        {"name": "Confident Co.", "source_snippet": "we definitely exhibit here", "extraction_confidence": 0.9},
        {"name": "Maybe Co.",     "source_snippet": "looks like an exhibitor maybe", "extraction_confidence": 0.4},
    ])
    llm = FakeLLM(responses=[canned])
    result = extract_exhibitors(
        capture=capture, lang="en", llm_provider=llm, config=_config(max_chars_per_chunk=10000)
    )
    accepted_names = {c.name for c in result.candidates}
    review_names = {c.name for c in result.needs_review}
    assert "Confident Co." in accepted_names
    assert "Maybe Co." in review_names
    assert "Maybe Co." not in accepted_names


# ---------- empty capture rejected ----------


def test_empty_capture_raises_source_capture_failed():
    capture = SourceCapture(text="   ", kind="text", source_ref="<empty>")
    llm = FakeLLM(responses=["[]"])
    with pytest.raises(MCPError) as exc_info:
        extract_exhibitors(
            capture=capture, lang="en", llm_provider=llm, config=_config(max_chars_per_chunk=8000)
        )
    assert exc_info.value.error_code == ErrorCode.SOURCE_CAPTURE_FAILED


# ---------- malformed JSON from LLM is recovered ----------


def test_malformed_llm_json_is_recovered_from_array_window():
    capture = SourceCapture(text="R" * 500, kind="text", source_ref="<x>")
    # Prose wrapper around a valid JSON array — extractor should still salvage.
    response = (
        "Here are the exhibitors I see:\n\n"
        '[{"name": "Salvaged Co.", "source_snippet": "salvaged from a noisy response indeed"}]\n\n'
        "Let me know if you need more."
    )
    llm = FakeLLM(responses=[response])
    result = extract_exhibitors(
        capture=capture, lang="en", llm_provider=llm, config=_config(max_chars_per_chunk=10000)
    )
    assert [c.name for c in result.candidates] == ["Salvaged Co."]


# ---------- dict-wrapped LLM responses (plan v3 R6) ----------


def test_dict_wrapped_response_with_known_key_unwraps(caplog):
    """GPT-5.5 frequently returns `{"exhibitors": [...]}` — extractor must unwrap."""
    capture = SourceCapture(text="W" * 500, kind="text", source_ref="<x>")
    response = json.dumps({
        "exhibitors": [
            {"name": "WrappedCo", "source_snippet": "wrapped enough to clear the snippet floor"}
        ]
    })
    llm = FakeLLM(responses=[response])
    with caplog.at_level("WARNING", logger="event_intel.events.extraction"):
        result = extract_exhibitors(
            capture=capture, lang="en", llm_provider=llm,
            config=_config(max_chars_per_chunk=10000),
        )
    assert [c.name for c in result.candidates] == ["WrappedCo"]
    assert any("'exhibitors'" in rec.message or "auto-unwrap" in rec.message
               for rec in caplog.records)


def test_dict_wrapped_response_with_data_key_unwraps():
    capture = SourceCapture(text="D" * 500, kind="text", source_ref="<x>")
    response = json.dumps({
        "data": [
            {"name": "DataKey Inc.", "source_snippet": "long enough source snippet right here"}
        ]
    })
    llm = FakeLLM(responses=[response])
    result = extract_exhibitors(
        capture=capture, lang="en", llm_provider=llm,
        config=_config(max_chars_per_chunk=10000),
    )
    assert [c.name for c in result.candidates] == ["DataKey Inc."]


def test_single_key_dict_with_list_value_unwraps_as_fallback():
    """Unknown wrapping key — single-key dict with list value still unwraps."""
    capture = SourceCapture(text="S" * 500, kind="text", source_ref="<x>")
    response = json.dumps({
        "totally_unknown_key": [
            {"name": "FallbackCo", "source_snippet": "snippet long enough to clear the floor"}
        ]
    })
    llm = FakeLLM(responses=[response])
    result = extract_exhibitors(
        capture=capture, lang="en", llm_provider=llm,
        config=_config(max_chars_per_chunk=10000),
    )
    assert [c.name for c in result.candidates] == ["FallbackCo"]


def test_multi_key_dict_without_list_value_returns_empty():
    """No list value anywhere — extractor returns zero candidates (not crash)."""
    capture = SourceCapture(text="N" * 500, kind="text", source_ref="<x>")
    response = json.dumps({"foo": 1, "bar": "two"})
    llm = FakeLLM(responses=[response])
    result = extract_exhibitors(
        capture=capture, lang="en", llm_provider=llm,
        config=_config(max_chars_per_chunk=10000),
    )
    assert result.candidates == []


# ---------- upstream LLM failure surfaces as UPSTREAM_ERROR ----------


def test_llm_failure_surfaces_as_upstream_error(monkeypatch):
    monkeypatch.setattr(_extraction, "_CHUNK_RETRY_SLEEP_SECONDS", 0)
    capture = SourceCapture(text="S" * 500, kind="text", source_ref="<x>")

    class BadLLM:
        def __init__(self):
            self.calls = 0

        def chat_once(self, **_):
            self.calls += 1
            raise RuntimeError("anthropic 429 boom")

    bad = BadLLM()
    with pytest.raises(MCPError) as exc_info:
        extract_exhibitors(
            capture=capture, lang="en", llm_provider=bad,
            config=_config(max_chars_per_chunk=10000),
        )
    assert exc_info.value.error_code == ErrorCode.UPSTREAM_ERROR
    assert exc_info.value.retryable is True
    # one transient retry happened before giving up
    assert bad.calls == 2
    assert "after 1 retry" in exc_info.value.message


def test_llm_transient_failure_recovers_on_chunk_retry(monkeypatch):
    """One flaky call must NOT discard the whole extraction run (observed:
    64-chunk build dying at chunk 39 after ~75 min). Second attempt succeeds →
    full result + a warning recording the retry."""
    monkeypatch.setattr(_extraction, "_CHUNK_RETRY_SLEEP_SECONDS", 0)
    capture = SourceCapture(text="S" * 500, kind="text", source_ref="<x>")

    class FlakyLLM:
        def __init__(self):
            self.calls = 0

        def chat_once(self, **_):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient 500")
            return _LLMResp(
                text='[{"name": "Mobius Labs", "source_snippet": "'
                     + "x" * 30 + '", "confidence": 0.9}]',
                usage={"input_tokens": 10, "output_tokens": 5},
            )

    flaky = FlakyLLM()
    result = extract_exhibitors(
        capture=capture, lang="en", llm_provider=flaky,
        config=_config(max_chars_per_chunk=10000),
    )
    assert flaky.calls == 2
    assert [c.name for c in result.candidates] == ["Mobius Labs"]
    assert any("failed once; retried" in w for w in result.warnings)


# ---------- module-reference import smoke (project DO NOT rule) ----------


def test_module_reference_import_smoke():
    # Confirm the import-by-module pattern (events.extraction as _extraction)
    # works so MCP tool wrappers can monkeypatch through it later.
    assert hasattr(_extraction, "extract_exhibitors")
    assert hasattr(_extraction, "ExhibitorCandidate")
    assert _extraction.extract_exhibitors is extract_exhibitors
    assert _extraction.ExhibitorCandidate is ExhibitorCandidate
