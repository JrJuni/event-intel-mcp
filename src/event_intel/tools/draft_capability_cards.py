"""draft_capability_cards MCP tool handler. Wraps cards.drafter.draft_cards in
the MCPError envelope convention.

IMPORTANT: imports drafter / provider modules by reference, not symbol, so
tests can monkeypatch them through the tool boundary (project DO NOT rule
for monkeypatchable deps). Module top has zero heavy imports — safe under
the cold-start regression.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from event_intel.cards import drafter as _drafter
from event_intel.errors import ErrorCode, MCPError, Stage, envelope_from_exception
from event_intel.providers import llm as _llm
from event_intel.runtime import preflight as _preflight


def _resolve_output_path(workspace_id: str, out_path: str | None) -> Path:
    if out_path:
        return Path(out_path).expanduser()
    return Path("outputs") / workspace_id / "capability_cards.draft.yaml"


def draft_capability_cards(
    *,
    workspace_id: str = "default",
    source_kind: str = "text",
    source_content: str = "",
    source_paths: list[str] | None = None,
    lang: str = "en",
    out_path: str | None = None,
    model: str | None = None,
) -> dict:
    """Draft capability_cards.yaml from source material and write it to disk.

    Returns:
        Success envelope with `draft_path` + `warnings` + `usage`.
        Failure envelope from `envelope_from_exception`.
    """
    try:
        _preflight._validate_workspace_id_minimal(workspace_id)
        config = _preflight.load_config()
        chosen_model = model or config["llm"]["draft_cards_model"]
        max_tokens = int(config["llm"].get("draft_cards_max_tokens", 4096))

        llm_provider = _llm.AnthropicProvider(model=chosen_model)
        ping = llm_provider.ping()
        if ping.get("status") != "ok":
            raise MCPError(
                error_code=ErrorCode.CONFIG_ERROR,
                stage=Stage.INGEST,
                message="ANTHROPIC_API_KEY missing or invalid",
                hint={"fix": "Set ANTHROPIC_API_KEY in .env"},
                retryable=False,
            )

        result = _drafter.draft_cards(
            source_kind=source_kind,
            source_content=source_content,
            source_paths=source_paths,
            lang=lang,
            llm_provider=llm_provider,
            max_tokens=max_tokens,
        )

        # Sanity-check the YAML is at least parseable before writing — the
        # human will edit it anyway, but a malformed draft wastes everyone's
        # time. We do not validate against the pydantic schema here on purpose:
        # drafts are allowed to be incomplete.
        try:
            yaml.safe_load(result.yaml_text)
        except yaml.YAMLError as exc:
            raise MCPError(
                error_code=ErrorCode.UPSTREAM_ERROR,
                stage=Stage.INGEST,
                message=f"LLM returned non-YAML output: {exc}",
                hint={"raw_output_preview": result.yaml_text[:500]},
                retryable=True,
            ) from exc

        draft_path = _resolve_output_path(workspace_id, out_path)
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(result.yaml_text, encoding="utf-8")

        return {
            "ok": True,
            "draft_path": str(draft_path),
            "warnings": result.warnings,
            "model": result.model,
            "usage": result.usage,
            "lang": lang,
        }
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.INGEST)
