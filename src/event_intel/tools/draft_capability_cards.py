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
from event_intel.providers import embedding as _embedding
from event_intel.providers import llm as _llm
from event_intel.providers import vectorstore as _vectorstore
from event_intel.runtime import paths as _paths
from event_intel.runtime import preflight as _preflight
from event_intel.sources import retrieval as _retrieval
from event_intel.storage import artifact_registry as _artifact_registry


def _resolve_output_path(
    workspace_id: str, out_path: str | None, config: dict | None = None
) -> Path:
    if out_path:
        return Path(out_path).expanduser()
    # Bug-(a) fix: previously this returned a cwd-relative ``outputs/<ws>/...`` —
    # the MCP server runs with an arbitrary cwd (Program Files) → unwritable, and
    # it disagreed with where build_event reads cards from. Route through the
    # central resolver so draft writes exactly where build looks.
    return (
        _paths.resolve_paths(config).workspace_dir(workspace_id)
        / "capability_cards.draft.yaml"
    )


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

    `source_kind`:
      - "text"          — draft from inline `source_content`.
      - "file"/"files"  — draft from `source_paths` (md/txt/pdf).
      - "workspace"     — retrieve from the workspace source library
                          (product_sources_{ws}, populated by `sources sync`)
                          and draft from that grounded context (WSL W3).

    Returns:
        Success envelope with `draft_path` + `warnings` + `usage`.
        Failure envelope from `envelope_from_exception`.

    """
    try:
        _preflight._validate_workspace_id_minimal(workspace_id)
        config = _preflight.load_config()
        chosen_model = model or config["llm"]["draft_cards_model"]
        max_tokens = int(config["llm"].get("draft_cards_max_tokens", 4096))

        llm_provider = _llm.make_llm_provider(config, model=chosen_model)
        ping = llm_provider.ping()
        if ping.get("status") != "ok":
            raise MCPError(
                error_code=ErrorCode.CONFIG_ERROR,
                stage=Stage.INGEST,
                message=ping.get("message", "LLM provider not configured"),
                hint={"fix": ping.get("fix", "Check LLM provider configuration")},
                retryable=False,
            )

        # Workspace drafting: pull grounded context from product_sources_{ws} and
        # feed it to the drafter as plain text. The text/file/files paths are
        # unchanged. Needs bge-m3 + Chroma → run the lightweight preflight first.
        drafter_kind, drafter_content, drafter_paths = (
            source_kind,
            source_content,
            source_paths,
        )
        source_retrieval = None
        if source_kind == "workspace":
            _preflight.run_preflight(
                workspace_id, require_product_context=False, config=config
            )
            drafter_content, source_retrieval = _retrieval.gather_workspace_source_text(
                workspace_id=workspace_id,
                embedding_provider=_embedding.BgeM3Provider(),
                vectorstore_provider=_vectorstore.ChromaProvider(config=config),
                lang=lang,
            )
            drafter_kind, drafter_paths = "text", None

        result = _drafter.draft_cards(
            source_kind=drafter_kind,
            source_content=drafter_content,
            source_paths=drafter_paths,
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

        draft_path = _resolve_output_path(workspace_id, out_path, config)
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(result.yaml_text, encoding="utf-8")

        # Y2.1d: register the draft as an artifact for path-free remote download.
        # Additive + best-effort — never fail the draft on a registry error.
        try:
            draft_artifact_id = _artifact_registry.put_artifact(
                workspace_id=workspace_id, content=result.yaml_text, suffix=".yaml"
            )["artifact_id"]
        except Exception:  # noqa: BLE001
            draft_artifact_id = None

        response = {
            "ok": True,
            "draft_path": str(draft_path),
            "draft_artifact_id": draft_artifact_id,
            "warnings": result.warnings,
            "model": result.model,
            "usage": result.usage,
            "lang": lang,
        }
        if source_retrieval is not None:
            response["source_retrieval"] = source_retrieval
        return response
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.INGEST)
