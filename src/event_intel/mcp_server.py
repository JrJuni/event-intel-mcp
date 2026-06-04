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

from dotenv import load_dotenv  # noqa: E402

# Load .env (project root) so API keys are available to provider modules.
# Silent no-op if .env is absent — env vars already set by Claude Desktop win.
load_dotenv()

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
def check_runtime(workspace_id: str = "default", warm_up: bool = False) -> dict:
    """Verify embedding model / vectorstore / API keys / product context (S1).

    Set warm_up=true to START loading the bge-m3 model in the background so the
    first build_event_tier_list call is fast. The call returns immediately (it
    will not block on the ~10-20s load). Check `checks.warm_up.status`: when it
    reads "ready" the model is loaded. If it reads "warming", just call
    check_runtime again in a minute to poll.
    """
    from event_intel.tools.check_runtime import check_runtime as _impl

    # Always non-blocking on the MCP surface (warm_up_block stays False) — only
    # the terminal CLI loads inline.
    return _impl(workspace_id=workspace_id, warm_up=warm_up)


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
    max_companies: int | None = None,
    enrichment_enabled: bool = True,
    resume_from: str | None = None,
    run_rationale: bool = True,
) -> dict:
    """Build a tiered exhibitor list from an event source (S3+S4+S5)."""
    from event_intel.tools.build_event_tier_list import build_event_tier_list as _impl

    return _impl(
        workspace_id=workspace_id,
        event_name=event_name,
        event_slug=event_slug,
        source_kind=source_kind,
        source_ref=source_ref,
        lang=lang,
        max_companies=max_companies,
        enrichment_enabled=enrichment_enabled,
        resume_from=resume_from,
        run_rationale=run_rationale,
    )


@app.tool()
def analyze_event_page(
    url: str = "",
    lang: str = "en",
    workspace_id: str = "default",
) -> dict:
    """Classify an exhibition site URL and return acquisition hints (Phase 18T)."""
    from event_intel.tools.analyze_event_page import analyze_event_page as _impl

    return _impl(url=url, lang=lang, workspace_id=workspace_id)


@app.tool()
def probe_exhibitor_endpoint(
    url: str = "",
    hints: dict | None = None,
    lang: str = "en",
) -> dict:
    """Given analyzer hints, probe XHR/embedded-JSON endpoints (Phase 18T)."""
    from event_intel.tools.probe_exhibitor_endpoint import probe_exhibitor_endpoint as _impl

    return _impl(url=url, hints=hints, lang=lang)


@app.tool()
def acquire_exhibitor_source(
    url: str = "",
    workspace_id: str = "default",
    event_slug: str = "",
    lang: str = "en",
    refetch: bool = False,
) -> dict:
    """Analyze → probe → fetch → artifact → (source_kind, source_ref) (Phase 18T)."""
    from event_intel.tools.acquire_exhibitor_source import acquire_exhibitor_source as _impl

    return _impl(
        url=url,
        workspace_id=workspace_id,
        event_slug=event_slug,
        lang=lang,
        refetch=refetch,
    )


def main() -> None:
    """Entrypoint for `python -m event_intel.mcp_server`."""
    app.run()


if __name__ == "__main__":
    main()
