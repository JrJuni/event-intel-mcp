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

from event_intel._env import load_project_env  # noqa: E402

# Load the repo's .env so API keys are available regardless of the cwd Claude
# Desktop spawns us in. Non-empty env (e.g. .mcpb form keys) wins; blank form
# fields fall back to .env. Silent no-op if .env is absent.
load_project_env()

from mcp.server.fastmcp import FastMCP  # noqa: E402

from event_intel.errors import ErrorCode, MCPError, Stage  # noqa: E402

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
    refresh: bool = False,
    enrichment_enabled: bool = True,
    resume_from: str | None = None,
    run_rationale: bool = True,
    target_mode: str | None = None,
) -> dict:
    """Build a tiered exhibitor list from an event source (S3+S4+S5).

    `target_mode` (customer | partner | ecosystem) controls competitor handling;
    None resolves via user config → card default → "customer" (Phase 18V item 2).
    """
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
        refresh=refresh,
        run_rationale=run_rationale,
        target_mode=target_mode,
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


def _preimport_heavy_deps() -> None:
    """Import heavy native deps on the MAIN thread before serving.

    FastMCP runs sync tool handlers in a worker thread. The first import of
    chromadb inside that worker thread hangs indefinitely in the Claude Desktop
    stdio server — reproduced 2026-06-04: a cold `check_runtime` got NO response
    in 240s, while pre-importing chromadb on the main thread first made the same
    call return in ~1.8s. (A plain asyncio+executor harness does NOT reproduce it,
    so the trigger is specific to FastMCP's execution context, not "any off-main
    import".) The exact mechanism is unconfirmed, but the fix is proven: do the
    import on the main thread once, at startup, so no tool-call worker thread ever
    performs the first import.

    sentence_transformers is pre-imported defensively against the identical failure
    mode in build/ingest, whose embed path lazily imports it in a worker thread too.

    Best-effort: a failed import is logged to stderr and left for the tool to
    surface as a proper MCPError, not crash startup. MUST run in main() (NOT at
    module import) so the cold-start contract (test_mcp_cold_start) still holds.
    """
    import sys

    for mod in ("chromadb", "sentence_transformers"):
        try:
            __import__(mod)
        except Exception as exc:  # noqa: BLE001 — defer real failures to the tool boundary
            sys.stderr.write(f"event-intel: startup pre-import of {mod} failed: {exc}\n")


def main() -> None:
    """Entrypoint for `python -m event_intel.mcp_server`."""
    # Pre-import heavy native deps on the MAIN thread (chromadb deadlocks if first
    # imported in a FastMCP worker thread). See _preimport_heavy_deps docstring.
    _preimport_heavy_deps()

    # Opt-in background warm-up (EVENT_INTEL_WARM_ON_START). No-op unless enabled
    # AND bge-m3 is already cached. Non-blocking — never delays server boot.
    from event_intel.runtime import warmup as _warmup

    _warmup.maybe_warm_on_start()
    app.run()


if __name__ == "__main__":
    main()
