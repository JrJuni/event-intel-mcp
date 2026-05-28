"""FastMCP entry point. Stdio server for Claude Desktop / CLI.

CRITICAL: No heavy ML imports (torch / transformers / sentence_transformers / chromadb)
at module load. All tool handlers must defer such imports until called.

Cold-start regression is enforced by tests/test_mcp_cold_start.py.
"""
from __future__ import annotations

import sys

# UTF-8 stdio reconfigure — required for Windows. Must run before any framework I/O.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, AttributeError):
            pass

from mcp.server.fastmcp import FastMCP  # noqa: E402

from event_intel.errors import ErrorCode, MCPError, Stage, envelope_from_exception  # noqa: E402

app = FastMCP("event-intel")


def _not_implemented(tool_name: str) -> dict:
    """S0 placeholder. Each tool replaces this stub in its dedicated stream."""
    return MCPError(
        error_code=ErrorCode.INTERNAL,
        stage=Stage.PREFLIGHT,
        message=f"{tool_name} is not implemented yet (S0 scaffold)",
        hint="Implementation lands in a later stream — see plan v0.5",
        retryable=False,
    ).to_envelope()


@app.tool()
def check_runtime(workspace_id: str = "default") -> dict:
    """Verify embedding model / vectorstore / API keys / product context (S1)."""
    from event_intel.tools.check_runtime import check_runtime as _impl

    return _impl(workspace_id=workspace_id)


@app.tool()
def draft_capability_cards(
    workspace_id: str = "default",
    source_kind: str = "text",
    source_content: str = "",
    source_paths: list[str] | None = None,
    lang: str = "en",
    out_path: str | None = None,
) -> dict:
    """Draft capability_cards.yaml from source docs/text (S2)."""
    from event_intel.tools.draft_capability_cards import (
        draft_capability_cards as _impl,
    )

    return _impl(
        workspace_id=workspace_id,
        source_kind=source_kind,
        source_content=source_content,
        source_paths=source_paths,
        lang=lang,
        out_path=out_path,
    )


@app.tool()
def validate_capability_cards(cards_path: str) -> dict:
    """Validate a hand-edited capability_cards.yaml against schema v1 (S2)."""
    from event_intel.tools.validate_capability_cards import (
        validate_capability_cards as _impl,
    )

    return _impl(cards_path=cards_path)


@app.tool()
def ingest_product_context(
    workspace_id: str = "default",
    cards_path: str = "",
    extra_source_paths: list[str] | None = None,
) -> dict:
    """Ingest validated capability cards into the Product Context mini-RAG (S2)."""
    from event_intel.tools.ingest_capability_cards import (
        ingest_product_context as _impl,
    )

    return _impl(
        workspace_id=workspace_id,
        cards_path=cards_path,
        extra_source_paths=extra_source_paths,
    )


@app.tool()
def build_event_tier_list(
    workspace_id: str = "default",
    event_name: str = "",
    event_slug: str = "",
    source_kind: str = "html_file",
    source_ref: str = "",
    lang: str = "en",
    max_companies: int = 30,
    enrichment_enabled: bool = True,
    resume_from: str | None = None,
) -> dict:
    """Build a tiered exhibitor list from an event source (S3+S4+S5)."""
    try:
        return _not_implemented("build_event_tier_list")
    except Exception as e:
        return envelope_from_exception(e, stage=Stage.PREFLIGHT)


def main() -> None:
    """Entrypoint for `python -m event_intel.mcp_server`."""
    app.run()


if __name__ == "__main__":
    main()
