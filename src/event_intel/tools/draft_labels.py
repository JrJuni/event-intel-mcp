"""draft_labels MCP tool handler — Y1 L6 (product surface).

Stage A+B of the multi-vendor labeling system, exposed so the HOST (Claude
Desktop / Claude Code) can run it in-app: the MCP server produces the GPT-OAuth
single-vendor draft (silver) + flags the gate-class / low-confidence rows, and
returns the flagged set. The host then does Stage C — web-search refine of the
flagged rows with ITS OWN search (the MCP server has no web search) — and seals.
This keeps the server simple and uses a vendor (Claude host) independent of the
GPT drafter, which is what makes cross/refine gold (plan v3 silver/gold).

Module-reference imports; cold-start safe at module top (heavy LLM loads lazily
inside the configured provider; yaml imported in-body).
"""
from __future__ import annotations

import json
from pathlib import Path

from event_intel.errors import ErrorCode, MCPError, Stage, envelope_from_exception
from event_intel.eval import label_draft as _label_draft
from event_intel.eval import labeling as _labeling
from event_intel.providers import llm as _llm
from event_intel.runtime import paths as _paths
from event_intel.runtime import preflight as _preflight
from event_intel.storage.identifiers import sanitize_slug


def _outputs_base() -> Path:
    """Workspace root — delegated to the central resolver (see runtime.paths)."""
    return _paths.resolve_paths().workspace_root


def draft_labels(
    *,
    workspace_id: str = "default",
    sheet_path: str = "",
    card_path: str | None = None,
    out_path: str | None = None,
    lang: str = "ko",
    batch_size: int = 30,
    min_confidence: float = 0.7,
) -> dict:
    """Draft (GPT-OAuth, silver) + flag a labeling sheet for host-side refine.

    Returns the drafted+flagged sheet path plus the companies the host should
    web-search-refine (the `needs_review` set). The host applies its refinements
    (gold) and seals separately — this tool never searches or seals.
    """
    try:
        ws = sanitize_slug(workspace_id, field_name="workspace_id")
        if not sheet_path:
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT,
                stage=Stage.PREFLIGHT,
                message="sheet_path is required (a labeling sheet from labeling-sheet)",
                hint={"field": "sheet_path"},
            )
        sheet_file = Path(sheet_path).expanduser()
        if not sheet_file.is_file():
            raise MCPError(
                error_code=ErrorCode.IO_ERROR,
                stage=Stage.PREFLIGHT,
                message=f"labeling sheet not found: {sheet_path}",
                hint={"field": "sheet_path"},
            )

        # card: explicit path or the workspace's capability_cards.yaml
        card_file = (
            Path(card_path).expanduser()
            if card_path
            else _outputs_base() / ws / "capability_cards.yaml"
        )
        if not card_file.is_file():
            raise MCPError(
                error_code=ErrorCode.PRODUCT_CONTEXT_MISSING,
                stage=Stage.PREFLIGHT,
                message=f"capability card not found for the rubric: {card_file}",
                hint={"field": "card_path", "workspace_id": ws},
            )

        import yaml as _yaml

        rows = json.loads(sheet_file.read_text(encoding="utf-8"))
        card = _yaml.safe_load(card_file.read_text(encoding="utf-8"))
        header = _labeling.product_header_from_card(card, lang=lang)

        # GPT-OAuth (the configured provider) is the silver drafter — a different
        # vendor than the Claude host that will refine.
        provider = _llm.make_llm_provider(_preflight.load_config())
        drafted = _label_draft.draft_labels(
            sheet_rows=rows, product_header=header, llm_provider=provider,
            batch_size=batch_size, lang=lang,
        )
        flagged = _labeling.flag_for_review(drafted, min_confidence=min_confidence)

        out = Path(out_path).expanduser() if out_path else sheet_file.with_name(
            sheet_file.stem + ".drafted.json"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(flagged, ensure_ascii=False, indent=2), encoding="utf-8")

        needs_review = [r["name"] for r in flagged if r.get("needs_review")]
        silver_auto = sum(1 for r in flagged if r.get("grade") == "silver")
        return {
            "ok": True,
            "workspace_id": ws,
            "drafted_sheet_path": str(out),
            "n": len(flagged),
            "silver_auto": silver_auto,
            "needs_review": len(needs_review),
            "needs_review_companies": needs_review,
            "next": (
                "Host: web-search-refine the needs_review_companies (Stage C, gold), "
                "then `benchmark apply-refinements` + `benchmark seal-labels`."
            ),
        }
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.PREFLIGHT)
