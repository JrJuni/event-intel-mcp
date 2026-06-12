"""Tests for cost lever #16-① — CSV direct-conversion short-circuit.

A structured CSV roster with a detectable name column converts straight to
candidates with ZERO LLM calls; detection failure or the off switch falls back
to the chunked LLM path unchanged.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from event_intel.events.extraction import extract_exhibitors
from event_intel.events.source_capture import SourceCapture, capture_source

# ---------- fakes ----------


@dataclass
class _LLMResp:
    text: str
    usage: dict[str, int]
    model: str = "fake-claude"
    stop_reason: str | None = None


class FakeLLM:
    def __init__(self, responses: list[str] | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def chat_once(self, *, system: str, user: str, max_tokens: int, temperature: float):
        self.calls.append({"system": system, "user": user})
        text = self.responses.pop(0) if self.responses else "[]"
        return _LLMResp(text=text, usage={"input_tokens": 10, "output_tokens": 5})


class ExplodingLLM:
    """Any call is a test failure — the short-circuit must never reach the LLM."""

    def chat_once(self, **_):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("LLM was called despite CSV short-circuit")


def _config(**overrides):
    cfg = {
        "extraction": {
            "max_chars_per_chunk": 8000,
            "max_chunks_per_event": 12,
            "source_snippet_min_chars": 20,
            "extraction_confidence_min": 0.6,
        },
        "llm": {"extract_max_tokens": 1024},
    }
    cfg["extraction"].update(overrides)
    return cfg


def _csv_capture(rows: list[dict[str, str]], text: str = "filler " * 20) -> SourceCapture:
    return SourceCapture(text=text, kind="csv_file", source_ref="roster.csv", csv_rows=rows)


# ---------- happy path: english headers ----------


def test_csv_short_circuit_zero_llm_calls_and_preserves_fields():
    rows = [
        {"Company": f"Acme Robotics {i}", "Website": f"https://acme{i}.example",
         "Description": f"industrial robot arms vendor number {i}"}
        for i in range(5)
    ]
    result = extract_exhibitors(
        capture=_csv_capture(rows), lang="en",
        llm_provider=ExplodingLLM(), config=_config(),
    )
    assert len(result.candidates) == 5
    assert result.chunks_processed == 0 and result.chunks_total == 0
    assert result.usage == {"input_tokens": 0, "output_tokens": 0}
    cand = next(c for c in result.candidates if c.name == "Acme Robotics 2")
    assert cand.url == "https://acme2.example"
    assert cand.description == "industrial robot arms vendor number 2"
    assert cand.extraction_confidence == 1.0
    assert cand.chunk_indices == [2]
    assert "Acme Robotics 2" in cand.source_snippet
    assert any("CSV short-circuit" in w for w in result.warnings)
    # confidence is 1.0 for every direct row → nothing routes to needs_review
    assert result.needs_review == []


# ---------- korean headers ----------


def test_csv_short_circuit_korean_headers():
    rows = [
        {"회사명": "㈜모비우스랩", "홈페이지": "https://mobius.example.kr",
         "설명": "엣지 AI 온디바이스 NPU 컴파일러"},
        {"회사명": "뉴로드라이브", "홈페이지": "", "설명": "자율주행 인지 스택"},
    ]
    result = extract_exhibitors(
        capture=_csv_capture(rows), lang="ko",
        llm_provider=ExplodingLLM(), config=_config(),
    )
    names = {c.name for c in result.candidates}
    assert names == {"㈜모비우스랩", "뉴로드라이브"}
    cand = next(c for c in result.candidates if c.name == "㈜모비우스랩")
    assert cand.url == "https://mobius.example.kr"
    assert cand.description == "엣지 AI 온디바이스 NPU 컴파일러"


# ---------- header detection priority + case-insensitivity ----------


def test_csv_name_column_priority_and_case_insensitive():
    # "NAME" (priority 1) wins over "exhibitor" even though both are present.
    rows = [{"NAME": "Priority Pick GmbH", "exhibitor": "Wrong Pick",
             "URL": "https://pick.example"}]
    result = extract_exhibitors(
        capture=_csv_capture(rows), lang="en",
        llm_provider=ExplodingLLM(), config=_config(),
    )
    assert [c.name for c in result.candidates] == ["Priority Pick GmbH"]
    assert result.candidates[0].url == "https://pick.example"


# ---------- fallback: no name column ----------


def test_csv_without_name_column_falls_back_to_llm():
    rows = [{"booth": "A-101", "hall": "West"}]
    canned = json.dumps([
        {"name": "Fallback Co.", "source_snippet": "booth A-101 in the West hall area"},
    ])
    llm = FakeLLM(responses=[canned])
    result = extract_exhibitors(
        capture=_csv_capture(rows, text="booth: A-101 | hall: West " * 4),
        lang="en", llm_provider=llm, config=_config(),
    )
    assert len(llm.calls) >= 1
    assert [c.name for c in result.candidates] == ["Fallback Co."]
    assert any("no name column" in w for w in result.warnings)


# ---------- off switch ----------


def test_csv_short_circuit_off_switch_uses_llm():
    rows = [{"company": "Acme Robotics", "url": "https://acme.example"}]
    canned = json.dumps([
        {"name": "Acme Robotics", "source_snippet": "company: Acme Robotics listed here"},
    ])
    llm = FakeLLM(responses=[canned])
    result = extract_exhibitors(
        capture=_csv_capture(rows), lang="en",
        llm_provider=llm, config=_config(csv_short_circuit=False),
    )
    assert len(llm.calls) >= 1
    assert [c.name for c in result.candidates] == ["Acme Robotics"]
    assert not any("CSV short-circuit" in w for w in result.warnings)


# ---------- short rows get a synthetic snippet that clears the floor ----------


def test_csv_short_row_synthesizes_snippet_above_floor():
    rows = [{"name": "AB"}]  # joined cells = "AB" (2 chars < 20)
    result = extract_exhibitors(
        capture=_csv_capture(rows), lang="en",
        llm_provider=ExplodingLLM(), config=_config(),
    )
    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert cand.name == "AB"
    assert len(cand.source_snippet) >= 20
    assert cand.source_snippet.startswith("CSV row 0 of roster.csv:")
    assert result.dropped_low_snippet == 0


# ---------- empty-name rows skipped, all-empty names fall back ----------


def test_csv_rows_with_blank_names_are_skipped():
    rows = [
        {"name": "Real Co.", "description": "a real exhibitor with a description"},
        {"name": "", "description": "orphan row without any name cell"},
    ]
    result = extract_exhibitors(
        capture=_csv_capture(rows), lang="en",
        llm_provider=ExplodingLLM(), config=_config(),
    )
    assert [c.name for c in result.candidates] == ["Real Co."]


def test_csv_all_names_blank_falls_back_to_llm():
    rows = [{"name": "", "description": "no names anywhere in this file"}]
    llm = FakeLLM(responses=["[]"])
    result = extract_exhibitors(
        capture=_csv_capture(rows, text="description: no names anywhere " * 3),
        lang="en", llm_provider=llm, config=_config(),
    )
    assert len(llm.calls) >= 1
    assert result.candidates == []


# ---------- duplicate rows merge through the shared dedup ----------


def test_csv_duplicate_names_merge():
    rows = [
        {"company": "Mobius Labs Inc.", "website": "https://mobius.example",
         "description": "on-device NPU compiler stack"},
        {"company": "Mobius Labs", "website": "",
         "description": "duplicate listing of the same vendor"},
    ]
    result = extract_exhibitors(
        capture=_csv_capture(rows), lang="en",
        llm_provider=ExplodingLLM(), config=_config(),
    )
    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert cand.name == "Mobius Labs Inc."  # first-seen wins
    assert cand.url == "https://mobius.example"
    assert cand.chunk_indices == [0, 1]  # both source rows auditable


# ---------- end-to-end through capture_source incl. ragged rows ----------


def test_csv_short_circuit_end_to_end_with_ragged_row(tmp_path):
    csv_path = tmp_path / "roster.csv"
    csv_path.write_text(
        "company,website,description\n"
        "Acme Robotics,https://acme.example,industrial arms vendor\n"
        # ragged: unquoted comma in description spills into _overflow
        "EdgeVision,https://edge.example,vision SDK, smart-city cameras\n",
        encoding="utf-8",
    )
    capture = capture_source(source_kind="csv_file", source_ref=str(csv_path))
    result = extract_exhibitors(
        capture=capture, lang="en",
        llm_provider=ExplodingLLM(), config=_config(),
    )
    names = {c.name for c in result.candidates}
    assert names == {"Acme Robotics", "EdgeVision"}
    edge = next(c for c in result.candidates if c.name == "EdgeVision")
    # overflow cell still lands in the snippet for audit
    assert "smart-city cameras" in edge.source_snippet
