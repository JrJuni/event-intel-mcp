"""analyze_event_page MCP tool handler — Phase 18T T1 (real implementation).

Module-reference imports for monkeypatch safety (project DO NOT rule).
Cold-start safe: no heavy ML imports at module top.
"""
from __future__ import annotations

from event_intel.acquisition import analyzer as _analyzer
from event_intel.errors import Stage, envelope_from_exception
from event_intel.providers import llm as _llm
from event_intel.runtime import preflight as _preflight


def analyze_event_page(
    url: str = "",
    *,
    lang: str = "en",
    workspace_id: str = "default",
) -> dict:
    """Classify an exhibition site URL and return acquisition hints."""
    try:
        _preflight._validate_workspace_id_minimal(workspace_id)
        if not url or not url.strip():
            from event_intel.errors import ErrorCode, MCPError
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT,
                stage=Stage.ACQUISITION,
                message="url is required",
                hint={"field": "url"},
            )

        config = _preflight.load_config()
        model = config.get("llm", {}).get("extract_exhibitors_model", "claude-sonnet-4-6")
        max_tokens = int(config.get("llm", {}).get("extract_max_tokens", 2048))

        llm_provider = _llm.AnthropicProvider(model=model)

        return _analyzer.analyze_page(
            url=url,
            lang=lang,
            llm_provider=llm_provider,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.ACQUISITION)
