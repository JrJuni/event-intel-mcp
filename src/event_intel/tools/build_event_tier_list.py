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

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from event_intel.cards import validator as _validator
from event_intel.errors import ErrorCode, MCPError, Stage, envelope_from_exception
from event_intel.events import enrichment as _enrichment
from event_intel.events import extraction as _extraction
from event_intel.events import run_summary as _run_summary
from event_intel.events import source_capture as _source_capture
from event_intel.providers import embedding as _embedding
from event_intel.providers import llm as _llm
from event_intel.providers import search as _search
from event_intel.providers import vectorstore as _vectorstore
from event_intel.rag import retriever as _retriever
from event_intel.report import tier_list_md as _tier_list_md
from event_intel.report import tier_list_yaml as _tier_list_yaml
from event_intel.runtime import paths as _paths
from event_intel.runtime import preflight as _preflight
from event_intel.scoring import compute as _scoring
from event_intel.sources import indexer as _src_indexer
from event_intel.sources import retrieval as _src_retrieval
from event_intel.storage.identifiers import sanitize_slug

if TYPE_CHECKING:
    from event_intel.cards.schema import CapabilityCards


_DEFAULT_TOP_K = 5


def _outputs_base() -> Path:
    """Base dir (workspace root) for tier-list outputs — cwd-INDEPENDENT.

    The MCP server is spawned by Claude Desktop with an arbitrary cwd (e.g.
    Program Files) where a relative ``outputs`` is unwritable → PermissionError
    (WinError 5). Delegated to ``runtime.paths`` so draft / build / migrate all
    agree on one root: EVENT_INTEL_OUTPUT_DIR (legacy) / EVENT_INTEL_WORKSPACE_DIR
    win, else ~/EventIntel with a back-compat fallback to <repo>/outputs.
    """
    return _paths.resolve_paths().workspace_root


def _resolve_output_dir(workspace_id: str, event_slug: str) -> Path:
    date_tag = datetime.now(UTC).strftime("%Y%m%d")
    return _outputs_base() / workspace_id / f"{event_slug}_{date_tag}"


_VALID_TARGET_MODES = ("customer", "partner", "ecosystem")


def _load_cards_or_warn(workspace_id: str) -> tuple[CapabilityCards | None, str | None]:
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
        f"no capability_cards file in outputs/{workspace_id}: category_fit and "
        "buying-trigger signals are DISABLED and target_mode defaults to customer "
        "— RANKINGS differ from a carded run, not just the rationale text"
    )


def _resolve_target_mode(
    arg: str | None, config: dict, cards: CapabilityCards | None
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
    refresh: bool = False,
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

        # 2b. Load + VALIDATE cards EARLY (review #6) — a present-but-invalid card
        #     file should fail before spending LLM extraction + Brave/embedding
        #     calls, not after. Also resolve target_mode here.
        cards, cards_warning = _load_cards_or_warn(ws)
        resolved_target_mode = _resolve_target_mode(target_mode, config, cards)

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
            # Resume is scoped per EVENT, not per workspace (review #4): the
            # default workspace-global resume let a later event silently reuse an
            # earlier event's rows (keyed by company name). The per-query search
            # cache still provides cross-event cost savings.
            if resume_from:
                resume_path = Path(resume_from).expanduser()
            else:
                resume_path = Path.home() / ".event-intel" / "resume" / ws / f"{slug}.jsonl"
            enrich_result = _enrichment.enrich_exhibitors(
                candidates=extraction.candidates,
                workspace_id=ws,
                lang=lang,
                config=config,
                search_provider=search_provider,
                resume_path=resume_path,
                max_companies=max_companies,
                refresh=refresh,
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
                vectorstore_provider=_vectorstore.ChromaProvider(config=config),
                top_k=int(_retrieval_cfg.get("top_k", _DEFAULT_TOP_K)),
                capability_top_k=int(_retrieval_cfg.get("capability_top_k", _DEFAULT_TOP_K)),
                capability_aggregate_top_n=int(_retrieval_cfg.get("capability_aggregate_top_n", 3)),
            )
        else:
            fit_results = []

        # 7. Scoring + tier decision + optional rationale (S4). cards +
        #    resolved_target_mode were loaded/validated early (step 2b).
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

        # 7b. Source-library grounding for S/A rows (WSL W4). RATIONALE-ONLY: it
        #     is computed AFTER scoring, reads the SEPARATE product_sources_{ws}
        #     collection, and is written only to the report's source_provenance
        #     field — there is no path from it back to any score/tier. Best-effort:
        #     an empty/missing library or any error → {} (card-based rationale).
        source_provenance: dict[str, list[dict]] = {}
        try:
            sa_items = [
                (
                    s.row.name,
                    f"{s.row.name}. {s.row.description or ''} "
                    f"{s.row.source_snippet or ''}".strip(),
                )
                for s in summary.rows
                if s.tier in ("S", "A")
            ]
            if sa_items:
                _src_vs = _vectorstore.ChromaProvider(config=config)
                _src_collection = _src_indexer.source_collection_name(ws)
                if _src_vs.collection_info(_src_collection).get("count", 0) > 0:
                    source_provenance = _src_retrieval.gather_exhibitor_provenance(
                        items=sa_items,
                        workspace_id=ws,
                        embedding_provider=_embedding.BgeM3Provider(),
                        vectorstore_provider=_src_vs,
                    )
        except Exception:  # noqa: BLE001 — grounding is auxiliary, never fail a build
            source_provenance = {}

        # 8. Reports — md + yaml (S5).
        context = _tier_list_md.ReportContext(
            workspace_id=ws, event_name=event_name, event_slug=slug,
            lang=lang, generated_at=datetime.now(UTC),
            target_mode=resolved_target_mode,
            tier_rules=config.get("scoring", {}).get("tier_rules"),
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
            source_provenance=source_provenance,
        )
        yaml_payload = _tier_list_yaml.build_tier_list_payload(
            summary=summary, needs_review=needs_review_rows, context=context,
            source_provenance=source_provenance,
        )
        yaml_text = _tier_list_yaml.dump_tier_list_yaml(yaml_payload)

        # 9. Write artifacts.
        out_dir = _resolve_output_dir(ws, slug)
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / "tier_list.md"
        yaml_path = out_dir / "tier_list.yaml"
        md_path.write_text(md_text, encoding="utf-8")
        yaml_path.write_text(yaml_text, encoding="utf-8")

        # 9b. Emit run-summary (CS1) — audit/reproducibility record. Auxiliary:
        #     an emitter failure must never fail an otherwise-successful build.
        run_summary_path: Path | None = None
        try:
            from dataclasses import asdict as _asdict

            ref_ts = context.generated_at.isoformat()
            caps = {
                "max_companies": max_companies,
                "max_chunks_per_event": int(
                    config.get("extraction", {}).get("max_chunks_per_event", 12)
                ),
            }
            model_ids = {
                "extract": llm_model_extract,
                "rationale": config["llm"].get("rationale_model", llm_model_extract),
                "embedding": "bge-m3",
            }
            # Prefer the CS7 ingest receipt's content_fingerprint (the Chroma
            # collection content actually scored against); fall back to the card
            # file's byte hash when no receipt exists yet (CS1 stand-in).
            from event_intel.cards import ingest as _ingest

            _receipt = _ingest.read_ingest_receipt(
                _outputs_base() / ws / _ingest.RECEIPT_FILENAME
            )
            cards_fp = (
                (_receipt or {}).get("content_fingerprint")
                or _run_summary.sha256_file(_outputs_base() / ws / "capability_cards.yaml")
            )
            # WSL W4: record which product_sources state was available (if any).
            _src_manifest = _src_indexer.read_manifest(
                _paths.resolve_paths(config).source_index_manifest(ws)
            )
            source_index_fp = (_src_manifest or {}).get("content_fingerprint")
            source_sha = _run_summary.sha256_text(capture.text or "")
            cfg_fp = _run_summary.config_hash(config)
            run_fp = _run_summary.compute_run_fingerprint(
                git_sha=_run_summary.git_commit_sha(),
                cards_fingerprint=cards_fp,
                config_fp=cfg_fp,
                source_sha256=source_sha,
                caps=caps,
                target_mode=resolved_target_mode,
                model_ids=model_ids,
            )
            rs = _run_summary.RunSummary(
                run_id=_run_summary.new_run_id(slug=slug, now_iso=ref_ts),
                run_fingerprint=run_fp,
                git_commit_sha=_run_summary.git_commit_sha(),
                config_fp=cfg_fp,
                cards_fingerprint=cards_fp,
                source_sha256=source_sha,
                provider=config.get("llm", {}).get("provider", "anthropic"),
                model_ids=model_ids,
                reference_timestamp=ref_ts,
                target_mode=resolved_target_mode,
                max_companies=max_companies,
                max_chunks_per_event=caps["max_chunks_per_event"],
                refresh=refresh,
                cache_hits=enrich_result.cache_hits,
                cache_misses=enrich_result.cache_misses,
                skipped_from_resume=enrich_result.skipped_from_resume,
                search_calls=enrich_result.cache_hits + enrich_result.cache_misses,
                extracted=len(extraction.candidates),
                enriched=len(enriched_rows),
                scored=len(summary.rows),
                extraction_coverage=None,  # CS2 fills (roster join)
                stages=[
                    _run_summary.StageStatus(s, True)
                    for s in ("source_capture", "extraction", "enrichment",
                              "retrieval", "scoring", "report")
                ],
                companies=[
                    _run_summary.CompanyScore(
                        name=r.name, tier=r.tier, final_score=r.final_score,
                        evidence_floor=r.evidence_floor,
                        dimensions=_asdict(r.dimensions),
                        tier_reasons=list(r.tier_reasons),
                    )
                    for r in summary.rows
                ],
                warnings=list(extraction.warnings) + list(enrich_result.warnings),
                source_index_fingerprint=source_index_fp,
            )
            run_summary_path = _run_summary.write_run_summary(rs, out_dir, allow_overwrite=True)
        except Exception:  # noqa: BLE001 — auxiliary, never fail the build
            run_summary_path = None

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
            "run_summary_path": str(run_summary_path) if run_summary_path else None,
        }
    except Exception as exc:
        # Stage hint best-effort — if exc is MCPError it carries its own.
        return envelope_from_exception(exc, stage=Stage.PREFLIGHT)
