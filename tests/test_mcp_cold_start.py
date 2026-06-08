"""Cold-start regression — see plan v0.5 Contract #13.

Importing event_intel.mcp_server MUST NOT pull heavy ML libraries into sys.modules.
A regression here causes the 4-minute hang Claude Desktop observed in bd-coldcall-agent
(see CLAUDE.md DO NOT entry for Phase 14C-B).
"""
from __future__ import annotations

import importlib
import sys

import pytest

FORBIDDEN_HEAVY = (
    "torch",
    "transformers",
    "sentence_transformers",
    "chromadb",
    "bitsandbytes",
    # CJK morphological backends (Phase 18W P2-4 / 18X) must stay lazy — imported
    # only inside scoring/cjk.py segmenter factories, never at module load.
    "janome",
    "jieba",
    "kiwipiepy",
    "kiwipiepy_model",
)


def _purge(prefix: str) -> None:
    for mod in list(sys.modules.keys()):
        if mod == prefix or mod.startswith(prefix + "."):
            sys.modules.pop(mod, None)


@pytest.fixture
def fresh_sys_modules():
    """Reset only event_intel.* and FORBIDDEN_HEAVY on teardown so the next test
    measures a real cold start. Do NOT snapshot-restore the entire sys.modules:
    pydantic uses lazy __getattr__ for RootModel and other submodules, and
    `from pydantic import X` caches the attribute on the parent package without
    re-triggering the lazy load on subsequent calls — so blindly removing any
    module that wasn't in the snapshot can wedge later re-imports with
    `KeyError: 'pydantic.root_model'`.
    """
    yield
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)


def test_import_mcp_server_does_not_load_heavy_ml(fresh_sys_modules):
    # Purge any prior import so the check measures a real cold start.
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.mcp_server")

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, (
        f"Cold-start regression: importing event_intel.mcp_server leaked {leaked} "
        "into sys.modules. Move the offending import inside a function body."
    )


def test_import_providers_package_is_cold(fresh_sys_modules):
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.providers.llm")
    importlib.import_module("event_intel.providers.embedding")
    importlib.import_module("event_intel.providers.vectorstore")
    importlib.import_module("event_intel.providers.search")
    importlib.import_module("event_intel.providers.fetch")

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, (
        f"Provider modules leaked heavy ML imports: {leaked}. "
        "All heavy deps must be imported inside method bodies, not at module top."
    )


def test_build_event_tier_list_module_keeps_cold(fresh_sys_modules):
    """S6: tools.build_event_tier_list pulls in source_capture/extraction/
    enrichment/retriever/scoring/report at module top — none of which may
    leak heavy ML deps. Heavy deps stay behind provider lazy-load."""
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.tools.build_event_tier_list")

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, (
        f"build_event_tier_list tool module leaked heavy ML imports: {leaked}. "
        "Every heavy dep must stay behind a provider lazy import."
    )


def test_storage_identifiers_module_is_cold(fresh_sys_modules):
    """S6: storage.identifiers is on every MCP tool's hot path (slug sanitize).
    Must be import-cold."""
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.storage.identifiers")

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, f"storage.identifiers leaked heavy ML imports: {leaked}"


def test_runtime_paths_module_is_cold(fresh_sys_modules):
    """W0: runtime.paths is the central path resolver, imported by
    providers.vectorstore + storage.artifacts (both on cold paths). It is
    stdlib-only and must never pull heavy ML deps at import."""
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.runtime.paths")
    importlib.import_module("event_intel.storage.migrate")  # W5 migration (stdlib)
    importlib.import_module("event_intel.runtime.async_job")  # #14 P1.0 (stdlib)
    importlib.import_module("event_intel.runtime.models")  # #14: cold (heavy import in-body)
    importlib.import_module("event_intel.tools.prepare_models")  # #14 P1.1 tool
    importlib.import_module("event_intel.tools.login_chatgpt")  # #14 P1.2 tool

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, f"runtime.paths leaked heavy ML imports: {leaked}"


def test_sources_indexer_module_is_cold(fresh_sys_modules):
    """W1: sources.indexer parses PDF/MD/TXT/CSV → product_sources_{ws}. pypdf is
    lazy-loaded in-body; embedding/vectorstore providers are injected. Module
    import must stay cold (no torch/chromadb/pypdf-pulled heavy deps)."""
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.sources")
    importlib.import_module("event_intel.sources.indexer")
    importlib.import_module("event_intel.sources.retrieval")  # W3 workspace drafting
    importlib.import_module("event_intel.tools.sync_product_sources")  # W2 tool

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, f"sources.indexer leaked heavy ML imports: {leaked}"


def test_cards_tools_keep_module_top_cold(fresh_sys_modules):
    """S2: tools/{draft,validate,ingest}_capability_cards must not pull heavy ML
    libs at module import — only on first real call (which still needs them via
    embedding/vectorstore providers)."""
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.tools.draft_capability_cards")
    importlib.import_module("event_intel.tools.validate_capability_cards")
    importlib.import_module("event_intel.tools.ingest_capability_cards")
    importlib.import_module("event_intel.tools.draft_labels")  # L6 labeling tool
    importlib.import_module("event_intel.cards.schema")
    importlib.import_module("event_intel.cards.validator")
    importlib.import_module("event_intel.cards.drafter")
    importlib.import_module("event_intel.cards.ingest")

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, (
        f"Cards tools / modules leaked heavy ML imports: {leaked}. "
        "All heavy deps must be imported inside method bodies, not at module top."
    )


def test_s4_modules_keep_module_top_cold(fresh_sys_modules):
    """S4: enrichment / rag.retriever / scoring.* must NOT pull heavy ML libs
    at module top. Embedding + vectorstore providers come in as args so the
    heavy deps stay quarantined."""
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.events.enrichment")
    importlib.import_module("event_intel.rag.retriever")
    importlib.import_module("event_intel.scoring.dimensions")
    importlib.import_module("event_intel.scoring.rules")
    importlib.import_module("event_intel.scoring.compute")

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, (
        f"S4 modules leaked heavy ML imports: {leaked}. "
        "All heavy deps must be imported inside method bodies, not at module top."
    )


def test_eval_modules_keep_module_top_cold(fresh_sys_modules):
    """Phase 18V: eval.metrics / eval.harness are pure stdlib + cold scoring
    imports — they must NOT pull heavy ML libs at module top (the eval matrix
    runs in CI and as a CLI without an embedding model loaded)."""
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.eval")
    importlib.import_module("event_intel.eval.metrics")
    importlib.import_module("event_intel.eval.harness")
    importlib.import_module("event_intel.eval.roster")
    importlib.import_module("event_intel.eval.blind")
    importlib.import_module("event_intel.eval.benchmark")
    importlib.import_module("event_intel.eval.replay")
    importlib.import_module("event_intel.eval.labeling")
    importlib.import_module("event_intel.eval.label_draft")
    importlib.import_module("event_intel.eval.label_refine")

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, (
        f"Phase 18V eval modules leaked heavy ML imports: {leaked}. "
        "Eval must stay deps-free (stdlib + cold scoring only)."
    )


def test_s5_report_modules_keep_module_top_cold(fresh_sys_modules):
    """S5: report/* must NOT pull heavy ML libs at module top — rendering is
    pure markdown/yaml so this is just a regression guard."""
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.report.tier_list_md")
    importlib.import_module("event_intel.report.tier_list_yaml")
    importlib.import_module("event_intel.report.brief_export")

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, (
        f"S5 report modules leaked heavy ML imports: {leaked}. "
        "Rendering should be deps-free."
    )


def test_events_modules_keep_module_top_cold(fresh_sys_modules):
    """S3: events.source_capture / events.extraction must NOT pull heavy ML
    libs at module import — trafilatura is the heaviest dep and is lazy-loaded
    inside source_capture._strip_html; nothing else should leak either."""
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.events.source_capture")
    importlib.import_module("event_intel.events.extraction")
    importlib.import_module("event_intel.events.evidence")
    importlib.import_module("event_intel.events.run_summary")

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, (
        f"Events modules leaked heavy ML imports: {leaked}. "
        "All heavy deps must be imported inside method bodies, not at module top."
    )


def test_t05_acquisition_safety_modules_keep_module_top_cold(fresh_sys_modules):
    """T0.5: url_safety / robots / raw_fetch / http_status_map must NOT pull heavy
    ML libs at module import — they only need stdlib + httpx (lazy in raw_fetch)."""
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.acquisition.url_safety")
    importlib.import_module("event_intel.acquisition.robots")
    importlib.import_module("event_intel.acquisition.raw_fetch")
    importlib.import_module("event_intel.acquisition.http_status_map")

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, (
        f"T0.5 acquisition safety modules leaked heavy ML imports: {leaked}."
    )


def test_t0_acquisition_modules_keep_module_top_cold(fresh_sys_modules):
    """Phase 18T T0: acquisition package + storage.artifacts + 3 stub tools
    must NOT pull heavy ML libs at module import."""
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.acquisition")
    importlib.import_module("event_intel.storage.artifacts")
    importlib.import_module("event_intel.tools.analyze_event_page")
    importlib.import_module("event_intel.tools.probe_exhibitor_endpoint")
    importlib.import_module("event_intel.tools.acquire_exhibitor_source")

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, (
        f"Phase 18T T0 acquisition modules leaked heavy ML imports: {leaked}. "
        "All heavy deps must stay behind provider lazy imports."
    )


def test_t0_stub_tools_return_acquisition_envelope(fresh_sys_modules):
    """Phase 18T: acquire is still a stub (T3); analyze + probe have real impls.
    - acquire_exhibitor_source returns INTERNAL (stub).
    - analyze_event_page returns INVALID_INPUT (empty url) — real impl.
    - probe_exhibitor_endpoint returns INVALID_INPUT (empty url) — real impl (T2).
    """
    _purge("event_intel")
    server = importlib.import_module("event_intel.mcp_server")

    # All three acquisition tools now have real impls (T1/T2/T3).
    # Empty url → INVALID_INPUT (caught at the boundary before any network call).
    for tool_name, kwargs in [
        ("analyze_event_page", {"url": ""}),
        ("probe_exhibitor_endpoint", {"url": "", "hints": {}}),
        ("acquire_exhibitor_source", {"url": "", "workspace_id": "default", "event_slug": "x"}),
    ]:
        result = getattr(server, tool_name)(**kwargs)
        assert isinstance(result, dict), f"{tool_name} did not return dict"
        assert result["ok"] is False
        assert result["error_code"] == "INVALID_INPUT"
        assert result["stage"] == "acquisition"


def test_acquire_module_keeps_module_top_cold(fresh_sys_modules):
    """Phase 18T T3: acquisition.acquire + tools.acquire_exhibitor_source must NOT
    pull heavy ML libs at module import — acquire is pure orchestration code."""
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.acquisition.acquire")
    importlib.import_module("event_intel.tools.acquire_exhibitor_source")

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, (
        f"Phase 18T T3 acquire modules leaked heavy ML imports: {leaked}. "
        "All heavy deps must stay behind provider lazy imports."
    )


def test_probe_module_keeps_module_top_cold(fresh_sys_modules):
    """Phase 18T T2: acquisition.probe + tools.probe_exhibitor_endpoint must NOT
    pull heavy ML libs at module import — probe is pure httpx + stdlib."""
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)

    importlib.import_module("event_intel.acquisition.probe")
    importlib.import_module("event_intel.tools.probe_exhibitor_endpoint")

    leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
    assert not leaked, (
        f"Phase 18T T2 probe modules leaked heavy ML imports: {leaked}. "
        "probe.py is pure code — no ML deps should appear at module top."
    )


def test_check_runtime_tool_returns_envelope(fresh_sys_modules):
    """check_runtime should always return an envelope (ok bool present), never raise.
    Whether ok=True or ok=False depends on the local machine state (bge-m3 cached,
    keys set, product context ingested) so we only assert envelope shape.
    """
    _purge("event_intel")
    server = importlib.import_module("event_intel.mcp_server")
    result = server.check_runtime(workspace_id="default")
    assert isinstance(result, dict)
    assert "ok" in result
    if result["ok"] is False:
        for key in ("error_code", "stage", "message"):
            assert key in result, f"failure envelope missing `{key}`"
