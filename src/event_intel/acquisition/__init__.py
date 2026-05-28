"""Phase 18T — AI-assisted source acquisition layer.

Three MCP tools form this layer:
    analyze_event_page     — Sonnet classifies the page (1 LLM call)
    probe_exhibitor_endpoint — deterministic candidate probe (0 LLM)
    acquire_exhibitor_source — orchestrator → (source_kind, source_ref)

The core pipeline (build_event_tier_list, S3-S5) stays locked.
Acquisition is strictly upstream: it emits a (source_kind, source_ref)
pair that pipes into the existing surface unchanged.
"""
