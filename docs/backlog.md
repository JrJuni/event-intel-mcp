# Backlog

장기 계획, 큰 그림, v0 scope 외 항목. 진행 중/최근 완료는 [status.md](status.md).

`status.md`와의 분리 원칙: **status는 "지금 또는 직전"**, **backlog은 "아직 안 시작 또는 의도적 defer"**.

---

## 다음 큰 방향 — 로드맵 (2026-06-07)

전체 로드맵: `~/.claude/plans/snoopy-weaving-robin.md` (v3, blind review 2라운드 후 확정). 범용화 백로그(#12/#13)가
전부 종료된 시점에서, 큰 그림 남은 일은 두 갈래. **북스타: 실데이터 정확도 검증(Y1) → 원격 배포(Y2).** 각 phase는
착수 시 별도 상세 plan + blind review로 실행한다(이 섹션은 방향 박제일 뿐, 실행 계약 아님).

- **Y1 — 실데이터 검증 + 근거기반 정확도**: gold-label로 정확도가 검증된 적 없음(합성 fixture + 단일 gold-set
  튜닝뿐). `eval/harness.py`는 scorer만 실행(cards=None)하므로 실데이터 정확도 미검증. → **Y1A** 벤치마크 계약(제품 2~3개
  × 대표 이벤트 5~8개, 라벨 종류별 메트릭 적격성, holdout 공개 전 통과기준 고정) → **Y1B** instrumentation 선행(verdict별
  성공정의 + 최소 run-summary) → **Y1C** 3계층(live smoke / contract-replay CI(fake embedding) / offline quality
  benchmark(실 bge-m3, 비필수 CI)) → **Y1D** 측정 후 조건부 fix. 9-cell은 scoring 회귀로 유지·분리.
  - **⏸️ Y1D + holdout 보류 — 장기 과제로 강등 (2026-06-09, 사용자 결정)**: Y1A~C 측정 인프라(CS1–CS9) + 멀티벤더 라벨링(L0–L6)은
    완성·머지됐고 GTC DEV 1-pair 측정·진단까지 했으나, **적합한 gold 데이터(대표 pair 다수 + 정식 holdout pair)를 확보하지 못해**
    Y1D(rerank/retrieval) 통제 실험과 holdout 본측정을 **무기한 보류**한다. 정합성(`capability_fit`) 정확도 개선은 아래
    **#1 / #6**으로 박제(데이터 확보 시 재개). 근시일 작업은 **#14(인앱 셋업 패리티)**부터.
- **Y2 — 원격 배포 (계획까지)**: **Y2.0** 아키텍처 게이트(single-user-private 우선) → **Y2.1** Remote I/O + file-backed
  job(현 도구는 서버 로컬 경로 I/O라 원격 선결; DB persist는 OOS지만 job manifest는 허용) → **Y2.2** Streamable HTTP +
  표준 MCP 인증(resource-server / Protected Resource Metadata / audience 검증 / SDK·protocol 고정) → **Y2.3** 운영강화 + 비로컬 smoke.
  - **⏸️ Y2.2 잔여 — OAuth 인증 + 운영강화, 실배포 시점까지 defer (2026-06-10, 사용자 결정)**: Y2.2 a/b/c/d-1(공식 OpenAI provider lane · deploy-mode OAuth 게이팅 · loopback opt-in streamable-http · 원격 tool allowlist)은 완료·머지(#59~#62), mcp 버전 핀(`<2.0.0`)까지 선반영 → **remote-ready 토대 완성, 현재 노출 0**. 남은 **Y2.2d-2(OAuth 2.1 resource-server: token verifier·audience 검증·Protected Resource Metadata)** + **Y2.2e(비로컬 smoke·secrets store·rate-limit backoff·health·구조화 로깅)** 는 (a) 토큰 발급자 topology가 design fork이고 (b) 실키/실토큰 테스트가 크레딧 없이 불가하며 (c) 노출 전엔 불필요 → **실제 원격 배포 의향 시 재개**. 재개 선결: 발급자(외부 IdP vs self-issued) 확정 + MCP Authorization spec 정독. **d-2 전까지 public host 바인딩 금지.**
- **아래 #1~#10 매핑**: #1 양방향 retrieval·#6 rerank → Y1D · #3 backoff → Y2.3 · #10 multi-tenant → Y2.0(공유 시) ·
  #2 provider swap·#4 brief export·#5 resume·#7 bd-agent bridge·#8 batch·#9 auto-monitoring → 파킹.

---

## P1 — v0 진입 후 가장 먼저 검토

### ~~#14 인앱 셋업 패리티 — models prepare + ChatGPT 로그인을 CLI 아닌 앱에서 (P1)~~ ✅ 완료 (2026-06-09, PR #46/#47/#48)

**완료**: `prepare_models`(11th)·`login_chatgpt`(12th) MCP 도구 + 범용 비동기 매니저 `runtime/async_job.py`로 둘 다 start→poll 비동기화(앱 타임아웃 회피). `check_runtime`에 `setup` 블록(model_prep/chatgpt_login 상태) 부착. surface 12 tools. 검증된 `warmup.py`·`_pkce_login`·`login()`은 무수정 보존. 상세 `status.md` #14 + 메모리 [[inapp-setup-parity]]. **770 passed.** 아래는 완료 시점 설계 기록(참고용).

**배경**: 타깃 사용자는 비개발자인데, 첫 실행 셋업 2단계가 CLI-only라 진입 장벽 ([[inapp-setup-parity]] 메모리). north star("Claude Desktop 단일 surface")와 모순.

**사용자 패턴(2026-06-08 확정)**: **ChatGPT OAuth가 디폴트 온보딩이 될 것** — 무료로 OAuth로 체험 → 본격 사용 시 Anthropic API 키. 따라서 `login_chatgpt` 인앱은 *선택*이 아니라 **무료 체험 퍼널의 메인 경로**. (OAuth 사용자도 bge-m3는 LLM provider와 무관하게 ingest/build에 필요 → `models prepare`도 동일하게 필수.)

- **`prepare_models` MCP 도구** — bge-m3(~1.3GB) 최초 다운로드를 앱에서 트리거. 기존 비동기 워밍업 패턴(`runtime/warmup.py` 상태기계, 18T.1) 재사용: start→즉시 리턴(`downloading`)→`check_runtime`로 `downloading/ready/failed`+경과초 폴링. 동기 tool 금지(앱 타임아웃). 현재 `warm_up`은 받아둔 모델만 로드 → "없으면 다운로드" 분기만 추가.
- **`login_chatgpt` MCP 도구** — `webbrowser.open(auth_url)` + 백그라운드 localhost 콜백 리스너 → 즉시 리턴("브라우저에서 승인하세요") → 콜백이 토큰 교환·저장 → `check_runtime`가 logged-in 반영. 기존 `ChatGPTOAuthProvider.login()`(터미널 blocking)을 백그라운드 리스너로 전환. 데스크톱 앱 전제라 브라우저 open 가능.
- **유지(CLI로 OK, 사용자 동의)**: `eval-matrix`(측정), `export-schema`(dev 유틸) — 비개발자 경로 아님.

**진입**: Y1 후 별도 소규모 plan(plan→blind review→슬라이스 커밋). 새 아키텍처 아님(워밍업 패턴 확장).

### ~~#11 analyze_event_page prompt 튜닝 — Vue/React 감지 시에도 endpoint 패턴 우선~~ ✅ 완료 (2026-05-29, commit pending)

**증거**: Phase 18T Done When #4 smoke (2026-05-29) 중 tbse26.mapyourshow.com / directory.conexpoconagg.com 페이지가 본문에 `/ajax/remote-proxy.cfm?action=...` 엔드포인트 + `fetch(url, {X-Requested-With: XMLHttpRequest})` JS 코드 + `{{searchresults}}` placeholder + "No exhibitors could be found" fallback이 모두 명시되어 있음에도 analyzer가 `detected_framework=Vue` 기준으로 `operator_capture_required` (confidence 0.66~0.72) 권고하던 문제.

**적용한 수정**:
- `acquisition/analyzer.py`에 `_extract_endpoint_evidence(html, scripts)` 정규식 pre-scan 추가 — 7가지 패턴 (`/ajax/*.cfm`, `/api/*`, `remote-proxy`, `fetch(...)`, `$.ajax({url:...})`, `axios(...)`, `XMLHttpRequest.open(...)`) deduplicate + 길이 cap 240자 + 최대 20개
- `<DETECTED_PATTERNS>` 블록을 user_content에 추가하여 30 KB HTML truncation 안에서도 엔드포인트 신호가 LLM 시야에 남음
- `prompts/{en,ko}/analyze_event_page.txt`에 **PRIORITY RULE** 추가: "Endpoint evidence beats framework label" — DETECTED_PATTERNS에 XHR/fetch/ajax URL이 하나라도 있으면 framework가 Vue/React여도 verdict는 반드시 `xhr_endpoint`. `operator_capture_required`는 관찰 가능한 API 호출이 전혀 없을 때만 적용.

**검증 결과**:
- tbse26.mapyourshow.com: `verdict=xhr_endpoint` confidence 0.97 + 4개 candidate_endpoints (search + getsearchcategories + getcountries + getstates) ✅
- directory.conexpoconagg.com: `verdict=xhr_endpoint` confidence 0.98 + 5개 candidate_endpoints ✅
- 340/340 tests green (+8 신규: 7 regex pattern 검증 + 4 prompt construction 검증)
- 이전 verdict 분류 회귀 없음 (FakeLLM parametrize 4 verdicts 그대로 통과)

### ~~#12 Phase 18V — 범용 exhibition intelligence (MVP→일반화)~~ ✅ 완료 (2026-06-06, branch `phase-18v`)

**완료**: 4항목 전부 구현(eval matrix 2층 / news recency+UTC 정규화 / pool 분리+sim-gated penalty / CJK / evidence_types+floor 재설계 / target_mode). 상세 `status.md` Phase 18V. plan v3(blind review 2라운드 14건 수용). **+ Phase 18V.1 머지후 정제(2026-06-06)**: 정적 blind review 7건(P1×4/P2×3) 전부 수정(eval 실연결·evidence 관련성 게이트·예산/캐시/resume 결정론·capability top-N·카드 조기검증·티어별 floor invariant). + CI 게이트(pytest+ruff blocking, main 브랜치 보호). 상세 `status.md` Phase 18V.1. 잔여 → #13.

### #13 Phase 18W — 범용화 잔여 + 측정 확장 (P2)

18V/18V.1 이후 남은 일반화·측정 항목 (현재 blocking 아님):

- ~~**형태소 분석 라이브러리 (P2)**~~ ✅ 완료 (2026-06-07, Phase 18W P2-4 Step 1, plan `phase-18w-cjk-lib.md`) — Step 0 측정으로 bigram false-overlap 100%(4/4) 입증 후 Step 1 도입. pluggable `scoring.cjk_tokenizer.mode: bigram|morphological`, `scoring/cjk.py`가 호출당 1회 segmenter 결정(가나→janome/한글→bigram/순수Han→`han_default`) — needle·haystack 동일 segmenter로 대칭. lazy import(cold-start 가드 + `[cjk]` extra) + bigram fallback(warn) + 잘못된 config→CONFIG_ERROR. acceptance: JP/CN 적대적 오탐 3건 제거 + positives 무회귀. **KR은 순수 파이썬 형태소기 부재로 bigram 유지(오탐 1건 known-limitation, kiwipiepy는 네이티브 의존 → 별도 phase).** blind review 2라운드(v1→v3) 후 실행.
- ~~**9-셀 full eval matrix (P2)**~~ ✅ 완료 (2026-06-07, Phase 18W) — 제품(DB/부품/B2B) × 행사(AI/제조/일반) 9셀 fixture 전부 작성(`tests/fixtures/eval/*.yaml`). 9셀 모두 AUC 1.0 / competitor leakage 0 / evidence-FP 0 통과. harness가 `*.yaml` glob → 자동 게이트.
- ~~**ecosystem 셀 leakage 재정의 (P2)**~~ ✅ 완료 (2026-06-07, Phase 18W P2-3) — 모드 정책표 확정(competitor: customer만 negative, partner neutral, ecosystem positive; bad_fit: 전 모드 negative). `bad_fit_leakage_rate` 분리 신설(competitor와 별도 denominator). `ecosystem.bad_fit_penalty_factor 0.0→1.0`(B안). 직접 mode 테스트(BASELINE_CELL 재채점, 신규 fixture 0).
- ~~**캐시 TTL / resume 신선도 (P2, blind review r2 #2)**~~ ✅ 완료 (2026-06-07, Phase 18W P2-1) — `ENRICH_CACHE_VERSION 4`: 캐시 페이로드 `cached_at` 래핑 + TTL(`cache_ttl_days`/`resume_ttl_days` 7, 0=항상stale/None=무기한). resume row `input_fp`(name|url|snippet|confidence|config_fp) → 변경 시 재enrich. `config_fp`는 enrichment 필드만(scoring weight 격리). 진짜 `--refresh`(resume+cache 읽기 둘 다 우회).
- ~~**evidence 예산 round-robin (P2, blind review r2 #6)**~~ ✅ 완료 (2026-06-07, Phase 18W P2-2) — `allocate_round_robin` 순수 함수: event cap 설정 시 각 회사가 2번째 슬롯 전에 1번째를 먼저 받음(starvation 제거). cap=0(기본) 기존 동등. 회사별 즉시 resume.append 유지(내구성).
- ~~**generic 단일토큰 회사명 floor 오탐 (P3, r3 #3)**~~ ✅ 완료 (2026-06-07, Phase 18W P3) — `name_tokens` 임계 len>=3→**len>=2**: 짧은 distinctive 토큰("Xy Data"의 "xy")이 살아남아 앵커 역할 + "Data AI"가 all-generic(["data","ai"])이 되어 phrase 요구. **잔여(수용): 단일 generic 단어 회사명("Data")은 둘째 토큰이 없어 여전히 느슨** — 거부하면 정당한 "Data" 회사 recall 손실이라 본질적 모호로 수용.
- ~~**same_site allowlist 한계 (P3, r3 #5)**~~ ✅ 완료 (2026-06-07, Phase 18W P3) — `_TWO_LEVEL_SUFFIXES` 확장(myshopify/azurewebsites/substack 등 관리형 호스팅 + co.id/com.vn/ac.kr 등 ccTLD). **전체 PSL은 cold-start/패키징 비용으로 계속 defer**(목록 확장 = backlog가 명시한 보수적 경로).
- ~~**lint 추가 룰 (P3)**~~ ✅ 완료 (2026-06-07, Phase 18W P3) — ruff select += ANN + 자동수정 D(D208/D209/D413), ignore += ANN401, tests/** 제외. D 34건 자동수정 + ANN 29건 수동. **전체 docstring 커버리지(D101/102/103)는 churn 과다로 미채택.**
- ~~**KR 형태소 분석 (kiwipiepy) (P3)**~~ ✅ 완료 (2026-06-07, Phase 18X, plan `phase-18x-kr-morphological.md`) — kiwipiepy를 ko 백엔드로 도입, **별도 `[kr]` extra**(네이티브 휠 + ~109MB 모델, opt-in — `[cjk]` 순수 파이썬 유지). lazy `@lru_cache` + content-morpheme 필터(NNG/NNP/NNB/SL/SN/XR) + cold-start 가드 + bigram fallback(warn `.[kr]`). **Step 0 spike가 가정 정정**: bigram-윈도우 인공물 오탐 제거 + 헤드라인 동음이의 케이스(이차전지↔전지적)도 word-isolation으로 해결(kiwi가 고립된 `전지적`→`{지적}` 파싱). 기존 `cjk` CI job이 `[dev,cjk,kr]`로 KR acceptance 실행. **479 passed.**

**18V.1 round-2 정제 완료분(2026-06-06, 참고)**: HIGH 3건(#1 news 게이트+generic-token, #3 report invariant config화, #5 카드↔vector replace) + MEDIUM 2건(#7 멀티테넌트 same_site, #4 top-N/recency eval 실검증) 머지. 상세 `status.md`.

원래 항목(아카이브) — Phase 18U는 **MongoDB×GTC 단일 use-case 기준 MVP**로 합격(경쟁사 S/A=0, 타깃 median 5위 vs 경쟁사 25위). 모든 전시회·제품에 범용으로 쓰려면 별도 phase 필요 — blind review 2·3라운드에서 반복 도출, 사용자가 4항목 모두 중요 표시(2026-06-05):

- **evidence_types 확장 (P1)** — evidence floor를 news 외 `official_url`/`product_page`/`press_release`/`partner_page`/`docs`로 확장. 뉴스 적은 소규모·비상장·지역 타깃이 구조적으로 S/A 못 가는 문제 해소. evidence floor 재설계 동반.
- **target_mode 정책 (P1)** — `customer`/`partner`/`ecosystem` 모드로 경쟁사 처리 전환. 현재는 카드 `bad_fit`/`competitors` 선언이 정책 레이어(파트너 타깃이면 카드에서 빼면 됨)지만, "고객이자 경쟁사" 겹침 케이스용 명시 모드.
- **다중도메인 labeled eval matrix (P1)** — 제품(DB/부품/B2B서비스) × 행사(AI/제조/일반) labeled set 10~20곳 + 지표 Precision@10 / competitor leakage rate / target-vs-bad-fit median rank / evidence false-positive rate. 현 acceptance가 단일 gold set 과적합이라는 한계 보완.
- **news 관련성·최근성 + retrieval pool 분리 (P1/P2)** — 뉴스 회사 일치도·기사 유형 판별·발행일(published_at 이미 보존) recency 가중. capability retrieval과 competitor/bad_fit retrieval을 별도 풀로 분리(현재 단일 top-k에서 kind별 분류). CJK/약어 토크나이저(한·일 토큰화 + 영문 약어 whitelist 확장).

---

## P2 — v0.4+ 영역

### #1 양방향 fit retrieval (event ↔ product)
v0는 단방향 (event evidence → product collection). 정확도 검증 후 양방향(product → event도 query) 도입 검토. plan v0.5 Mini-RAG 섹션 참조.
**Y1D 측정 증거(2026-06-08, GTC×MongoDB measure-grade)**: `capability_fit`(단방향 RAG 코사인)이 전 라벨에서 **~0.5로 평평**(target 0.54 ≈ bad_fit 0.50) → target 양성 식별 실패. 양방향이 이 평탄함을 깰지 #6 rerank와 함께 후보.
**⏸️ 보류(2026-06-09)**: #6과 함께 **정합성 정확도 개선 장기 과제**로 강등 — 적합 gold 데이터 확보가 선결.
**🔓 선결 조건 변동(2026-06-11, ZNC G3)**: gold DEV pair가 **4종**으로 확대(GTC + Snowflake×BDLDN + 네이버클라우드×AI EXPO + Siemens×Hannover — 도메인·언어 다변, 각 20/20 gold + revenue tier). B2의 `news_relatedness`(본문 코사인)도 전 pair에서 0.43~0.48 평탄 — capability_fit 평탄과 동일 현상 재확인. **재개 여부는 사용자 결정**(재개 시 별도 phase plan·통제 실험·선빌드 금지 유지).

### #2 Provider 교체 구현
- **LLM**: Anthropic / ChatGPT OAuth / **OpenAI API**(Y2.2a 완료). embedding(bge-m3)·vectorstore(Chroma)는 단일.
- **Search ✅ 일부 완료 (2026-06-10, ZCS S1~S4, PR #65~#68)**: Brave 단일 → 플러그블 `search.provider: ddgs(키리스 zero-config 기본) | searxng | brave` + `make_search_provider` 팩토리. cache/resume provider-격리, throttle/backoff/degraded(ddgs), SearXNG json/403. blind review R1 8건 반영. 남은 것: **`.mcpb` provider 셀렉터**(기본 ddgs가 zero-config라 비차단 — 원격 셀렉터는 `EVENT_INTEL_SEARCH_PROVIDER` env→config 오버라이드 신설 필요 시).
- 후순위: Voyage embedding, Tavily search 등.

### #3 Rate-limit / backoff
Anthropic 대상 + **search ✅ ddgs는 ZCS S2에서 process-wide throttle + 지수 backoff + degraded 흡수**(`_RateLimiter`). v0는 per-call cache로 충분하지만 large-scale 사용 시 Anthropic/그 외도 본격 backoff 필요.
**ZNC R3 갱신(2026-06-11)**: 운영 전략은 `docs/retry-playbook.md`가 단일 출처 — 검색 lane 스로틀-우선 CONFIRMED(rate-limit 0/900), 본문 lane 패턴별 재시도 코드 반영(4xx 0회/transient 1회). 재시도 상한은 PROVISIONAL(R2 크론 누적으로 개정).

### #16 비용 최적화 6-레버 — ✅ ①④⑤ 완료 / ⏸️ ②③⑥ deferred (P1, phase plan `config-zero-mossy-toucan.md` v1 완료 — S0~S6, PR #105~#111)

**배경 (D3 실측 근거, p7 fullx로 확정)**: run당 **$5.06 Sonnet-4.6 환산 / $1.48 gpt-5.4-mini 환산**(p7 fullx, 2,885사 CSV·64청크) — extraction $4.35(출력 토큰 지배) + triage $0.61 + fit $0.09. 검색 ~150 쿼리/run(ddgs 키리스라 $0이나 쿼터·시간 비용). 실지출 $0(OAuth)이지만 유료 전환 시 그대로 청구되는 구조. 메모리 [[june-2026-structure-focus]].

**phase 결과 (skeleton+mock 검증만 — 라이브 런 0회, 비용은 투영치·전부 PROVISIONAL)**: CSV run **$5.06 → ~$0.24 투영(≈95%↓)** — ① extraction $4.35→$0(CSV 직변환) + ④ triage $0.61→~$0.20 / fit $0.09→~$0.03(Haiku) + ⑤ HTML 재실행 ~$0(extraction 캐시). 추가: **홈페이지 크롤 lane**(`enrichment.evidence_source: homepage` 출하)으로 검색 쿼리 **~150→~30/run**(CSV+url 보유 시 ~0). 상세 표는 status.md. **라이브 검증(CSV 휴리스틱 오탐률·Haiku 품질·캐시 hit률·homepage lane 품질)은 차후 phase.**

1. **① CSV 직변환 short-circuit — ✅ 완료 (#106)**: name-컬럼 휴리스틱으로 candidate 직생성 → CSV run extraction $4.35→$0 + head-truncation 구조 결함 동시 제거. 탐지 실패 → LLM fallback + `extraction.csv_short_circuit: false` escape hatch.
2. **② Anthropic prompt caching — ⏸️ deferred**: `chat_cached()`는 이미 구현(llm.py) — 유료 Anthropic 경로에서만 효과라 후순위.
3. **③ Batch API — ⏸️ deferred**: 비동기 폴링 구현 부담 큼.
4. **④ 스테이지별 모델 right-sizing — ✅ 완료 (#107)**: `llm.triage_model`/`fit_model` 기본 Haiku(PROVISIONAL, 품질 라이브 검증 차후), rationale은 Sonnet 유지. ledger `blended_cost_usd`(schema v2). oauth는 no-op + warning.
5. **⑤ LLM 결과 캐시 — ✅ 완료 (#108, extraction 한정)**: `sha256(version|model|lang|prompt_sha|chunk_sha)` 키 영속 캐시 — 재실행 재과금 제거, "75분 빌드가 chunk 39에서 전사" 근본 처방(retry-playbook §3.6). **triage/fit 캐시는 확장 후보**(범위 외 기록).
6. **⑥ Brave rescue rung — ⏸️ deferred**: 6월 Brave 쿼터 소진이라 라이브 검증 불가. **이번 phase의 홈페이지 크롤 lane이 쿼리 ~150→~30을 먼저 달성(S5 머지)** — Brave rescue의 필요 자체가 줄었을 수 있음, 라이브 데이터 확보 후 재평가.

### #17 triage lookalike-bias 처방 — ✅ ①+② 완료 (사용자 승인, 2026-06-13)

**D3 확정 진단(2026-06-11)**: p7 fullx(청크캡 64)가 추출 94.8%(2,735/2,885)를 달성하고도 gold 4사 전원이 scored 30에 못 들어감(P@10 0.0) — p5에서도 누락 gold 5사 전원이 "추출됐으나 triage drop". **triage의 "제품 도메인 관련도" 채점 축이 경쟁사/lookalike(같은 도메인 어휘)를 승격시키고 고객형(customer-type) 타깃을 밀어내는 구조 편향.**

**처방 (사용자 2-결정 잠금)**: ①+② 한 슬라이스 = triage 채점 축을 "제품 도메인 관련도" → **`target_mode`(customer|partner|ecosystem) 하의 타깃 적합도**로 재정의(프롬프트 en/ko 재작성 + `triage_roster(target_mode=...)` 주입, build site의 `resolved_target_mode` 흐름) + capability_digest에 고객 프로필(ideal-customer signals · buyer pains · bad-fit keywords) 보강. 경쟁사 처리 = **고객 recall 최대화: 컷 허용** — "경쟁사는 반드시 통과" 불변식 제거(competitor_penalty는 스코어링 단계에서 여전히 적용). ③ 2-pass는 미채택. **검증은 offline plumbing(프롬프트 내용·digest 필드·선택 로직·회귀 0)만**; **de-bias 효능(P@10 개선)은 offline 검증 불가 → PROVISIONAL, 라이브 1회 차후.**

### #15 기준 ⑤ 충족 — count_news 상향 (P1, 사용자 결정 대기)
**ZNC G4 정량 결론(2026-06-11)**: 대기업 본문뉴스 ≥10건 달성률 0~20% (3 pair measure, advisory `news_capture`). `count_news` 12로는 게이트·중복제거·본문 fetch(73~78%) 통과 후 5~9건 대역. **처방 후보**: ① `count_news` 12→20+(검색 예산 ~1.7배) ② rescue식 보조 뉴스 쿼리 ③ 기준 완화. 검색 예산 trade-off라 **사용자 결정 필요**; 결정 시 1슬라이스(config+재측정).

### #4 운영자 brief export 자동화
v0에 `product_brief.md`를 capability cards에서 자동 생성하는 export view는 있음 (S5 `brief_export.py`). v0.4+에서 PDF / Notion / Slack post export로 확장.

### #5 Resume granularity 강화
v0는 per-row resume (enrichment 실패 row만 재시도). v0.4+에서 stage 단위 + per-call cache 결합으로 더 세밀한 재개.

---

## P3 — v0.5+ 영역

### #6 Cross-encoder rerank — 정합성 정확도 개선 (장기 과제, 데이터 블로커)
bge-m3 only로 시작. 정확도 부족 검증 시 reranker 도입.
**Y1D 진단이 지목한 핵심 fix 후보(2026-06-08)**: GTC×MongoDB measure-grade에서 `capability_fit` RAG 코사인이 target/non-target을 못 가름(전 라벨 ~0.5). 가중치 재조정은 무의미(평평한 신호). top-K를 exhibitor↔product fit로 cross-encoder/LLM 재랭킹 → target 양성 식별 직접 개선.
**⏸️ 보류(2026-06-09, 사용자 결정)**: **적합한 gold 데이터 미확보**로 장기 과제 강등. 재개 조건 = 대표 pair 다수 + 정식 holdout pair 확보. 재개 시 **별도 phase plan으로 통제 실험(rerank→DEV 재measure + 9-cell 회귀), 선빌드 금지.** (#1 양방향 retrieval과 함께 검토.)

### #7 bd-agent bridge
event-intel-mcp 결과를 bd-coldcall-agent의 `Targets` 테이블로 export. 별도 phase로 분리.

### #8 Multi-event batch
v0는 1 event/호출. 동시 다수 전시회 처리는 별도 surface 필요 (cron 자동화와 연동).

### #9 Auto-monitoring
전시회 페이지 cron 폴링 + diff 알림. v1.0+ scope.

### #10 Multi-tenant SaaS
현재 `workspace_id`는 single-machine. 진짜 멀티유저 운영은 별도 product로 분리 권고.

---

## 의도적 OOS (재검토 안 함)

- **bd-agent 내부 통합**. 정합성/lifecycle 분리 우선. bridge는 P3로.
- **JS rendering / browser automation / login wall**. operator-assisted capture(브라우저에서 직접 저장한 HTML / CSV)로 충분히 cover.
- **Notion / Web UI / DB persist**. v0는 artifact-only. UI surface는 별도 product 결정.
- **bilingual auto-detect**. `--lang` 명시 강제. en / ko 둘 다 v0 지원.

---

## 신규 백로그 항목 추가 규칙

- P1: 다음 phase 진입 결정에 영향. status로 곧 promote 가능
- P2: 다음 minor version (v0.4)에 검토. 정확도/UX 영향 큼
- P3: 차차 검토. 별도 phase 가능

새 항목 추가 시 `#N` 번호 + 한 줄 요약 + 배경 1-2 문장. 우선순위 변동 시 P 레벨 update.
