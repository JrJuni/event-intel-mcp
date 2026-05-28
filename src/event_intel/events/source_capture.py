"""Capture an exhibitor-list source into a normalized text blob.

v0 source kinds (per plan §S3):
    - `html_file`  : .html / .htm on disk
    - `html_text`  : raw HTML string (e.g. operator-pasted page source)
    - `csv_file`   : CSV with at least a name column; optional url / description
    - `text`       : plain text (each line is candidate / fallback)

trafilatura is imported lazily — keep this module cold for the MCP server.

Returns `SourceCapture` carrying the cleaned text + the original kind/ref so the
extraction stage can choose lang-specific normalization downstream.

Failures fold into `MCPError(SOURCE_CAPTURE_FAILED, stage=EXTRACTION)` with a
hint dict, so the MCP tool boundary surfaces them in the standard envelope.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

from event_intel.errors import ErrorCode, MCPError, Stage

SUPPORTED_SOURCE_KINDS = ("html_file", "html_text", "csv_file", "text")

# Below this length the captured text is considered useless — extraction would
# produce zero candidates and the user would be staring at an empty tier list
# wondering why.
_MIN_CAPTURED_CHARS = 40


@dataclass
class SourceCapture:
    text: str
    kind: str
    source_ref: str
    warnings: list[str] = field(default_factory=list)
    # For CSV we keep the parsed rows so extraction can short-circuit the LLM
    # for the rows that already carry name+url+description in structured form.
    csv_rows: list[dict[str, str]] | None = None


def _raise_capture(message: str, *, hint: dict | None = None, retryable: bool = False) -> None:
    raise MCPError(
        error_code=ErrorCode.SOURCE_CAPTURE_FAILED,
        stage=Stage.EXTRACTION,
        message=message,
        hint=hint,
        retryable=retryable,
    )


def _read_file(path: Path) -> str:
    if not path.is_file():
        _raise_capture(
            f"source file not found: {path}",
            hint={"expected_path": str(path)},
        )
    return path.read_text(encoding="utf-8", errors="replace")


def _strip_html(html: str) -> str:
    """trafilatura main_text extraction. Falls back to raw HTML on failure so
    the user at least sees something instead of an empty capture."""
    import trafilatura  # lazy

    cleaned = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    return cleaned or html


def _capture_csv(path: Path) -> SourceCapture:
    raw = _read_file(path)
    try:
        reader = csv.DictReader(StringIO(raw))
        rows = [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]
    except csv.Error as exc:
        _raise_capture(
            f"failed to parse CSV {path}: {exc}",
            hint={"expected_path": str(path)},
        )
        return  # type: ignore[return-value]  # _raise_capture always raises

    if not rows:
        _raise_capture(
            f"CSV {path} has no data rows",
            hint={"expected_path": str(path), "fix": "Add at least one exhibitor row"},
        )

    # Render rows back as a deterministic text blob so the extractor can read
    # them like any other source. We keep the structured rows on the capture
    # for the extractor to use as a structured shortcut.
    lines = []
    for row in rows:
        parts = [f"{k}: {v}" for k, v in row.items() if v]
        if parts:
            lines.append(" | ".join(parts))
    text = "\n".join(lines)

    warnings: list[str] = []
    if not text:
        warnings.append("CSV had rows but every cell was empty after trim")

    return SourceCapture(
        text=text,
        kind="csv_file",
        source_ref=str(path),
        warnings=warnings,
        csv_rows=rows,
    )


def capture_source(*, source_kind: str, source_ref: str) -> SourceCapture:
    """Resolve a source descriptor to a `SourceCapture`.

    `source_ref` semantics depend on `source_kind`:
      - html_file / csv_file: filesystem path
      - html_text / text     : the raw string itself
    """
    if source_kind not in SUPPORTED_SOURCE_KINDS:
        _raise_capture(
            f"unsupported source_kind={source_kind!r}",
            hint={"supported": list(SUPPORTED_SOURCE_KINDS)},
        )

    if source_kind == "html_file":
        html = _read_file(Path(source_ref).expanduser())
        text = _strip_html(html)
        capture = SourceCapture(text=text, kind=source_kind, source_ref=source_ref)
    elif source_kind == "html_text":
        if not source_ref or not source_ref.strip():
            _raise_capture("html_text source_ref is empty")
        text = _strip_html(source_ref)
        capture = SourceCapture(text=text, kind=source_kind, source_ref="<inline html>")
    elif source_kind == "csv_file":
        capture = _capture_csv(Path(source_ref).expanduser())
    else:  # text
        if not source_ref or not source_ref.strip():
            _raise_capture("text source_ref is empty")
        capture = SourceCapture(text=source_ref.strip(), kind=source_kind, source_ref="<inline text>")

    if len(capture.text) < _MIN_CAPTURED_CHARS:
        capture.warnings.append(
            f"captured text is short ({len(capture.text)} chars < {_MIN_CAPTURED_CHARS}); "
            "extraction may yield 0 candidates"
        )

    return capture
