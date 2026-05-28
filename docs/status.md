# Status

프로젝트 진행 상황과 장단기 계획의 단일 원천. **현재 진행 중 / 최근 완료**만 여기에 둔다. 장기 계획·아직 안 시작한 작업은 [backlog.md](backlog.md).

---

## 진행 중

- **Phase 18S — Event Intelligence MCP v0 (2026-05-28 시작)**
  - **목표.** 전시회 참가사 리스트(URL / HTML / CSV / pasted text)를 evidence-backed BD 타겟 티어리스트로 변환하는 **standalone MCP 서버**. bd-coldcall-agent의 `discover_targets` 약점 두 가지(factual verification 부족 + bottom-up seed 부재)를 자체 mini-RAG + 5개 MCP tool로 해소.
  - **Plan v0.5 final** (`~/.claude/plans/tender-mixing-badger.md`). 3 round blind review (1st 8 findings P1×5+P2×3, 2nd 8 findings P1×4+P2×4, 3rd 4 findings P2×2+P3×2) 모두 반영. Repo name `event-intel-mcp` lock. 핵심 결정:
    - Standalone repo (bd-agent import 0)
    - FastMCP framework + 5 MCP tools (`check_runtime` / `draft_capability_cards` / `validate_capability_cards` / `ingest_product_context` / `build_event_tier_list`)
    - Capability Cards YAML (schema_version 1, Pydantic SSOT) = Product Context SSOT
    - Mini-RAG: bge-m3 + Chroma persistent, 단방향 fit retrieval (event evidence → product collection)
    - LLM bounded use (extraction + rationale 1-sentence only). tier/score는 코드 authority
    - 10 error_codes × 6 stages MCP error envelope + sanitize_slug + suggest_slug + max_chunks_per_event=12 cap
  - **Stream 진행**:
    - ✅ **S0** — Repo scaffold + provider abstractions + FastMCP skeleton + cold-start guard (commit `fa3932a`, 2026-05-28)
      - pyproject.toml (PEP 621, 11 deps)
      - `src/event_intel/` 11 서브패키지 (providers / cards / events / rag / scoring / report / tools / storage / runtime / prompts)
      - `errors.py` — 10 error_codes + 6 stages enum + MCPError envelope
      - `mcp_server.py` — FastMCP app + 5 tool stubs (envelope-shaped "not implemented yet")
      - `providers/{llm,embedding,vectorstore,search,fetch}.py` — ABC + default impl. **모든 heavy import lazy-loaded inside methods.**
      - `config/defaults.yaml` — extraction caps + scoring weights + tier rules
      - `tests/test_mcp_cold_start.py` — `torch/transformers/sentence_transformers/chromadb/bitsandbytes` sys.modules 미진입 회귀 가드 + envelope shape 검증
      - 테스트: 3/3 green
    - ✅ **S1** — Runtime preflight + `check_runtime` tool + `models prepare` CLI (2026-05-28)
      - `runtime/preflight.py` — `run_preflight(workspace_id, *, require_product_context, ...providers)` 5-check orchestrator. `load_config()` 가 nested required keys 검사 후 path-localized hint 반환.
      - `runtime/models.py` — `prepare_bge_m3` (sentence_transformers lazy import + smoke encode + cache 재검증), `verify_bge_m3` (cache-only check)
      - `tools/check_runtime.py` — preflight 를 **module reference 로 import** (lazy symbol import 은 monkeypatch 회피 패턴, project DO NOT 규칙)
      - `mcp_server.py` — `check_runtime` stub → 실제 handler 로 교체
      - `cli.py` — typer thin wrapper (`check-runtime` + `models prepare` + `models verify`). UTF-8 stdio reconfigure module top.
      - 테스트: **22/22 green** (S0 4 + S1 18 신규)
        - `test_runtime_preflight.py` 13건: 5-check 성공/실패 매트릭스 + R3 신규 3건 (product_context_missing, brave_quota_null_ok, config_error)
        - `test_mcp_error_taxonomy.py` 5건: 10 error_codes × 6 stages snapshot + envelope_from_exception fallback
        - `test_mcp_cold_start.py` 4건: stub loop 에서 check_runtime 제외 + envelope shape 검증 추가
      - cold-start 회귀 가드 유지 (preflight.py 와 tools/check_runtime.py 모두 module top 에서 torch/chromadb/sentence_transformers 미진입)
    - ✅ **S2** — Capability Cards (schema + drafter + validator + ingester) (2026-05-28)
      - `cards/schema.py` — Pydantic v2 SSOT (`SCHEMA_VERSION=1`, `CapabilityCards` + 5 nested models, `extra="forbid"` on every model so typos like `ideal_customers` fail loud)
      - `cards/validator.py` — `load_and_validate(path) -> CapabilityCards` + `validate_dict(data)`. YAML errors / non-mapping roots / pydantic ValidationErrors all funnel to `MCPError(SCHEMA_ERROR, stage=INGEST)` with a path-localized hint dict (`errors: [{path, type, msg}]`)
      - `cards/drafter.py` — single-shot draft via injected LLM provider. `text` / `file` source kinds (md/txt/pdf via lazy pypdf). Strips ```yaml fences. Truncates oversize input with a warning instead of failing.
      - `cards/ingest.py` — `flatten_cards_to_chunks()` emits content-derived stable ids (`product:summary`, `cap:{i}:{name}`, `ideal_customer:{facet}`, `trigger:{i}`, `bad_fit:{i}`, `competitor:{i}:{name}`) so re-ingest is an in-place upsert. Collection name `product_{ws}` agrees with the runtime preflight check.
      - `cards/schema_snapshot.json` + `tests/test_cards_schema_drift.py` — locks `model_json_schema()` against a committed snapshot; refresh path: `event-intel export-schema --out src/event_intel/cards/schema_snapshot.json` after bumping `SCHEMA_VERSION`.
      - `tools/{draft,validate,ingest}_capability_cards.py` — module-reference imports (drafter / validator / ingest / preflight / embedding / vectorstore / llm) so tests can monkeypatch through the MCP tool boundary. Cold-start safe at module top.
      - `mcp_server.py` — three stubs replaced with real handler delegations (lazy import inside `@app.tool()` bodies).
      - `cli.py` — 4 new flat subcommands: `draft-cards`, `validate`, `ingest`, `export-schema` (per plan §CLI Surface, NOT nested under `cards`).
      - 테스트: **59/59 green** (S0/S1 22 + S2 37 신규)
        - `test_cards_schema.py` 7건: minimum cards / keywords min_length / schema_version literal / extra="forbid" / weight bounds / geo default / SCHEMA_VERSION constant
        - `test_cards_validator.py` 7건: valid dict / path-localized error / missing file IO_ERROR / invalid YAML / non-mapping root / fixture happy path / module-ref import smoke
        - `test_cards_drafter.py` 7건: text input / fence strip / file input / oversize truncation+warning / empty source / ko lang clause / fixture md → validator round-trip
        - `test_cards_ingest.py` 5건: flatten emits product_summary + per-cap chunks / stable ids across runs / writes to workspace collection / re-ingest idempotent (no dup) / collection name matches preflight convention
        - `test_cards_schema_drift.py` 3건: snapshot matches / SCHEMA_VERSION=1 literal / root extra="forbid"
        - `test_cards_tools.py` 7건: validate envelope on SCHEMA_ERROR / validate happy path / draft envelope on missing key / draft writes yaml / ingest requires cards_path / ingest validates workspace_id / ingest end-to-end with mocked providers
        - `test_mcp_cold_start.py` 신규 1건 (`test_cards_tools_keep_module_top_cold`): cards modules + tools must NOT pull heavy ML imports at module top
      - cold-start 회귀 가드 유지 + `fresh_sys_modules` 픽스처 수정 — 기존 snapshot-restore 가 pydantic lazy `__getattr__` 캐싱과 충돌해 (pop 한 `pydantic.root_model` 이 후속 `from pydantic import RootModel` 으로 재로드되지 않음) `KeyError: 'pydantic.root_model'` 유발. 이제 teardown 에서 `event_intel.*` + `FORBIDDEN_HEAVY` 만 명시적으로 purge.
      - CLI smoke OK: 5 top-level subcommands (`check-runtime`, `draft-cards`, `validate`, `ingest`, `export-schema`) + `models` subapp. `export-schema` JSON 출력 검증.
    - ✅ **S3** — Event Source → Extraction (chunked + cap + snippet-anchored) (2026-05-28)
      - `events/source_capture.py` — 4 source kinds (`html_file`, `html_text`, `csv_file`, `text`). trafilatura lazy import (`include_tables=True`, `favor_recall=True` for exhibitor cards that often live inside structured layouts). CSV path keeps parsed rows on `SourceCapture.csv_rows` so extraction can short-circuit the LLM for structured rows in S4+. Failures fold to `MCPError(SOURCE_CAPTURE_FAILED, stage=EXTRACTION)`.
      - `events/extraction.py` — chunked LLM extraction. `_split_chunks` prefers double-newline boundaries (preserves exhibitor card structure) and falls back to single-newline + hard slicing. `max_chunks_per_event=12` cap (review R2-#7): excess chunks dropped + warning. Snippet floor `>= 20 chars` (review R2-#9 raw_extraction): rows below are dropped silently. Lang-specific `_normalize_name` strips legal suffixes (`Co., Ltd.`, `Inc.`, etc.) + Korean prefixes (`㈜`, `주식회사 `). Confidence `< extraction_confidence_min` (0.6 default) routes to `needs_review` instead of main `candidates`. Tolerant LLM JSON parser (strips fences, recovers JSON array from prose wrapper).
      - 테스트: **80/80 green** (S0/S1/S2 59 + S3 21 신규)
        - `test_source_capture.py` 8건: html/csv/text/html_text happy paths + unsupported kind / missing file / short-capture warning / empty inline text
        - `test_event_extraction.py` 12건: normalize_name (en + ko) / _split_chunks paragraph boundary / english html happy / snippet floor drops short rows / **chunk cap triggers warning + truncates to 12 + 12 LLM calls** / ko name merge collapses ㈜ vs 모비우스랩 (single candidate, chunk_indices accumulates) / low confidence routes to needs_review / empty capture → SOURCE_CAPTURE_FAILED / malformed LLM JSON recovered from prose wrapper / LLM exception → UPSTREAM_ERROR (retryable) / module-reference import smoke
        - `test_mcp_cold_start.py` 신규 1건 (`test_events_modules_keep_module_top_cold`): events.* modules must NOT pull heavy ML imports at module top
      - cold-start 회귀 가드 유지 (trafilatura lazy in `_strip_html`, all other heavy deps deferred to provider methods).
    - ✅ **S4** — Enrichment + Fit Retrieval (단방향) + Scoring + Resume (2026-05-28)
      - `events/enrichment.py` — for each ExhibitorCandidate: trust extraction-supplied URL OR Brave web search for `"{name}" official site` + score each candidate URL (host-stem difflib ratio + name-token hit + LinkedIn/FB/Wikipedia/Crunchbase/Bloomberg/X/YouTube hard reject), pick top-scoring above threshold. Brave news search for `"{name}"` within `news_days_back`. **Per-(query,kind,lang) sha1 cache** under `~/.event-intel/cache/search/{ws}/` — re-runs hit cache with 0 search calls. **Resume artifact** JSONL at `~/.event-intel/resume/{ws}.jsonl` — pre-existing rows skipped by name, new rows appended. `max_companies` cap (review #11). Failures fold to `MCPError(UPSTREAM_ERROR, stage=ENRICHMENT, retryable=True)`.
      - `rag/retriever.py::retrieve_fit_event_to_product` — **단방향** (review R2-#5 정정). Embed (name + snippet + description + top 3 news titles) for each exhibitor in a single batch, query `product_{ws}` collection top_k=5, return `FitResult(capability_fit=avg(top_k cosine), capability_fit_breakdown, competitor_hits, bad_fit_hits)`. Cosine derived from Chroma squared-L2 with normalized bge-m3 embeddings (`sim ≈ 1 - dist/2`, clamped). **One batched VS query call regardless of exhibitor count**. Never queries an `event_*` collection (mock-verified).
      - `scoring/dimensions.py` — 7 deterministic dimensions (capability_fit, source_confidence, buying_signal w/ trigger-keyword bonus, website_verification, category_fit via ideal_customer overlap, competitor_penalty, bad_fit_penalty). All return 0..1 floats. Penalty weights in yaml are negative so the same `Σ w_i × d_i` formula subtracts naturally.
      - `scoring/rules.py::decide_tier` — pure function over `(final_score, evidence_floor, tier_rules)`. **Evidence floor 3-state lifecycle** (Contract #9): `floor = int(has_official_url) + int(has_news_signals) ∈ {0,1,2}`. S requires floor ≥ 2; A requires floor ≥ 1; B/C floor 0 OK.
      - `scoring/compute.py::score_exhibitors` — ties dimensions × weights → `final_score = clamp(0, 10, Σ × 10)` → tier via rules. Optional **rationale call** runs Sonnet (1 sentence + opening angle) only for `rationale_for_tiers=("S","A")` by default — LLM bounded use (Contract #5). Rationale failures swallowed (decorative). En/Ko prompt variants in `_RATIONALE_PROMPT_*`.
      - 테스트: **106/106 green** (S0/S1/S2/S3 80 + S4 26 신규)
        - `test_enrichment.py` 7건: 5-candidate happy path / extraction-supplied URL skips web search / **re-run hits cache with 0 search calls** / resume skips done rows by name + only retries remaining / max_companies cap + warning / upstream Brave failure → UPSTREAM_ERROR (retryable) / official_url threshold filters bad-host hits (LinkedIn/Wikipedia)
        - `test_rag_ingest_retrieve.py` 6건: product collection naming matches preflight / similarity_from_distance clamping / averages top_k similarity + breakdown / counts competitor + bad_fit hits / **only queries product_{ws} not event_*** + single batched call / empty input → empty list
        - `test_scoring.py` 12건: evidence_floor matrix (all 4 combos) / website_verification binary / buying_signal news count brackets / buying_signal trigger keyword bonus / category_fit zero w/o cards / category_fit increases with industry overlap / decide_tier **floor caps tier** (same score 9.0 → floor 2 = S, floor 1 = A, floor 0 = B) / decide_tier picks highest satisfied / full-pipeline floor cap (Both → S, UrlOnly → A, NoneEvidence → B) / bad_fit + competitor penalty drops tier / rationale call gated by tier (LLM count = #{S+A}) / length mismatch → MCPError(INTERNAL)
        - `test_mcp_cold_start.py` 신규 1건 (`test_s4_modules_keep_module_top_cold`): enrichment + retriever + scoring.* keep torch/transformers/sentence_transformers/chromadb/bitsandbytes out of sys.modules at import
      - cold-start 회귀 가드 유지. Heavy deps (sentence_transformers, chromadb) only enter via injected provider methods.
    - ⏳ **S5** — Report (~2h)
    - ⏳ **S6** — MCP wrap + slug validation + integration tests (~4.5h)
  - **Resumable batches**:
    - batch1 = S0 + S1 + S2 (~10.5h) ✅ — runtime preflight + product mini-RAG 살아있는 시점
    - batch2 = S3 + S4 (~11h) ✅ — event extraction + scoring 끝, 실 전시회 fixture 수집 직전
    - batch3 = S5 + S6 + 실 전시회 smoke (~6h) — v0 surface 완성, Claude Desktop 통합
  - **Done When (14 결정적 기준)**: `pip install -e .` + pytest 75+ green / cold-start 회귀 0 / 5개 MCP tool Claude Desktop 호출 가능 / e2e (check-runtime → draft → validate → ingest → build) 한 사이클 성공 / 실 전시회 2-3개 다른 패턴으로 tier_list.md 생성 + 모든 S/A row의 `has_url + has_news >= 1` / tier_list.yaml round-trip / README에 5-tool workflow + 10 error_code 매핑 / bd-agent와 import 0 / envelope snapshot + schema drift test green / sanitize_slug edge case green / `build_event_tier_list` 가 product 미ingest 시 `PRODUCT_CONTEXT_MISSING` 반환 / Korean event_slug → `INVALID_INPUT` envelope의 `hint.suggested_slug` ASCII-safe 값 / `check_runtime`가 Brave quota 미노출 시 `remaining_quota: null` + status `ok` / `config/defaults.yaml` 필수 키 누락 → `CONFIG_ERROR` + path-localized hint

---

## 완료

(없음 — Phase 18S S0이 첫 커밋)

---

## 다음 진입 순서

1. **batch1 완성** (S0+S1+S2 ✅) — runtime preflight + product mini-RAG 활성
2. **S3 ✅** — Event Source → Extraction (chunked + cap + snippet-anchored)
3. **S4 ✅** — Enrichment + Fit Retrieval (단방향) + Scoring + Resume — batch2 완료
4. **S5** (다음 step) — Report (~2h)
5. S6 — MCP wrap + slug validation + integration tests (~4.5h)

세션 간 재개: 항상 `docs/status.md` + `~/.claude/plans/tender-mixing-badger.md` 두 파일을 fresh context로 진입 시 먼저 읽기.
