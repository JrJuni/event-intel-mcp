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
from event_intel.events import news_body as _news_body
from event_intel.events import run_summary as _run_summary
from event_intel.events import source_capture as _source_capture
from event_intel.events import triage as _triage
from event_intel.providers import embedding as _embedding
from event_intel.providers import llm as _llm
from event_intel.providers import search as _search
from event_intel.providers import vectorstore as _vectorstore
from event_intel.rag import retriever as _retriever
from event_intel.report import tier_list_md as _tier_list_md
from event_intel.report import tier_list_yaml as _tier_list_yaml
from event_intel.runtime import io_contract as _io
from event_intel.runtime import llm_ledger as _llm_ledger
from event_intel.runtime import paths as _paths
from event_intel.runtime import preflight as _preflight
from event_intel.scoring import compute as _scoring
from event_intel.scoring import llm_fit as _llm_fit
from event_intel.sources import indexer as _src_indexer
from event_intel.sources import retrieval as _src_retrieval
from event_intel.storage import artifact_registry as _artifact_registry
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
    source_content: str | None = None,
    source_artifact_id: str | None = None,
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
        # Y2.1b-2: source as source_ref (server-local path, OR inline text for the
        # html_text/text kinds) | source_content | source_artifact_id — exactly one.
        # content/artifact carry the bytes for a FILE kind (html_file/csv_file/
        # text_file); inline kinds (html_text/text) pass their text via source_ref.
        _file_kinds = ("html_file", "csv_file", "text_file")
        _src_n = sum(bool(x) for x in (source_ref, source_content, source_artifact_id))
        if _src_n == 0:
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT, stage=Stage.PREFLIGHT,
                message="one of source_ref / source_content / source_artifact_id is required",
                hint={"supported_kinds": list(_source_capture.SUPPORTED_SOURCE_KINDS)},
            )
        if _src_n > 1:
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT, stage=Stage.PREFLIGHT,
                message="provide exactly one of source_ref / source_content / source_artifact_id",
                hint={"rule": "mutually exclusive"},
            )
        if (source_content or source_artifact_id) and source_kind not in _file_kinds:
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT, stage=Stage.PREFLIGHT,
                message=(
                    f"source_content/source_artifact_id apply to file kinds {_file_kinds}; "
                    "for inline kinds (html_text/text) pass the content via source_ref"
                ),
                hint={"source_kind": source_kind, "file_kinds": list(_file_kinds)},
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
        if source_content or source_artifact_id:
            _suffix = {"html_file": ".html", "csv_file": ".csv", "text_file": ".txt"}[source_kind]
            with _io.materialize_input(
                workspace_id=ws, field="source", content=source_content,
                artifact_id=source_artifact_id, path=None, suffix=_suffix,
                config=config, stage=Stage.PREFLIGHT,
            ) as _src_file:
                # capture_source reads the file fully into memory, so the temp can
                # be cleaned (on with-exit) right after.
                capture = _source_capture.capture_source(
                    source_kind=source_kind, source_ref=str(_src_file)
                )
        else:
            capture = _source_capture.capture_source(
                source_kind=source_kind, source_ref=source_ref
            )

        # 4. Extraction (S3, raises UPSTREAM_ERROR on LLM failure).
        # Y1D D0: one usage ledger per build — every LLM stage records into it.
        usage_ledger = _llm_ledger.LlmUsageLedger()
        llm_model_extract = config["llm"]["extract_exhibitors_model"]
        extract_llm = _llm.make_llm_provider(config, model=llm_model_extract)
        extraction = _extraction.extract_exhibitors(
            capture=capture, lang=lang, llm_provider=extract_llm, config=config,
        )
        # extraction pre-aggregates its per-chunk usage; fold it in as one entry.
        # Record the provider's ACTUAL model, not the config request: for
        # chatgpt_oauth the factory ignores the model param entirely, so
        # llm_model_extract would mislabel a free-OAuth run as a paid one.
        usage_ledger.record(
            "extraction",
            getattr(extract_llm, "model", llm_model_extract) or llm_model_extract,
            extraction.usage,
            calls=extraction.chunks_processed,
        )

        # 4.4. Per-stage model right-sizing (#16-④): triage and llm_fit are
        #      bounded classification judgments — config may pin a cheaper
        #      model per stage (llm.triage_model / llm.fit_model). Absent keys
        #      → the extraction provider, identical to pre-rightsizing
        #      behaviour. chatgpt_oauth pins llm.chatgpt_oauth_model and the
        #      factory ignores the model param, so right-sizing is a no-op
        #      there — surfaced as one warning instead of silently.
        stage_model_warnings: list[str] = []
        _llm_cfg = config.get("llm", {}) or {}
        _is_oauth = _llm_cfg.get("provider", "anthropic") == "chatgpt_oauth"
        _oauth_noop_keys = [
            k for k in ("triage_model", "fit_model") if _is_oauth and _llm_cfg.get(k)
        ]
        if _oauth_noop_keys:
            stage_model_warnings.append(
                f"llm.{' / llm.'.join(_oauth_noop_keys)} have no effect under "
                "provider=chatgpt_oauth (the OAuth lane pins "
                "llm.chatgpt_oauth_model) — per-stage right-sizing skipped"
            )

        def _stage_llm(config_key: str) -> object:
            stage_model = _llm_cfg.get(config_key)
            if not stage_model or _is_oauth:
                return extract_llm
            if stage_model == getattr(extract_llm, "model", None):
                return extract_llm
            return _llm.make_llm_provider(config, model=stage_model)

        triage_llm = _stage_llm("triage_model")
        fit_llm = _stage_llm("fit_model")

        # 4.5. Roster triage (Y1D D2) — when the roster exceeds the enrichment
        #      cap, pick WHICH companies get the slots by LLM product-domain
        #      relevance instead of "the first N in page order". Enrichment's
        #      own cap stays as a no-op backstop. Roster ≤ cap → zero calls;
        #      total LLM failure → first-N (old behaviour); never fails a build.
        triage_warnings: list[str] = []
        candidates_for_enrich = extraction.candidates
        _triage_cfg = (config.get("enrichment", {}) or {}).get("triage", {}) or {}
        if (
            enrichment_enabled
            and extraction.candidates
            and bool(_triage_cfg.get("enabled", False))
        ):
            _enrich_cap = max_companies or int(
                (config.get("enrichment", {}) or {}).get("max_companies", 30)
            )
            triage_result = _triage.triage_roster(
                extraction.candidates,
                _triage.build_capability_digest(cards),
                triage_llm,
                max_companies=_enrich_cap,
                batch_size=int(_triage_cfg.get("batch_size", 120)),
                lang=lang,
                ledger=usage_ledger,
            )
            candidates_for_enrich = triage_result.selected
            triage_warnings = triage_result.warnings

        # 5. Enrichment (S4) — optional.
        if enrichment_enabled and extraction.candidates:
            search_provider = _search.make_search_provider(config)
            # Resume is scoped per EVENT, not per workspace (review #4): the
            # default workspace-global resume let a later event silently reuse an
            # earlier event's rows (keyed by company name). The per-query search
            # cache still provides cross-event cost savings.
            if resume_from:
                resume_path = Path(resume_from).expanduser()
            else:
                resume_path = Path.home() / ".event-intel" / "resume" / ws / f"{slug}.jsonl"
            enrich_result = _enrichment.enrich_exhibitors(
                candidates=candidates_for_enrich,
                workspace_id=ws,
                lang=lang,
                config=config,
                search_provider=search_provider,
                resume_path=resume_path,
                max_companies=max_companies,
                refresh=refresh,
                # N4 query-rescue: reuse the extraction LLM (proposes alternate
                # queries for blocked-and-empty companies; never fetches).
                llm_provider=extract_llm,
                usage_ledger=usage_ledger,
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

        # 6b. LLM capability fit (Y1D D1) — the DEFAULT mode replaces the
        #     dead-flat cosine value per company (target ≈ bad_fit ≈ 0.5
        #     measured). Any per-company failure keeps that company's cosine
        #     value; this stage never fails a build. Escape hatch:
        #     scoring.capability_fit_mode: cosine.
        llm_fit_warnings: list[str] = []
        fit_mode = str(
            config.get("scoring", {}).get("capability_fit_mode", "llm")
        ).strip().lower()
        if fit_mode not in ("llm", "cosine"):
            llm_fit_warnings.append(
                f"unknown scoring.capability_fit_mode {fit_mode!r} — using 'llm'"
            )
            fit_mode = "llm"
        if fit_mode == "llm" and fit_results:
            llm_fit_warnings += _llm_fit.apply_llm_capability_fit(
                rows=enriched_rows, fit_results=fit_results,
                llm_provider=fit_llm, lang=lang, ledger=usage_ledger,
            )

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
            usage_ledger=usage_ledger,
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

        # 7c. News-body ↔ product relatedness (news plan B2, criterion ③).
        #     REPORT-ONLY like 7b: computed AFTER scoring against the product
        #     card collection; written only to the report's news_relatedness
        #     field — no path back to any score/tier (staged: tier folding
        #     needs separate approval). Best-effort: any error → {}.
        news_relatedness: dict[str, list[dict]] = {}
        try:
            nb_cfg = (config.get("enrichment", {}) or {}).get("news_body", {}) or {}
            scored_rows = [s.row for s in summary.rows]
            has_bodies = any(
                getattr(n, "body_sha", None)
                for r in scored_rows
                for n in getattr(r, "news_signals", []) or []
            )
            if bool(nb_cfg.get("enabled", False)) and has_bodies:
                from event_intel.cards.ingest import product_collection_name

                _nb_loader = _news_body.NewsBodyFetcher(
                    cfg=_news_body.NewsBodyConfig.from_dict(nb_cfg),
                    cache_dir=Path.home() / ".event-intel" / "cache" / "news_body",
                    now=datetime.now(UTC),
                )
                news_relatedness = _news_body.gather_news_relatedness(
                    rows=scored_rows,
                    body_loader=_nb_loader.load_body,
                    collection=product_collection_name(ws),
                    embedding_provider=_embedding.BgeM3Provider(),
                    vectorstore_provider=_vectorstore.ChromaProvider(config=config),
                )
        except Exception:  # noqa: BLE001 — diagnostics only, never fail a build
            news_relatedness = {}

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
            source_provenance=source_provenance, news_relatedness=news_relatedness,
        )
        yaml_payload = _tier_list_yaml.build_tier_list_payload(
            summary=summary, needs_review=needs_review_rows, context=context,
            source_provenance=source_provenance, news_relatedness=news_relatedness,
        )
        yaml_text = _tier_list_yaml.dump_tier_list_yaml(yaml_payload)

        # 9. Write artifacts.
        out_dir = _resolve_output_dir(ws, slug)
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / "tier_list.md"
        yaml_path = out_dir / "tier_list.yaml"
        md_path.write_text(md_text, encoding="utf-8")
        yaml_path.write_text(yaml_text, encoding="utf-8")

        # Y2.1d: also register the reports as artifacts so a remote client can
        # download them by id (path-free). Additive + best-effort — a registry
        # failure must never fail an otherwise-successful build.
        def _maybe_artifact(text: str, suffix: str) -> str | None:
            try:
                return _artifact_registry.put_artifact(
                    workspace_id=ws, content=text, suffix=suffix
                )["artifact_id"]
            except Exception:  # noqa: BLE001 — output artifact id is additive
                return None

        md_artifact_id = _maybe_artifact(md_text, ".md")
        yaml_artifact_id = _maybe_artifact(yaml_text, ".yaml")

        # 9b. Emit run-summary (CS1) — audit/reproducibility record. Auxiliary:
        #     an emitter failure must never fail an otherwise-successful build.
        # Y1D D0: snapshot the ledger ONCE; reused by run_summary AND the return
        # envelope. summary() never raises (pricing problems become warnings).
        llm_usage_summary = usage_ledger.summary(
            (config.get("llm", {}) or {}).get("reference_pricing")
        )
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
                warnings=(
                    list(extraction.warnings)
                    + stage_model_warnings
                    + triage_warnings
                    + list(enrich_result.warnings)
                    + llm_fit_warnings
                ),
                source_index_fingerprint=source_index_fp,
                llm_usage=llm_usage_summary,
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
                + stage_model_warnings
                + triage_warnings
                + list(enrich_result.warnings)
                + llm_fit_warnings
                + ([cards_warning] if cards_warning else [])
            ),
            "tier_list_md_path": str(md_path),
            "tier_list_yaml_path": str(yaml_path),
            "tier_list_md_artifact_id": md_artifact_id,
            "tier_list_yaml_artifact_id": yaml_artifact_id,
            "run_summary_path": str(run_summary_path) if run_summary_path else None,
            "llm_usage": llm_usage_summary,
        }
    except Exception as exc:
        # Stage hint best-effort — if exc is MCPError it carries its own.
        return envelope_from_exception(exc, stage=Stage.PREFLIGHT)
