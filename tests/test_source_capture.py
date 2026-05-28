"""Tests for events.source_capture — HTML / CSV / text capture."""
from __future__ import annotations

import pytest

from event_intel.errors import ErrorCode, MCPError
from event_intel.events.source_capture import (
    SUPPORTED_SOURCE_KINDS,
    SourceCapture,
    capture_source,
)


def _fixture_path(repo_root, name: str):
    return repo_root / "tests" / "fixtures" / "events" / name


def test_html_file_capture_strips_chrome(repo_root):
    path = _fixture_path(repo_root, "sample_exhibitors.html")
    cap = capture_source(source_kind="html_file", source_ref=str(path))
    assert isinstance(cap, SourceCapture)
    assert cap.kind == "html_file"
    # trafilatura should keep the exhibitor names but drop the <html>/<head> chrome.
    assert "Mobius Labs" in cap.text
    assert "NeuroDrive" in cap.text
    assert "<html" not in cap.text.lower()
    assert "<head" not in cap.text.lower()


def test_csv_file_capture_parses_rows(repo_root):
    path = _fixture_path(repo_root, "sample_exhibitors.csv")
    cap = capture_source(source_kind="csv_file", source_ref=str(path))
    assert cap.kind == "csv_file"
    assert cap.csv_rows is not None
    assert len(cap.csv_rows) == 5
    names = [row["name"] for row in cap.csv_rows]
    assert "Mobius Labs" in names
    assert "Synaptik Robotics" in names
    # Rendered text contains the names too.
    assert "Mobius Labs" in cap.text


def test_html_text_capture_works_inline():
    html = "<html><body><h2>Acme Co.</h2><p>We sell widgets to fortune 500s.</p></body></html>"
    cap = capture_source(source_kind="html_text", source_ref=html)
    assert cap.kind == "html_text"
    assert "Acme" in cap.text
    assert cap.source_ref == "<inline html>"


def test_text_capture_trims_and_keeps_as_is():
    cap = capture_source(
        source_kind="text",
        source_ref="  Acme — booth 12. Sells widgets to fortune 500s.  ",
    )
    assert cap.kind == "text"
    assert cap.text.startswith("Acme")
    assert cap.source_ref == "<inline text>"


def test_unsupported_source_kind_raises_source_capture_failed():
    with pytest.raises(MCPError) as exc_info:
        capture_source(source_kind="pdf_file", source_ref="anything.pdf")
    assert exc_info.value.error_code == ErrorCode.SOURCE_CAPTURE_FAILED
    assert list(SUPPORTED_SOURCE_KINDS) == list(exc_info.value.hint["supported"])


def test_missing_html_file_raises_source_capture_failed(tmp_path):
    missing = tmp_path / "nope.html"
    with pytest.raises(MCPError) as exc_info:
        capture_source(source_kind="html_file", source_ref=str(missing))
    assert exc_info.value.error_code == ErrorCode.SOURCE_CAPTURE_FAILED
    assert str(missing) in exc_info.value.message


def test_short_capture_emits_warning():
    cap = capture_source(source_kind="text", source_ref="tiny")
    assert cap.text == "tiny"
    assert any("short" in w for w in cap.warnings), cap.warnings


def test_empty_inline_text_raises():
    with pytest.raises(MCPError) as exc_info:
        capture_source(source_kind="text", source_ref="   ")
    assert exc_info.value.error_code == ErrorCode.SOURCE_CAPTURE_FAILED
