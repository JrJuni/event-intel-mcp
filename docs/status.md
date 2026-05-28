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
    - ⏳ **S1** — Runtime preflight + `check_runtime` tool + `models prepare` CLI (~3.5h 예상)
    - ⏳ **S2** — Capability Cards (schema + drafter + validator + ingester) (~5h)
    - ⏳ **S3** — Event Source → Extraction (chunked + cap + snippet-anchored) (~5h)
    - ⏳ **S4** — Enrichment + Fit Retrieval + Scoring + Resume (~6h)
    - ⏳ **S5** — Report (~2h)
    - ⏳ **S6** — MCP wrap + slug validation + integration tests (~4.5h)
  - **Resumable batches**:
    - batch1 = S0 + S1 + S2 (~10.5h) — runtime preflight + product mini-RAG 살아있는 시점
    - batch2 = S3 + S4 (~11h) — event extraction + scoring 끝, 실 전시회 fixture 수집 직전
    - batch3 = S5 + S6 + 실 전시회 smoke (~6h) — v0 surface 완성, Claude Desktop 통합
  - **Done When (14 결정적 기준)**: `pip install -e .` + pytest 75+ green / cold-start 회귀 0 / 5개 MCP tool Claude Desktop 호출 가능 / e2e (check-runtime → draft → validate → ingest → build) 한 사이클 성공 / 실 전시회 2-3개 다른 패턴으로 tier_list.md 생성 + 모든 S/A row의 `has_url + has_news >= 1` / tier_list.yaml round-trip / README에 5-tool workflow + 10 error_code 매핑 / bd-agent와 import 0 / envelope snapshot + schema drift test green / sanitize_slug edge case green / `build_event_tier_list` 가 product 미ingest 시 `PRODUCT_CONTEXT_MISSING` 반환 / Korean event_slug → `INVALID_INPUT` envelope의 `hint.suggested_slug` ASCII-safe 값 / `check_runtime`가 Brave quota 미노출 시 `remaining_quota: null` + status `ok` / `config/defaults.yaml` 필수 키 누락 → `CONFIG_ERROR` + path-localized hint

---

## 완료

(없음 — Phase 18S S0이 첫 커밋)

---

## 다음 진입 순서

1. **S1** (현재 다음 step) — runtime preflight + check_runtime tool + models prepare CLI
2. S2 — Capability Cards 라이프사이클
3. batch1 commit → fixture 검증 → batch2

세션 간 재개: 항상 `docs/status.md` + `~/.claude/plans/tender-mixing-badger.md` 두 파일을 fresh context로 진입 시 먼저 읽기.
