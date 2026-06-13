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


def test_html_directory_preserves_heading_and_href():
    """Directory-like HTML (>= 3 links) keeps company headings AND the href URL.

    Regression for the 2026-06-05 identity bug: trafilatura dropped both the
    <h2> name and the <a href>, so the extractor saw only the bare domain anchor
    text and named rows after the domain (e.g. "llamaindex.ai") with url=None.
    """
    html = (
        "<html><body><h1>Expo Directory</h1>"
        '<div><h2>LlamaIndex</h2><p>Data framework for LLM and RAG apps.</p>'
        '<a href="https://www.llamaindex.ai">llamaindex.ai</a></div>'
        '<div><h2>Snowflake</h2><p>Cloud data warehouse.</p>'
        '<a href="https://www.snowflake.com">snowflake.com</a></div>'
        '<div><h2>ClickHouse</h2><p>OLAP database.</p>'
        '<a href="https://clickhouse.com">clickhouse.com</a></div>'
        "</body></html>"
    )
    cap = capture_source(source_kind="html_text", source_ref=html)
    # Heading names survive (not just the domain).
    assert "LlamaIndex" in cap.text
    assert "Snowflake" in cap.text
    # Full href URLs survive so the extractor can fill `url`.
    assert "https://www.llamaindex.ai" in cap.text
    assert "https://www.snowflake.com" in cap.text
    # Heading lands on its own line — not glued to the previous entry.
    assert "\nLlamaIndex\n" in ("\n" + cap.text + "\n")


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


def test_csv_ragged_row_does_not_crash(tmp_path):
    """An unquoted comma in a field makes a row wider than the header — the extras
    land under the restkey as a list. Must not crash (was AttributeError on .strip)."""
    p = tmp_path / "ragged.csv"
    p.write_text(
        "company,description\nAcme,Builds X, Y, and Z products\nBeta,Normal desc\n",
        encoding="utf-8",
    )
    cap = capture_source(source_kind="csv_file", source_ref=str(p))
    assert cap.csv_rows is not None and len(cap.csv_rows) == 2
    acme = cap.csv_rows[0]
    assert acme["company"] == "Acme" and "Builds X" in acme["description"]
    # overflow from the unquoted commas is folded, not lost or crashed.
    assert "Y" in acme.get("_overflow", "") and "Z" in acme.get("_overflow", "")
    assert "Acme" in cap.text


def test_csv_short_row_fills_missing_with_empty(tmp_path):
    """A row with fewer columns than the header fills missing cells with '' (restval)."""
    p = tmp_path / "short.csv"
    p.write_text("company,description\nAcme\n", encoding="utf-8")
    cap = capture_source(source_kind="csv_file", source_ref=str(p))
    assert cap.csv_rows[0]["company"] == "Acme"
    assert cap.csv_rows[0]["description"] == ""


def test_csv_utf8_bom_header_is_stripped(tmp_path):
    """Excel / Windows CSV exports prepend a UTF-8 BOM (U+FEFF). It is NOT
    whitespace, so a header.strip() leaves it glued to the first column name
    ('\\ufeffcompany_name') — which silently defeats the CSV name-column
    short-circuit and forces the expensive LLM path. Regression for the
    Hannover×Siemens 2,885-row smoke that fell to the LLM path on this.

    utf-8-sig is exactly how Excel/Windows write the BOM, so it reproduces the
    real failure rather than a synthetic one.
    """
    p = tmp_path / "bom.csv"
    p.write_text(
        "company_name,detail_url\nAcme,https://acme.example\n",
        encoding="utf-8-sig",
    )
    cap = capture_source(source_kind="csv_file", source_ref=str(p))
    assert cap.csv_rows is not None and len(cap.csv_rows) == 1
    keys = list(cap.csv_rows[0].keys())
    # The first header is the clean name, NOT '\ufeffcompany_name'.
    assert keys[0] == "company_name"
    assert not any(k.startswith("\ufeff") for k in keys)
    assert cap.csv_rows[0]["company_name"] == "Acme"


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


# ---------- text_file (Phase 18T) ----------


def test_text_file_reads_file_contents_as_inline_text(tmp_path):
    """text_file source kind reads a filesystem path and returns contents as inline
    text — used by the acquisition layer when it writes JSON blobs to disk."""
    p = tmp_path / "exhibitors.json"
    content = '[{"name": "ExhibitorA", "description": "an AI company with long description for min"}]'
    p.write_text(content, encoding="utf-8")
    cap = capture_source(source_kind="text_file", source_ref=str(p))
    assert cap.kind == "text_file"
    assert cap.source_ref == str(p)
    assert cap.text == content
    assert cap.csv_rows is None


def test_text_file_missing_path_raises_source_capture_failed(tmp_path):
    with pytest.raises(MCPError) as ei:
        capture_source(source_kind="text_file", source_ref=str(tmp_path / "nonexistent.txt"))
    assert ei.value.error_code == ErrorCode.SOURCE_CAPTURE_FAILED


def test_text_file_is_in_supported_kinds():
    assert "text_file" in SUPPORTED_SOURCE_KINDS
