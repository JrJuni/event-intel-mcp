"""build_event_tier_list MCP tool handler — the headline 5th tool.

Pipeline (plan v0.5 §S3+S4+S5 wired together):

    sanitize slugs
      → preflight (require_product_context=True → PRODUCT_CONTEXT_MISSING)
      → source_capture (S3)
      → extract_exhibitors (S3, LLM)
      → enrich_exhibitors (S4, Brave web+news, cache + resume)
      → retrieve_fit_event_to_product (S4, single-direction RAG)
      → score_exhibitors (S4, weighted sum + tier + optional Sonnet rationale)
      → render_tier_list_md + dump_tier_list_yaml (S5)
      → write to outputs/{workspace_id}/{event_slug}_{date}/

All heavy deps stay lazy (providers wrap them). Module-reference imports for
every monkeypatchable seam: source_capture / extraction / enrichment /
retriever / scoring.compute / report.* / providers.* / preflight.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from event_intel.cards import validator as _validator
from event_intel.errors import ErrorCode, MCPError, Stage, envelope_from_exception
from event_intel.events import enrichment as _enrichment
from event_intel.events import extraction as _extraction
from event_intel.events import source_capture as _source_capture
from event_intel.providers import embedding as _embedding
from event_intel.providers import llm as _llm
from event_intel.providers import search as _search
from event_intel.providers import vectorstore as _vectorstore
from event_intel.rag import retriever as _retriever
from event_intel.report import tier_list_md as _tier_list_md
from event_intel.report import tier_list_yaml as _tier_list_yaml
from event_intel.runtime import preflight as _preflight
from event_intel.scoring import compute as _scoring
from event_intel.storage.identifiers import sanitize_slug

if TYPE_CHECKING:
    from event_intel.cards.schema import CapabilityCards


_DEFAULT_TOP_K = 5


def _outputs_base() -> Path:
    """Base dir for tier-list outputs — cwd-INDEPENDENT.

    The MCP server is spawned by Claude Desktop with an arbitrary cwd (e.g.
    Program Files) where a relative ``outputs`` is unwritable → PermissionError
    (WinError 5). Derive ``<repo>/outputs`` from the package location instead
    (same cwd-independence fix as event_intel._env for .env). EVENT_INTEL_OUTPUT_DIR
    overrides for users who want outputs elsewhere.
    """
    base_env = os.environ.get("EVENT_INTEL_OUTPUT_DIR")
    if base_env:
        return Path(base_env).expanduser()
    # <repo>/src/event_intel/tools/build_event_tier_list.py → parents[3] == <repo>
    return Path(__file__).resolve().parents[3] / "outputs"


def _resolve_output_dir(workspace_id: str, event_slug: str) -> Path:
    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _outputs_base() / workspace_id / f"{event_slug}_{date_tag}"


_VALID_TARGET_MODES = ("customer", "partner", "ecosystem")


def _load_cards_or_warn(workspace_id: str) -> tuple["CapabilityCards | None", str | None]:
    """Card-load contract (review round-2 #3):
      - a candidate file EXISTS but fails validation → raise (explicit error).
      - NO candidate file exists → (None, warning). This is a legitimate state:
        the RAG collection can be ingested (preflight passed) while the local
        card file is absent, so we must not hard-fail — scoring proceeds with
        target_mode=customer + generic rationale.
    """
    base = _outputs_base()
    candidates = [
        base / workspace_id / "capability_cards.yaml",
        base / workspace_id / "capability_cards.draft.yaml",
    ]
    for path in candidates:
        if path.is_file():
            # Present → must validate. load_and_validate raises MCPError on bad
            # YAML / schema; we let it propagate (no silent except: continue).
            return _validator.load_and_validate(path), None
    return None, (
        f"no capability_cards file in outputs/{workspace_id}; scoring proceeds "
        "with target_mode=customer default and generic rationale"
    )


def _resolve_target_mode(
    arg: str | None, config: dict, cards: "CapabilityCards | None"
) -> str:
    """Precedence: build_event arg > user config > card default > 'customer'."""
    cfg_mode = config.get("target_mode")
    card_mode = getattr(cards, "target_mode", None) if cards is not None else None
    for candidate in (arg, cfg_mode, card_mode, "customer"):
        if candidate:
            if candidate not in _VALID_TARGET_MODES:
                raise MCPError(
                    error_code=ErrorCode.INVALID_INPUT,
                    stage=Stage.PREFLIGHT,
                    message=f"invalid target_mode {candidate!r}",
                    hint={"field": "target_mode", "allowed": list(_VALID_TARGET_MODES)},
                )
            return candidate
    return "customer"


def build_event_tier_list(
    *,
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
    rationale_for_tiers: tuple[str, ...] = ("S", "A"),
    target_mode: str | None = None,
) -> dict:
    """Build a tiered exhibitor list from an event source. See module docstring."""
    try:
        # 1. Input validation — slug sanitize is the path-traversal gate.
        ws = sanitize_slug(workspace_id, field_name="workspace_id")
        slug = sanitize_slug(event_slug, field_name="event_slug")
        if not event_name or not event_name.strip():
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT,
                stage=Stage.PREFLIGHT,
                message="event_name is required",
                hint={"field": "event_name"},
            )
        if not source_ref:
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT,
                stage=Stage.PREFLIGHT,
                message="source_ref is required",
                hint={"field": "source_ref", "supported_kinds": list(_source_capture.SUPPORTED_SOURCE_KINDS)},
            )

        # 2. Preflight — must include product_context check (R3-#1).
        config = _preflight.load_config()
        _preflight.run_preflight(ws, require_product_context=True, config=config)

        # 3. Source capture (S3, raises SOURCE_CAPTURE_FAILED on failure).
        capture = _source_capture.capture_source(
            source_kind=source_kind, source_ref=source_ref
        )

        # 4. Extraction (S3, raises UPSTREAM_ERROR on LLM failure).
        llm_model_extract = config["llm"]["extract_exhibitors_model"]
        extract_llm = _llm.make_llm_provider(config, model=llm_model_extract)
        extraction = _extraction.extract_exhibitors(
            capture=capture, lang=lang, llm_provider=extract_llm, config=config,
        )

        # 5. Enrichment (S4) — optional.
        if enrichment_enabled and extraction.candidates:
            search_provider = _search.BraveSearchProvider()
            enrich_result = _enrichment.enrich_exhibitors(
                candidates=extraction.candidates,
                workspace_id=ws,
                lang=lang,
                config=config,
                search_provider=search_provider,
                resume_path=Path(resume_from).expanduser() if resume_from else None,
                max_companies=max_companies,
            )
            enriched_rows = enrich_result.rows
        else:
            # Skip enrichment — synthesize rows with snippet-only evidence.
            enriched_rows = [
                _enrichment.EnrichedExhibitor(
                    name=c.name,
                    source_snippet=c.source_snippet,
                    url=c.url,
                    official_url=c.url,
                    description=c.description,
                    extraction_confidence=c.extraction_confidence,
                )
                for c in extraction.candidates
            ]
            enrich_result = _enrichment.EnrichmentResult(
                rows=enriched_rows, cache_hits=0, cache_misses=0,
                skipped_from_resume=0, warnings=["enrichment disabled by caller"],
            )

        # 6. Fit retrieval (S4, single-direction).
        if enriched_rows:
            _retrieval_cfg = config.get("scoring", {}).get("retrieval", {})
            fit_results = _retriever.retrieve_fit_event_to_product(
                exhibitors=enriched_rows,
                workspace_id=ws,
                embedding_provider=_embedding.BgeM3Provider(),
                vectorstore_provider=_vectorstore.ChromaProvider(),
                top_k=int(_retrieval_cfg.get("top_k", _DEFAULT_TOP_K)),
                capability_top_k=int(_retrieval_cfg.get("capability_top_k", _DEFAULT_TOP_K)),
            )
        else:
            fit_results = []

        # 7. Scoring + tier decision + optional rationale (S4).
        cards, cards_warning = _load_cards_or_warn(ws)
        resolved_target_mode = _resolve_target_mode(target_mode, config, cards)
        rationale_llm = None
        if run_rationale and enriched_rows:
            rationale_model = config["llm"].get("rationale_model", llm_model_extract)
            rationale_llm = _llm.make_llm_provider(config, model=rationale_model)
        summary = _scoring.score_exhibitors(
            enriched=enriched_rows,
            fit_results=fit_results,
            cards=cards,
            config=config,
            top_k=_DEFAULT_TOP_K,
            llm_provider=rationale_llm,
            rationale_lang=lang,
            rationale_for_tiers=rationale_for_tiers,
            rationale_max_tokens=int(config["llm"].get("rationale_max_tokens", 256)),
            target_mode=resolved_target_mode,
        )

        # 8. Reports — md + yaml (S5).
        context = _tier_list_md.ReportContext(
            workspace_id=ws, event_name=event_name, event_slug=slug,
            lang=lang, generated_at=datetime.now(timezone.utc),
        )
        needs_review_rows = [
            _enrichment.EnrichedExhibitor(
                name=c.name,
                source_snippet=c.source_snippet,
                url=c.url,
                description=c.description,
                extraction_confidence=c.extraction_confidence,
                enrichment_status="needs_review",
                enrichment_warnings=[
                    f"extraction_confidence {c.extraction_confidence:.2f} below threshold"
                ],
            )
            for c in extraction.needs_review
        ]
        md_text = _tier_list_md.render_tier_list_md(
            summary=summary, needs_review=needs_review_rows, context=context,
        )
        yaml_payload = _tier_list_yaml.build_tier_list_payload(
            summary=summary, needs_review=needs_review_rows, context=context,
        )
        yaml_text = _tier_list_yaml.dump_tier_list_yaml(yaml_payload)

        # 9. Write artifacts.
        out_dir = _resolve_output_dir(ws, slug)
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / "tier_list.md"
        yaml_path = out_dir / "tier_list.yaml"
        md_path.write_text(md_text, encoding="utf-8")
        yaml_path.write_text(yaml_text, encoding="utf-8")

        return {
            "ok": True,
            "workspace_id": ws,
            "event_slug": slug,
            "event_name": event_name,
            "lang": lang,
            "target_mode": resolved_target_mode,
            "tier_counts": yaml_payload["tier_counts"],
            "rationale_calls": summary.rationale_calls,
            "candidates_extracted": len(extraction.candidates),
            "candidates_needs_review": len(extraction.needs_review),
            "candidates_dropped_low_snippet": extraction.dropped_low_snippet,
            "chunks_processed": extraction.chunks_processed,
            "chunks_total": extraction.chunks_total,
            "cache_hits": enrich_result.cache_hits,
            "cache_misses": enrich_result.cache_misses,
            "skipped_from_resume": enrich_result.skipped_from_resume,
            "warnings": (
                list(extraction.warnings)
                + list(enrich_result.warnings)
                + ([cards_warning] if cards_warning else [])
            ),
            "tier_list_md_path": str(md_path),
            "tier_list_yaml_path": str(yaml_path),
        }
    except Exception as exc:
        # Stage hint best-effort — if exc is MCPError it carries its own.
        return envelope_from_exception(exc, stage=Stage.PREFLIGHT)
