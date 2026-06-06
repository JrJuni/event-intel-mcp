# Status

프로젝트 진행 상황과 장단기 계획의 단일 원천. **현재 진행 중 / 최근 완료**만 여기에 둔다. 장기 계획·아직 안 시작한 작업은 [backlog.md](backlog.md).

---

## 진행 중

- **Phase 18V.1 — 머지 후 blind review 정제 + CI 게이트 (2026-06-06)**
  - **계기.** 18V(PR #1) 머지 후 정적 blind review 7건(P1×4, P2×3) — 전부 코드 대조로 valid 확인. 스택 PR로 순차 수정, 각 단계 **실연결된 eval matrix로 회귀 검증**. (메모리: 리뷰는 HEAD 대조 후 경험적으로 해소.)
  - ✅ **#2 eval 실연결** (PR #2) — 하버스가 `target_mode`/`reference_date`/`competitor_similarity`를 scorer에 안 넘겨 4a recency·4b penalty·target_mode가 **실제로 미검증**이었음. 전달하도록 수정 + fixture에 sim 부여(임계 0.5 위/아래 혼합) → penalty 실발화. 1B가 실제 retriever 실행. **baseline A8/B5/C3 → A8/B2/C6**(penalty 정확). 내가 PR #1 본문에 쓴 "baseline이 안전성 입증"은 과장이었음 — 인정·수정.
  - ✅ **#1 evidence 관련성 게이트** (PR #7, #3 대체) — 제3자 경로매칭(`/products` 등)이 floor 2 만들던 것 차단: identity는 official 도메인과 **same-site**일 때만(서브도메인 인식 `registrable_domain`), extra 쿼리 결과는 same-site OR 회사명 토큰(whole-token) 일치만 채택.
  - ✅ **#3/#4 결정론** (PR #4) — evidence 예산을 **per-company**(순서·캐시상태 무관, attempt 카운트) + 캐시 키에 `count`/`days` + resume **이벤트 스코프**(`resume/{ws}/{slug}.jsonl`).
  - ✅ **#5/#6/#7 P2** (PR #5) — capability_fit = **top-N 평균**(top_k 20 vs ≤10 capability 평탄화 해소); 카드 **조기 검증**(enrichment 비용 전) + cards=None 경고를 "랭킹 변동"으로 강화; floor invariant **티어별 최소**(S=2); `target_mode`를 `tier_list.yaml`에 기록(`REPORT_SCHEMA_VERSION` 3).
  - ✅ **CI 게이트** (PR #6/#8/#9) — GitHub Actions `pytest`+`ruff` 둘 다 **blocking**, `main` 브랜치 보호로 `pytest (python 3.11)` strict 필수. ruff 클린 스윕(194건). actions Node 24(checkout@v5/setup-python@v6).
  - **CI가 잡은 크로스플랫폼 버그 2건**: (a) eval가 핵심 미검증(#2), (b) `load_tier_list_yaml`이 Windows는 통과/Linux는 `OSError errno 36`(긴 YAML 문자열을 경로로 probe) — `is_file()` guard로 수정. → **Windows 단독 통과 버그를 Linux 러너가 차단**.
  - **테스트**: 430 passed, ruff clean. PR #1,#2,#7,#4,#5,#6,#8,#9 머지, 브랜치 정리(`main`만).
  - **round-2 정제 (2026-06-06)** — 머지된 main에 2차 정적 blind review 7건(전부 valid, HEAD 대조). HIGH 3(PR #11): #1 news floor-evidence 게이트+generic-token name 매칭, #3 report floor invariant를 effective tier_rules로(하드코딩 제거), #5 카드↔vector ingest replace(orphan 제거). MEDIUM 2(PR #12): #7 멀티테넌트 same_site(github.io/vercel.app 등 분리), #4 top-N·recency eval 실검증(retriever→scorer + news published_at). 잔여 #2(캐시 TTL/resume 신선도)·#6(예산 round-robin) → backlog #13. **438 passed**.
  - **남은 갭(18W)**: backlog #13 참조.

- **Phase 18V — 범용 exhibition intelligence 엔진 (2026-06-06, plan `snoopy-weaving-robin.md`, branch `phase-18v`)**
  - **계기.** 18U는 MongoDB×GTC 단일 gold set 기준 MVP 합격. 모든 제품·전시회 범용화에 backlog #12의 4개 P1 필요. 18U 교훈("측정 먼저, 튜닝 마지막")에 따라 **eval matrix를 먼저** 짓고 모든 변경을 거기에 회귀 검증. plan v1→v3 (blind review 2라운드, 코드 대조 후 14건 전부 수용).
  - ✅ **18V-1 eval matrix** — 2층: scoring matrix(fake FitResult, 빠른 회귀) + pipeline-contract matrix(fake provider, 실제 enrichment+retriever). metrics: `ranking_accuracy_auc`(정규화 pairwise, 셀크기 무관) / mode-aware `precision_at_10` / `competitor_leakage_rate` / `evidence_false_positive_rate`. `event-intel eval-matrix` CLI. baseline 스냅샷(DB×AI: A8/B5/C3, AUC 1.0, leakage 0, P@10 0.8) 커밋 상수.
  - ✅ **18V-2 (4a)** news relevance(회사명 매칭→generic 반감) + recency decay + **UTC-aware timestamp 정규화**(naive/date-only가 aware reference_date와 충돌하던 TypeError 차단, parse+cache 양쪽). `timeutil.py`.
  - ✅ **18V-2 (4b)** retrieval **pool 분리** + **sim-gated negative penalty**(count는 negative-only pool에서 포화 → max similarity≥threshold만 penalty). `FitResult`에 competitor/bad_fit_similarity.
  - ✅ **18V-2 (4c)** 규칙기반 **CJK bigram** 토크나이저(한·일·중 회사명/카테고리 세그먼트, lazy-import 불필요).
  - ✅ **18V-2 (item1)** **typed evidence**(`EvidenceItem{type,url,source_domain,published_at}`) + canonical URL **dedupe** + 결정적 type precedence(경로 기준) + **identity-vs-activity floor**(floor 2는 activity/독립출처 요구 — official_url+동일사이트 product_page는 floor 1). budgeted 신규 Brave 쿼리. `ENRICH_CACHE_VERSION` 3. `REPORT_SCHEMA_VERSION` 2. floor-invariant를 `rules.compute_evidence_floor`로 단일화.
  - ✅ **18V-3 (item2)** `target_mode`(customer/partner/ecosystem) **build 실행 인자**(우선순위 arg>config>card>customer, None sentinel) + 카드 스키마 **v1→v2 migration**(기존 v1 무손상) + **카드 로드 계약**(파일 부재→warning+customer, 파일 존재+invalid→명시 에러). mode별 penalty factor(customer 1/1, partner 0/1, ecosystem 0/0).
  - **테스트**: 전체 green. 커밋 6개 엄격 분리. baseline 무회귀(A8/B5/C3) 전 단계 유지.
  - **남은 갭(18W 후보)**: 형태소 분석 라이브러리(janome/jieba), 9-셀 full matrix, ecosystem 셀 leakage 재정의, 양방향 retrieval.

- **Phase 18U — 스코어링 변별력 복구 (2026-06-05, plan `snoopy-weaving-robin.md`)**
  - **계기.** 실사용 검증(제품=MongoDB Atlas 카드, 이벤트=NVIDIA GTC 2026 실참가사 34곳)에서 티어가 전부 B로 뭉치고 경쟁사가 A에 섞임. 3라운드 blind review로 "입력/신호 오염 상태에서 penalty만 튜닝하면 과적합" 리스크 도출 → **순서 고정**: 입력 identity → 신호 정확성 → 마지막에 penalty.
  - ✅ **S1 news 파서 버그** (`782a64c`) — Brave `/news/search`는 최상위 `results`인데 파서가 `data["news"]["results"]`를 읽어 전원 news=0 → S 원천 불가. 수정 후 news 0→156. contract 테스트 `test_search_provider.py`.
  - ✅ **S2 입력 identity** (`7e92f3e`) — trafilatura가 디렉터리에서 `<h2>`·href를 버려 회사명이 도메인·url=None이던 문제: 링크 많은 페이지를 구조보존 strip(헤딩 자기줄 + `text (url)`)으로 라우팅 + 프롬프트 헤딩명/url. 캐시 키에 `ENRICH_CACHE_VERSION`(파서 bump 시 stale 무효). news published_at 보존 + 비-기사 path 드롭.
  - ✅ **S3 신호 정확성** (`93d8965`) — capability_fit를 **capability 청크만** 평균(경쟁사가 자기 competitor 청크와 가까워 fit 부풀던 것 제거). category_fit substring→토큰경계 집합교집합 + 불용어 + 약어 whitelist(AI/ML/US…).
  - ✅ **S4 penalty 튜닝** — competitor_penalty -0.1→-0.35, bad_fit_penalty -0.1→-0.25 (`config/defaults.yaml`).
  - **실측 검증 (HTML 경로, GTC 34곳, S 0 / A 7 / B 23 / C 4)**: 경쟁사 5곳 **S/A=0**(Vespa B#19·Activeloop B#23·PlanetScale B#25·Snowflake B#26·ClickHouse B#28), 타깃 median rank **5위** vs 경쟁사 **25위**. 상위 A 7개 전부 진짜 타깃/개발자도구. MaxLinear(반도체) capfit 0.00→C(약fit→저티어 회귀 속성 유지). **→ 경쟁사 hard-cap 불필요: penalty+신호정확성으로 충분(R2 결정 실증).**
  - **테스트**: 전체 green (+신규: search_provider 4, source_capture 디렉터리 보존, enrichment 캐시버전·비기사·published_at, retriever capability-only·전부-competitor→0, scoring category 토큰경계/약어).
  - **blind review 3라운드**: R1(입력 정렬 순서·category_fit·캐시버전 도출), R2(범용엔진 vs MVP 범위 — 사용자 MVP 선택, 4개 일반화 항목 backlog), R3(**stale 스냅샷** — 지적 5개 중 4개 이미 S2/S3서 수정; "A=Vespa"는 수정 전 상태, 현재 Vespa B#19). 상세 `lesson-learned.md`.
  - **남은 갭(비-blocking, Phase 18V backlog)**: news 회사일치도·기사판별·recency, retrieval pool 분리, target_mode, evidence_types, 다중도메인 eval.

- **Phase 18T.2 — 무마찰 `.mcpb` 설치 (2026-06-04, plan `snoopy-weaving-robin.md`)**
  - **목표.** 재설치마다 경로/키를 다시 입력하는 마찰 제거. (사용자 피드백: "path는 자동으로 못 찾나")
  - ✅ `repo_path`/`PYTHONPATH` **제거** — `event_intel`이 editable 설치라 PYTHONPATH 없이 `python -m event_intel.mcp_server` import됨. 경로 입력 1개 소거.
  - ✅ `python_path`에 `${HOME}/miniconda3/envs/event-intel/python.exe` **기본값** — 폼 pre-fill(확인만). (Claude Desktop이 ${HOME} 확장 — 다음 설치 때 실확인 필요.)
  - ✅ `src/event_intel/_env.py::load_project_env` — 패키지 위치로 repo 루트 역산 후 `<repo>/.env` 로드. 빈 폼키(`""`)는 pop 후 .env로 채움(override=False), 비어있지 않은 폼키는 우선. `mcp_server.py` + `cli.py`가 사용. → API 키 폼 **optional**(brave required:false).
  - ✅ manifest `.mcpb` **0.5.0** 재빌드(validate 통과), 0.4.0 제거. launcher 메시지 갱신.
  - ✅ **blind review R1 반영**(avg 71%, 4건 전부 accept): boolean form env authoritative 정책+테스트(#2), plan/코드 정합(#3), 번들↔패키지 버전 별도 트랙 문서화(#4). 커밋 분리(#1): warm-on-start(`c9b8f1a`) / 18T.2 install(`cf19080`). 타임아웃 진단은 코드 0줄로 deferred 유지.
  - **테스트**: 372/372 green (+8: env_loading 4 + mcpb_manifest 4). cold-start 0(`_env`는 os/pathlib/dotenv만).
  - **결정(사용자 확인)**: 경로+.env 키 자동로드 풀스코프 / 설치 UX를 타임아웃 진단보다 먼저 / blind review 엄격 커밋 분리.
  - ✅ **cwd 상대 output 경로 버그 수정 (2026-06-05)**: build가 `Path("outputs")`(cwd 상대)에 써서 Claude Desktop(임의 cwd)에서 `PermissionError WinError 5`. 패키지 위치로 repo-root 역산(`_outputs_base`, EVENT_INTEL_OUTPUT_DIR override). `.env`에 이은 동일 cwd-의존성 클래스 — lesson-learned 기록. 회귀 테스트 3건(`test_output_path.py`). commit `81c395e`.
  - ✅ **Claude Desktop 풀 e2e 검증 완료 (2026-06-05)**: 두 MCP-런타임 버그(worker-thread chromadb 데드락 + cwd output) 수정 후, Claude Desktop에서 `check_runtime`(smoke ok:true) → `build_event_tier_list`(smoke, simtos) → tier_list.md/yaml 정상 기록. worker-thread 경로 최종 검증. (C tier 5건 = product_smoke[AI/NPU] vs 공작기계 전시회 = 정상적 약-fit 판별.)
  - ✅ **4분 타임아웃 진단·수정 완료 (2026-06-04)**: 가설이 3번 뒤집힘 — C2(stdout 오염)·warm-up 둘 다 **반증**(프로브: stdout 0 bytes, warm_up=false도 240s 행). **진짜 원인: FastMCP worker thread에서의 첫 `import chromadb` 데드락**(메인 스레드 pre-import 시 240s행→1.8s, 3중 실증). 단독 collection_info(0.81s)·단순 asyncio 하니스(0.78s)는 재현 못 함 → 충실한 재현은 실 MCP subprocess뿐. **수정**: `mcp_server._preimport_heavy_deps`(main()에서 chromadb+sentence_transformers 메인 스레드 pre-import; 서버 시작 ~7s↑, tool은 즉시). **회귀 가드**: `tests/test_stdio_integrity.py`(slow). blind review R1(avg 68%)이 WHERE는 확인, false-negative 테스트 제안은 정정 후 채택. 상세: `docs/lesson-learned.md` 2026-06-04.

- **Phase 18T.1 — ChatGPT OAuth 설치 UX (2026-06-04, plan `snoopy-weaving-robin.md`)**
  - **목표.** `.mcpb` 설치 폼에서 ChatGPT OAuth를 바로 선택 가능하게 — 기존엔 config.yaml 손편집이 유일 경로라 "OAuth 쓰기 너무 어려움" 피드백.
  - ✅ `runtime/preflight.py::load_config` — `_apply_llm_provider_env_override` 추가. precedence: `EVENT_INTEL_LLM_PROVIDER`(명시, 양방향 authoritative, invalid→CONFIG_ERROR) > `EVENT_INTEL_USE_CHATGPT_OAUTH`(opt-in boolean: truthy→oauth, falsey/empty→**no-op**). `path is None` 분기에서만 적용(테스트 호환 분기 불변).
  - ✅ `providers/llm.py::ChatGPTOAuthProvider.login(force=)` — 터미널 주도 PKCE 로그인 public 메서드. `ping()` not_logged_in fix 문자열을 `event-intel login-chatgpt`로 갱신 → `check_runtime`이 자동 노출.
  - ✅ `cli.py` — `login-chatgpt` 명령 (module-reference import, envelope on failure).
  - ✅ `mcpb/manifest.json` — `use_chatgpt_oauth` boolean 체크박스 + `EVENT_INTEL_USE_CHATGPT_OAUTH` env 매핑 + anthropic desc 갱신 + **version 0.2.0 → 0.3.0**. `.mcpb` 재빌드(`mcpb validate` 통과, 4.2kB).
  - ✅ docs — README "Choosing an LLM provider" 섹션 + mcpb/README 설치 단계/버전.
  - ✅ **모델 워밍업 — 비동기 패턴 (Claude app 타임아웃 대응)** — 진단: `check_runtime`은 bge-m3를 로드하지 않음(`is_ready`는 캐시 존재만 확인). 무거운 비용은 매 `build_event_tier_list`가 새 `BgeM3Provider`로 ~1.3GB(콜드 11~20s)를 재로드하는 것(프로세스 캐시 부재).
    - **초안(동기 warm)은 폐기** — tool 호출 안에서 동기 로드 시 Claude app 자체 타임아웃에 걸림(coldcall 선례). `docs/lesson-learned.md` 2026-06-04 참조.
    - `runtime/warmup.py` (신규) — 프로세스 전역 thread-safe 상태기계(`not_started/warming/ready/failed`) + `start(block=)` + `status()`. 보수적 ETA 메시지.
    - `BgeM3Provider._MODEL_CACHE` + `_CACHE_LOCK` — instance 간 모델 재사용 + 백그라운드 로드/동시 build 중복 로드 방지.
    - `check_runtime`는 **항상 `checks.warm_up` 상태 보고**. `warm_up=true`는 백그라운드 로드 *시작*만 하고 즉시 리턴(폴링 패턴). MCP=비동기, CLI `--warm-up`=inline blocking(`warm_up_block`). manifest 변경 불필요.
    - 검증: trigger 1.27s 리턴(status=warming), 14s 후 폴링 ready(load_seconds 14.1).
    - **opt-in 시작 워밍업** (`warmup.maybe_warm_on_start`) — `EVENT_INTEL_WARM_ON_START` truthy AND `is_ready()`(캐시 존재)일 때만 서버 부팅 시 백그라운드 워밍. 기본 off. 캐시 없으면 다운로드 방지 위해 skip. `mcp_server.main()`에서 호출(논블로킹). manifest `warm_on_start` 체크박스 + env, **`.mcpb` 0.4.0**.
  - **테스트**: 364/364 green (+24: OAuth preflight 6 + cli 1 + oauth provider 3, warmup manager 5+3 + preflight warm 3 + embedding 3). cold-start 0 유지.
  - **결정(사용자 확인)**: 체크박스 opt-in 전용(미체크가 기존 oauth 설정 안 깸) + CLI 명령 + lazy 폴백 / 워밍업은 **비동기 + status 폴링**(런타임 동기 금지).

- **Phase 18T Done When 잔여 항목 (2026-05-29)**
  - ✅ Done When #4 — 실 전시회 smoke ≥2 verdicts 확보 (2026-05-29):
    - `operator_capture_required`: smarttechkorea.com x2, tbse26.mapyourshow.com, directory.conexpoconagg.com (Vue 감지 시 analyzer가 capture로 분류 — Map Your Show `/ajax/remote-proxy.cfm` endpoint를 페이지 본문에 명시함에도 보수적으로 capture 권고. → backlog #11에서 해소)
    - `static_html` (0.98 confidence): simtos.org → acquire-source까지 OK + build-event 풀 파이프라인 e2e (20 candidates → 10 enriched → tier_list.md/yaml C tier 10건, machine tool 회사들이라 Mobilint NPU fit 약함 — 점수 분포 정상)
  - ✅ Done When #13 — Claude Desktop `.mcpb` 0.3.0 설치 → 8 tools 노출/호출 확인 (2026-06-04, 사용자 스크린샷 검증). **→ Phase 18T 완전 종료.**
  - **Done When #1–13 모두 완료** (cold-start 0, 14×7=98 envelope, core lock clean). 테스트 수치는 18T.1 기준 364/364.

- **Phase 18U (별도 plan — 18T 마감 후 진입)**
  - Streamable HTTP transport + OAuth 2.1 PKCE + ChatGPT App 등록. 상세: `docs/backlog.md`.
  - 후보 묶음: manifest provider 선택을 18U의 원격 OAuth 작업과 통합 검토.

---

## 완료

- **Phase 18T — Adaptive Source Acquisition Layer (2026-05-29 완료)**
  - **목표.** URL 하나로 analyze → probe → artifact → `build_event_tier_list` 파이프라인 진입. 5개 → 8개 MCP 도구.
  - **Baseline**: Phase 18S commit `2682032`, 173/173 green.
  - **Stream 진행**:
    - ✅ **T0** — acquisition scaffold + error taxonomy 10→14 + stage 6→7 + `text_file` source kind + 3 stub tools (commit `78481f7`)
      - `src/event_intel/acquisition/__init__.py` 패키지 신설
      - `storage/artifacts.py` — path resolution + atomic write + manifest read/write skeleton
      - `errors.py` +4 ErrorCode (`ACQUISITION_AMBIGUOUS`, `LOGIN_REQUIRED`, `OPERATOR_CAPTURE_REQUIRED`, `ROBOTS_DISALLOWED`) + Stage `acquisition` (총 14×7=98 쌍)
      - `source_capture.py` — `text_file` source kind 추가, 기존 4 kinds byte-for-byte 불변
      - `cli.py` — `--text-file` → `source_kind="text_file"` 매핑 수정 (R2-4 fix)
      - 3 stub tools `analyze_event_page` / `probe_exhibitor_endpoint` / `acquire_exhibitor_source` → `INTERNAL` envelope. `mcp_server.py` lazy-wrap 등록
    - ✅ **T0.5** — URL safety + robots + raw_fetch + http_status_map (commit `df0a404`)
      - `acquisition/url_safety.py` — `validate_url()` (private IP / localhost / non-http scheme / userinfo / no-dot host 거부) + `host_relation()` (PSL-free same/subdomain/cross)
      - `acquisition/robots.py` — stdlib `urllib.robotparser` + per-host 1h cache + conservative deny on 5xx
      - `acquisition/raw_fetch.py` — `RawResponse` raw GET/POST. safety violation→raises, transport failure→returns with `status=0 + network_error`. HTTP semantic mapping 없음 (R2-3)
      - `acquisition/http_status_map.py` — `map_http_response()` Contract #9 전체 소유. short-body SPA shell 패스 (R2-2). `(should_proceed, MCPError | None)` 반환
      - tests: `test_url_safety.py` 10건 + `test_robots.py` 6건 + `test_raw_fetch.py` 6건 + `test_http_status_map.py` 8건 = 30건 신규
    - ✅ **T1** — `analyze_event_page` real impl (commit `6f0d3c4`)
      - `acquisition/analyzer.py` — `AnalyzeHints` pydantic schema (`extra="forbid"`) + `analyze_page()` 1 Sonnet call + `<PAGE_HTML>` / `<PAGE_SCRIPTS>` UNTRUSTED 딜리미터 + ignore-instruction rule
      - `tools/analyze_event_page.py` — stub body → real impl. module-reference import 패턴.
      - `prompts/en/analyze_event_page.txt` + `prompts/ko/analyze_event_page.txt`
      - tests: `test_analyzer.py` 10건 (4 verdict FakeLLM + private IP → INVALID_INPUT + robots → ROBOTS_DISALLOWED + 401→LOGIN_REQUIRED + 5xx→UPSTREAM_ERROR + non-JSON + prompt-construction delimiter + schema-rejects-unexpected-verdict)
    - ✅ **T2** — `probe_exhibitor_endpoint` real impl (이번 세션)
      - `acquisition/probe.py` — `_response_looks_like_exhibitor_list(body, lang)` en+ko 키워드 밀도 스코어 + `ProbeAttempt` / `ProbeResult` dataclass + `probe_endpoints()` (candidate 루프, method allowlist {GET,POST}, host_relation 체크, advisory warning carry) + `probe_embedded_json()` (stdlib regex script_id/script_var_name + dotted key_path walk)
      - `tools/probe_exhibitor_endpoint.py` — stub body → real impl
      - tests: `test_probe.py` 14건 (winner / all-below-threshold → ACQUISITION_AMBIGUOUS / 4xx skip / 5xx all → ACQUISITION_AMBIGUOUS / Korean scorer / cross-origin skip / embedded_json regex / max-5 cap / hints validation → INVALID_INPUT / method allowlist PUT skip / advisory warning carry / tool wrapper happy / tool wrapper failure envelope)
      - **핵심 패턴**: Korean 스코어 `max(ko_density, en_density)` (별도 분모), string-path `patch()` (cold-start purge 후 모듈 identity 보장), MCPError 클래스 test body 내부 import (class identity 보장)
    - ✅ **T3** — `acquire_exhibitor_source` orchestrator + manifest + e2e (이번 세션)
      - `storage/artifacts.py` 완성 — `artifact_dir` / `write_artifact` (atomic) / `write_manifest` (atomic) / `read_manifest` / `verify_artifact_sha256` / `make_manifest` / `sha256_of` / `ManifestModel` / `EVENT_INTEL_ARTIFACTS_DIR` env override
      - `acquisition/acquire.py` — 5-verdict 오케스트레이터. analyze→(probe→)fetch→write_artifact+manifest. 캐시 hit sha256 검증 (corrupt→refetch+warn). XHR 페이지네이션 `max_pages=3`. embedded_json → `("text_file", path)` (v1.1 R1-3 fix)
      - `tools/acquire_exhibitor_source.py` — stub body → real impl
      - `cli.py` — `analyze-page` + `acquire-source` subcommand 추가
      - tests: `test_artifacts.py` 10건 (atomic write / manifest round-trip / sha256 correct/mismatch/missing / read None cases / env override) + `test_acquire.py` 12건 (5 verdict branches / cache hit / sha256 mismatch refetch / refetch=True / Korean slug → INVALID_INPUT / workspace isolation / tool wrapper)
      - cold-start: `test_acquire_module_keeps_module_top_cold` + `test_probe_module_keeps_module_top_cold` 추가
  - **최종 수치**: 290/290 green, `git diff 2682032 src/event_intel/tools/build_event_tier_list.py` = empty (core lock clean)
  - **Done When 잔여**: #4 (real smoke), #13 (Claude Desktop reload)

- **Phase 18S — Event Intelligence MCP v0 (2026-05-28 완료)**
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
    - ✅ **S5** — Report (2026-05-28)
      - `report/tier_list_md.py` — 6-section Markdown render (event header + Summary line + Tier S/A/B/C + Needs Review). Per-row chips (`url`, `news×N`, `snippet-only`), evidence snippet (whitespace-collapsed for scannability), top-3 news titles, rationale + angle when Sonnet ran, top-3 capability_fit breakdown. Within tier, descending by final_score. **Floor invariant guard**: `_assert_floor_invariant` raises if any S/A row has `has_official_url + has_news_signals < 1` — defensive against misconfigured `tier_rules`. En/Ko section headers.
      - `report/tier_list_yaml.py` — machine-readable yaml. `build_tier_list_payload` → `dump_tier_list_yaml` → `load_tier_list_yaml` round-trips (load handles str or Path). `REPORT_SCHEMA_VERSION=1`. Payload includes per-exhibitor name/tier/final_score/evidence_floor/official_url/news_count/source_snippet/rationale/angle/capability_fit/capability_fit_breakdown + `needs_review` array.
      - `report/brief_export.py` — optional `product_brief.md` from `CapabilityCards` (people-facing export view per plan §Context). Renders capabilities (keywords + buyer_pains + evidence_queries), ideal_customer, optional buying_triggers / bad_fit / competitors. En/Ko labels.
      - 테스트: **115/115 green** (S0/S1/S2/S3/S4 106 + S5 9 신규)
        - `test_report.py` 8건: 6 sections present + summary count line / floor invariant guard raises on bad S row / needs_review name does not leak into S/A/B/C sections / ko section headers / yaml round-trip preserves rationale + angle + breakdown + tier_counts / yaml load_from_path / brief renders all sections / ko brief labels
        - `test_mcp_cold_start.py` 신규 1건 (`test_s5_report_modules_keep_module_top_cold`): report/* keeps torch/transformers/sentence_transformers/chromadb/bitsandbytes out of sys.modules (rendering is deps-free).
    - ✅ **S6** — MCP wrap + slug validation + integration tests (2026-05-28)
      - `storage/identifiers.py` — `sanitize_slug` (raise MCPError(INVALID_INPUT) on miss, with `suggested_slug` in hint), `validate_slug` (pure bool predicate), `suggest_slug` (NFKD-fold Latin diacritics → keep surviving ASCII → hyphenate runs → 64-char trim → hash-suffix fallback `event-{sha1[:8]}` for all-non-ASCII input). Deterministic across re-runs (same input → same slug → same Chroma collection).
      - `tools/build_event_tier_list.py` — **the real 5th MCP tool**. Wires source_capture → extraction → enrichment (with cache + resume) → fit retrieval → scoring (rationale only for S/A) → render_tier_list_md + dump_tier_list_yaml → write to `outputs/{ws}/{slug}_{YYYYMMDD}/`. All providers via module-reference imports (monkeypatch safe). Preflight runs with `require_product_context=True` so missing-ingest fails fast with `PRODUCT_CONTEXT_MISSING` instead of an opaque downstream error. `enrichment_enabled=False` synthesizes snippet-only rows (no Brave calls). `_load_cards_if_available` best-effort loads cards for rationale prompting; absence falls back to generic rationale (no failure).
      - `mcp_server.py::build_event_tier_list` — stub replaced with real handler delegation (lazy import inside the `@app.tool()` body).
      - `runtime/preflight.py` — **provider imports moved to module top** (module-reference, not symbol). Closes a class-identity drift trap: when cold-start tests purge `event_intel.*` and run_preflight does function-local `from event_intel.providers.embedding import BgeM3Provider`, Python re-imports a fresh module whose `BgeM3Provider` is *not* the test's monkeypatched FakeEmbedding. Module-top `_embedding`/`_llm`/`_search`/`_vectorstore` references survive purge because the test files hold the same module objects. `_validate_workspace_id_minimal` retained as a back-compat shim that delegates to `sanitize_slug`.
      - 테스트: **171/171 green** (S0/S1/S2/S3/S4/S5 115 + S6 56 신규)
        - `test_identifiers.py` 36건: validate_slug accept/reject matrix (alphanum / underscore / hyphen / 64 chars / >64 / spaces / `..` / `/` / Hangul / Latin diacritics) + suggest_slug (punctuation strip / lowercase / Latin diacritic fold / `/` → `-` / Korean preserves embedded ASCII / pure Korean → hash fallback / determinism / 64-char truncation / empty / consecutive separator collapse / leading-trailing strip) + sanitize_slug passthrough + raise+hint (Korean / `..` / empty / oversize / envelope round-trip via `to_envelope()`)
        - `test_mcp_tools.py` 10건: 4 input-validation cases (Korean event_slug → INVALID_INPUT + hint.suggested_slug + field="event_slug" / bad workspace_id / empty event_name / empty source_ref) + **PRODUCT_CONTEXT_MISSING via FakeVS product_chunks=0** / e2e full pipeline writes tier_list.md + tier_list.yaml + counts match / enrichment_enabled=False path (0 cache calls, "enrichment disabled" warning) / Korean lang e2e renders `# 샘플 박람회` + `최우선` headers / 5-tool surface check (all real handlers, no stubs) / SOURCE_CAPTURE_FAILED propagates at stage=extraction (not generic INTERNAL/preflight)
        - `test_mcp_error_taxonomy.py` 신규 3건: **10 ErrorCode × 6 Stage cartesian matrix** (60 unique pairs, envelope schema lock) / INVALID_INPUT hint carries `suggested_slug` + `field` + `rule` via sanitize_slug / per-field envelope contract (workspace_id, event_slug; path-traversal bytes don't leak into suggested_slug)
        - `test_cli.py` 6건: root --help lists all subcommands / models --help lists prepare+verify / export-schema writes valid JSON Schema / validate against sample_cards.yaml ok=true / draft-cards complains without --source-or-text / check-runtime always emits JSON envelope (ok or fail), exit code matches `ok`
        - `test_mcp_cold_start.py` 신규 2건: build_event_tier_list module + storage.identifiers stay import-cold (no torch/transformers/sentence_transformers/chromadb/bitsandbytes leak)
      - cold-start 회귀 가드 유지. Class-identity drift trap moved to documented module-top import pattern (lesson re-confirmed).
  - **Resumable batches**:
    - batch1 = S0 + S1 + S2 (~10.5h) ✅ — runtime preflight + product mini-RAG 살아있는 시점
    - batch2 = S3 + S4 (~11h) ✅ — event extraction + scoring 끝, 실 전시회 fixture 수집 직전
    - batch3 = S5 + S6 (~6h) ✅ — v0 surface 완성. 다음: 실 전시회 2-3개 fixture로 smoke + Claude Desktop 등록 검증
  - **Done When (14 결정적 기준)**: `pip install -e .` + pytest 75+ green / cold-start 회귀 0 / 5개 MCP tool Claude Desktop 호출 가능 / e2e (check-runtime → draft → validate → ingest → build) 한 사이클 성공 / 실 전시회 2-3개 다른 패턴으로 tier_list.md 생성 + 모든 S/A row의 `has_url + has_news >= 1` / tier_list.yaml round-trip / README에 5-tool workflow + 10 error_code 매핑 / bd-agent와 import 0 / envelope snapshot + schema drift test green / sanitize_slug edge case green / `build_event_tier_list` 가 product 미ingest 시 `PRODUCT_CONTEXT_MISSING` 반환 / Korean event_slug → `INVALID_INPUT` envelope의 `hint.suggested_slug` ASCII-safe 값 / `check_runtime`가 Brave quota 미노출 시 `remaining_quota: null` + status `ok` / `config/defaults.yaml` 필수 키 누락 → `CONFIG_ERROR` + path-localized hint

---


---

## 다음 진입 순서

1. ✅ Phase 18S (S0~S6) — v0 surface 완성 (173/173 green)
2. ✅ Phase 18T (T0~T3) — acquisition layer 완성 (290/290 green)
3. ✅ Phase 18T.1 — ChatGPT OAuth 설치 UX + 비동기 워밍업 + opt-in 시작 워밍업 (364/364 green)
   ✅ Phase 18T.2 — 무마찰 `.mcpb` 설치(repo_path 제거 + python_path 기본값 + .env 키 자동로드) + `.mcpb` 0.5.0 (371/371 green)
4. ✅ Phase 18T 마감 — `.mcpb` Claude Desktop 8 tools 노출 확인 (Done When #13, 2026-06-04)
5. **Phase 18U** (별도 plan 작성 필요) — Streamable HTTP + OAuth 2.1 PKCE + ChatGPT App 등록

세션 간 재개: `docs/status.md` + `~/.claude/plans/tender-mixing-badger.md` 먼저 읽기. 18T.1 plan: `~/.claude/plans/snoopy-weaving-robin.md`.
