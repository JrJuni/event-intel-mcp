# Status

프로젝트 진행 상황과 장단기 계획의 단일 원천. **현재 진행 중 / 최근 완료**만 여기에 둔다. 장기 계획·아직 안 시작한 작업은 [backlog.md](backlog.md).

---

## 진행 중

- **Evidence-first triage 재설계 ✅ 전체 완료 (E1~E4, 2026-06-13, PR #115/#116/#117/E4)**
  - **계기.** homepage smoke P@10=0.0(메모 `p7-homepage-smoke-result`) 진단: triage가 **이름만** 보고 채점 → 실타깃과 산업적-이름 lookalike를 구분 불가. 처방 = **증거 우선 2-tier 적응 루프** + 2-시그널 출력(fit_score vs evidence_status).
  - ✅ **E1 profile_fetch** (PR #115) — `events/profile_fetch.py`: triage 전 풀 로스터의 detail-page 본문을 `candidate.profile_text`로 적재(HTTP+캐시+trafilatura, **검색·LLM 0콜**). Tier-1 증거.
  - ✅ **E2 triage 재작성** (PR #116) — 2-시그널: 각 사 → 수치 fit(0.0-1.0, KNOWN) **또는** `"unknown"`(증거 없음 = 라우팅 STATE, 낮은 점수 아님). 3-band 선택(사용자 승인): KNOWN_FIT(≥cutoff) > UNKNOWN(로스터순) > KNOWN_NOFIT. all-UNKNOWN = 기존 first-N과 동일 degrade. 프롬프트 en/ko "이름은 증거 아님" 재작성.
  - ✅ **E3 Tier-2 적응 검색** (이 브랜치) — `events/tier2.py` `resolve_unknowns`: 슬롯 경합 중인 UNKNOWN을 **회사당 웹검색 1회 → profile_text → 재-triage**로 KNOWN 전환. **게이트**: Tier-1 KNOWN_FIT ≥ cap이면 전체 skip(증거 충분 = $0). **로스터 자동 게이팅**(사용자 선택): 소형(≤`small_roster_threshold` 300) 전수 브루트포스 / 대형은 `max_searches_per_event`(50) 상한 + 미검색분 로깅. **적응 조기종료**: cap 충족 시 중단. ddgs degrade 3연속 → abort. 상수 전부 PROVISIONAL·config 구동. triage 내부를 `score_indexed`/`select_by_band`로 추출(부분 재채점 공유). offline 16-케이스 + build 배선 1-케이스, 전체 pytest green.
  - ✅ **E4 cost ledger 검색 카운트** (이 브랜치) — `llm_ledger`에 **search_usage**(schema v2→v3) 신설: `record_search(lane, count, degraded)` → 레인별 웹검색 쿼리/degraded 카운트. **토큰과 별개 spend 축**(ddgs는 키리스지만 쿼터 제한, Brave는 쿼리당 과금)을 run_summary에 노출 — 그간 보이지 않던 Tier-2의 최대 `max_searches_per_event` ddgs 쿼리가 이제 감사 가능(no silent cost). Tier-2가 회사당 검색 1건씩 ledger에 기록(None/legacy ledger 안전, 절대 raise 안 함). USD 환산은 아직 없음(ddgs 현금 $0, Brave 단가 라인은 차후). enrichment 레인 카운트는 follow-up(backlog #16).
  - **투영 Tier-2 비용(PROVISIONAL, 라이브 검증 차후)**: UNKNOWN이 슬롯 경합 시에만 발화 — 소형 로스터 전수(≤300사), 대형 `max_searches_per_event` 50 상한. worst-case run당 **≤50 ddgs 쿼리(현금 $0) + ≤~5 Haiku 재-triage 콜**. Tier-1 KNOWN_FIT가 cap 충족 시 $0(skip-gate).

- **R2 캠페인 취소 + homepage = 확정 evidence 방향 (2026-06-13 사용자 결정)**
  - **R2 취소.** zero-config 뉴스-스모크 캠페인(`EventIntelR2Smoke` 크론, 누적 1/10)을 **전면 취소** — 크론 삭제, retry-playbook 재집계 트리거 해제. 뉴스 검색 retry-stats 수집이 목적이었으나 evidence lane을 homepage로 트면서 목적이 소멸.
  - **homepage 확정.** `evidence_source: homepage`는 이미 출하 default(S4/S5, #16). 이제 **실험이 아니라 확정 방향**으로 격상 — 뉴스 레인(count_news·news_body·supplement·RSS 폴백·query_rescue)은 `evidence_source: news` 한 줄로 되돌리는 **escape-hatch fallback**으로만 유지(삭제 아님).
  - **다음.** R2(뉴스)를 대체하는 **homepage smoke 1회**(사용자 지시 "한번 해보자")로 homepage lane을 라이브 실증 — 그간 PROVISIONAL이던 homepage lane 품질 / Haiku 품질 / 캐시 hit률 / CSV 휴리스틱 / #17 de-bias P@10을 한 런에서 동시 확인. 타깃은 **holdout(MongoDB×AI Expo Tokyo) 제외**, 비-블라인드 gold pair에서 선정. ← 진행 예정.

- **#17 triage lookalike-bias 처방 ✅ ①+② 슬라이스 완료 (2026-06-13, 사용자 2-결정 잠금, branch `triage-target-fit-debias`)**
  - **계기.** D3 확정 진단: 추출 94.8% 달성에도 P@10=0 — triage의 "제품 도메인 관련도" 채점 축이 경쟁사/lookalike(같은 도메인 어휘)를 승격시키고 고객형 타깃을 top-K 밖으로 밀어내는 구조 편향.
  - **처방(① + ② 한 슬라이스).** triage 채점 축을 **`target_mode`(customer|partner|ecosystem) 하의 타깃 적합도**로 재정의 — 프롬프트 en/ko 재작성 + `triage_roster(target_mode=…)` 주입(build site의 `resolved_target_mode` 흐름). capability_digest에 **고객 프로필**(ideal-customer signals · buyer pains · bad-fit keywords) 보강. 경쟁사 처리 = **고객 recall 최대화: 컷 허용** — "경쟁사 반드시 통과" 불변식 제거(competitor_penalty는 스코어링에서 여전히 적용). ③ 2-pass 미채택.
  - **검증.** offline plumbing만(프롬프트 내용·digest 필드·선택 로직·회귀 0) — ruff + 전체 pytest green. **de-bias 효능(P@10 개선)은 offline 검증 불가 → PROVISIONAL, 라이브 1회 차후.** 라이브 스모크 0회(사용자 지시 유지).

- **비용 최적화(#16 ①④⑤) + 홈페이지 크롤 증거 lane ✅ 전체 완료 (2026-06-11 착수, plan `config-zero-mossy-toucan.md` v1 [완료·아카이브] — S0~S6, PR #105(S0 동승)·#106~#111)**
  - **계기.** D3 실측 비용 **$5.06/run**(Sonnet-4.6 환산, p7 fullx 2,885사)이 예상보다 높아 #16 즉시 착수. **검증은 전부 skeleton + mock(offline pytest) — 라이브 스모크/벤치마크 런 0회**(사용자 지시). 비용 효과는 실측 baseline에서의 **투영치만** 보고(전부 PROVISIONAL). 추가로 사용자 선택 "뉴스 대체 실험": activity 증거를 뉴스 검색 대신 회사 자체 /press·/news 페이지에서.
  - ✅ **S0** (#105 동승) — D3 문서 마감(잔여 라이브런 p6 재실행·p1 mini 취소 명기).
  - ✅ **S1 ① CSV 직변환** (#106) — CSV 소스는 name-컬럼 휴리스틱(en/ko 헤더 우선순위 리스트)으로 candidate 직생성 → **extraction LLM 0콜·$0**. 탐지 실패 → 기존 LLM 경로 fallback(warning) + `extraction.csv_short_circuit: false` escape hatch. CSV head-truncation 구조 결함(p7의 청크캡 탈락)도 동시 제거.
  - ✅ **S2 ④ right-sizing** (#107) — `llm.triage_model` / `llm.fit_model`(기본 Haiku, **품질 PROVISIONAL — 라이브 검증 차후**), rationale은 Sonnet 유지. ledger에 stage별 실제 모델 단가 환산 `blended_cost_usd`(schema v2; 기존 `reference_costs_usd` 연속성 유지). 키 부재 시 기존 동작, oauth provider는 no-op + warning.
  - ✅ **S3 ⑤ LLM extraction 캐시** (#108) — `events/llm_cache.py`: per-chunk 응답 디스크 캐시, 키 `sha256(version|model|lang|prompt_sha|chunk_sha)`(prompt **불변부만** hash — chunk 카운터 등 휘발 토큰 제외, 재실행·재청킹에서 hit 보존). hit = LLM 0콜 + `calls_cached`(토큰 미합산). "75분 빌드가 chunk 39에서 전사"(retry-playbook §3.6) 손실의 근본 처방.
  - ✅ **S4 홈페이지 fetcher** (#109) — `events/homepage_evidence.py` `HomepageCrawler`(robots 게이트 → byte cap → trafilatura, transient 미캐시 + 1회 재시도, **절대 raise 안 함** — 실패는 identity-only degrade) + **`press_page` evidence type 신설**: same-registrable-domain의 /press·/news·/newsroom·/media 서브페이지 = activity 증거(third-party는 방어적 False, type 직접 지정 — `classify_url_type` 미경유). **floor 임계(2→S/A, 1→A, 0→B) 불변**(매핑 변경은 사용자 승인 범위).
  - ✅ **S5 enrichment 배선** (#110) — `enrichment.evidence_source: homepage`(defaults 출하; **코드 기본값은 news** — 키 부재 시 byte-동일, config 한 줄로 가역). homepage 모드: news 검색·rescue news 재시도·evidence ×3 쿼리·news_body lane 전부 skip → **per-company 검색 쿼리 ≤1(URL 보유 시 0)**. `homepage_excerpt`가 llm_fit/rationale 입력(뉴스 타이틀 대역, 사용자 승인 입력 변경). `ENRICH_CACHE_VERSION` 7→8 + fingerprint lane 키(news↔homepage 교차 재사용 차단).
  - ✅ **S6** (#111) — 문서 마감 + 투영 비용표(아래). backlog #16 ①④⑤ ✅ / ②③⑥ ⏸️, architecture.md evidence 절 갱신, patchnotes.
  - **투영 비용표 (p7 fullx 실측 $5.06 기준 — 전부 PROVISIONAL, 라이브 검증 차후 phase)**:
    | Stage | Baseline | +① CSV | +④ right-size | +⑤ 캐시(재실행) |
    |---|---|---|---|---|
    | extraction | $4.35 | **$0**(CSV) / $4.35(HTML) | 동일 | HTML 재실행 ~$0 |
    | triage | $0.61 | $0.61 | **~$0.20** | 동일 |
    | fit | $0.09 | $0.09 | **~$0.03** | 동일 |
    | rationale 등 | ~$0.01 | ~$0.01 | ~$0.01 | 동일 |
    | **합계(CSV)** | **$5.06** | $0.71 | **~$0.24** (≈95%↓) | 재실행 ~$0.24 |
    | 검색 쿼리/run | ~150 | ~150 | ~150 | homepage lane: **~30**(CSV+url ~0) |
  - **남은 것(차후 phase)**: ②prompt-caching ③Batch ⑥Brave rescue deferred(backlog #16) · homepage lane 품질 / Haiku 품질 / 캐시 hit률 / CSV 휴리스틱 오탐의 **라이브 검증** → **homepage smoke로 흡수**(아래 R2 취소 항목) · buying_signal의 press_page 추출 여부(open).

- **Y1D — capability_fit LLM-first 재설계 (D0~D3) ✅ 전체 완료 (2026-06-11, plan `config-zero-mossy-toucan.md` v3 [완료·아카이브], D0~D2 머지 + D3 branch `y1d-d3-measure`)**
  - **계기.** capability_fit(가중 0.30)이 cosine에서 전 라벨 ~0.5 평탄(양성 식별 실패) + 대형 로스터(p7 2,885사)는 head-truncation으로 gold 전원 추출 전 탈락. 사용자 결정: cosine→**LLM 명시 판단을 기본값으로**(cosine은 폴백/escape hatch), 대형 로스터는 **LLM 트리아지 깔때기**(벤치 hack 불가), 단 **비용 계측 선행**.
  - ✅ **D0** — `runtime/llm_ledger.py`: 전 스테이지 LLM usage 수집 → run_summary `llm_usage`(stage별 calls/tokens/models) + **기준모델 양측 환산**(`reference_costs_usd`: Sonnet 4.6 / gpt-5.4-mini). `benchmark cost` CLI. 후속 fix: ledger가 config 문자열이 아닌 **실제 응답 모델** 기록(`46eabab`).
  - ✅ **D1** — `scoring/llm_fit.py` + `capability_fit_mode: llm` 기본값(실패 시 회사 단위 cosine 자동 폴백 + warning). 9-cell 무영향(fixture 하드코딩), cosine 핀 회귀 green.
  - ✅ **D2** — `events/triage.py` LLM 배치 트리아지(≤cap 무호출, 초과 시 배치 채점→top-K). 전 배치 실패 → 선두-N 폴백.
  - ✅ **D3 실측 (gold 4 pair + 통제 변형)** — measure 파일 커밋(`benchmarks/gold/*/measure_llm*.json`):
    | pair | P@10 | tgt_extraction_cov | 비고 |
    |---|---|---|---|
    | p1 GTC×MongoDB | **0.40** | 7/7 | cosine 0.40과 동률 (소형 로스터) |
    | p5 BDLDN×Snowflake | 0.30 | 5/10 | 누락 5사 전원 **추출은 됐으나 triage가 drop** |
    | p6 AI EXPO×네이버클라우드 | 0.20 | 4/8 | **CONTAMINATED** — Brave 쿼터 사망 mid-run |
    | p7 Hannover×Siemens (default) | 0.00 | 0/4 | 청크캡 12/43 head-truncation (예측 적중) |
    | p7 **fullx** (캡 64 통제변형) | 0.00 | 0/4 | **추출 94.8%(2,735/2,885) 달성에도 0/4 — 추출 무죄, triage 병목 확정** |
  - **진단 — triage lookalike-bias**: triage의 "제품 도메인 관련도" 축이 경쟁사/lookalike를 승격시키고 고객형 타깃을 밀어냄(p5 진단과 p7 mega-roster에서 동일 패턴). **처방은 스코어링/프롬프트 변경이라 사용자 결정 대기**(backlog #17).
  - **비용 실측 (p7 fullx, 97 calls / 238k in / 290k out)**: **$5.06 Sonnet-4.6 환산 / $1.48 gpt-5.4-mini 환산** — extraction $4.35(출력 토큰 지배) + triage $0.61 + fit $0.09. 실지출 $0(ChatGPT OAuth). 검색 ~150 쿼리/run. → **backlog #16 비용 최적화의 실측 근거.**
  - **운영 사건**: R2 크론이 fullx outputs를 덮어씀 → run_result.json 내장 run_summary로 측정 구제(--tier-list는 advisory 전용이라 생략 가능). 부수 fix: 청크당 transient 1회 재시도(`9387389` — 75분 빌드가 단발 LLM 오류로 전사하던 것).
  - **취소(2026-06-11 사용자 지시 "스모크 임의 실행 금지")**: p6 ddgs 재실행 + p1 mini 라이브런 — fullx가 병목을 이미 확정해 정보 가치 낮음.

- **ZNC — Zero-config 뉴스 수집(본문 크롤링 포함) 신뢰성 + entity 게이트 + 경험적 retry policy + gold pair 3종 ✅ 전체 완료 (2026-06-11, plan `~/.claude/plans/config-zero-mossy-toucan.md` [완료·아카이브] — PR #75~#93, 단 하루에 19 PR. R2 크론은 2026-06-13 취소)**
  - **계기.** 사용자 최우선: **config zero — 배포 패키지만으로(키 0) 뉴스 서치·크롤링 동작** + "너무 빨리 포기하지 않게". 탐색이 실버그 확인: ① ddgs 결과-0건이 `DDGSException`으로 raise → **enrichment 스테이지 전체 abort**, ② 기존 retry가 잡던 `RatelimitException`은 ddgs 9.14가 **실제로 raise 안 하는 죽은 코드**, ③ rate-limit 빈 결과가 캐시/resume에 7일 고착. **성공 기준 5(사용자 계약)**: ①본문 크롤링 ②제품/솔루션 정보 포함 ③RAG 연관도 산출 가능(이번 phase는 report-only) ④중복 없음 ⑤매출 ≥$10M→뉴스 ≥10건/미만→≥3건(revenue tier는 G-lane 라벨링에서). retry 상수는 **경험적**(실패 계측→스모크 ≥10회→R3 명문화, 최대 ~5회 + 사이트형태 playbook).
  - ✅ **N1** (#75) — degraded 결과 미고착: per-call `last_call_degraded`, degraded는 캐시 미기록 + resume row `degraded`(재사용 금지→다음 실행 재시도). `ENRICH_CACHE_VERSION` 6. → playbook #15.
  - ✅ **N2** (#76) — 예외 분류 fix(genuine-empty=`[]`·캐시 / 그 외=재시도→degrade, 메시지 리터럴 핀 테스트) + `search.ddgs_backend: auto`(fresh DDGS/attempt=엔진 회전) + no-stage-abort(MCPError만 fatal) + max_retries 5(잠정—R3 확정). → lesson 2026-06-11.
  - ✅ **R1** (#77) — 실패 패턴 계측: `runtime/failure_log.py` + provider 이벤트(`drain_events`) → `~/.event-intel/diagnostics/{ws}/*.jsonl` + `benchmark retry-stats`(회복률 vs 시도 횟수, 익명 집계, silver).
  - ✅ **B1** (#78) — 뉴스 **본문** fetch lane: `events/news_body.py` — robots 게이트(acquisition 헬퍼)→스트리밍 byte cap→trafilatura→min-chars. 실패는 per-item degrade(빌드 무중단)+`fetch_failures` 기록, transient 미캐시(N1 원칙). `NewsSignal.body_sha/chars`. count_news 5→12.
  - ✅ **B2** (#79) — 본문 content 게이트(wrong-entity 본문=floor 비기여) + 근사중복 제거(8-word shingle Jaccard≥0.7, 통신사 전재) + **본문↔제품 RAG 연관도**(빌드 7c, report-only — `news_relatedness`, REPORT_SCHEMA v5; scoring 필드 불변 단언, W4 패턴).
  - ✅ **N3** (#80) — **Google News RSS 키리스 폴백**: news 전용, primary degraded일 때만 발화(진짜 빈결과는 폴백 안 탐), `FallbackSearchProvider` 합성 시그니처로 캐시 격리. ddgs만 기본 래핑(`search.news_fallback`).
  - ✅ **C1** (#81) — entity 게이트 predicate(`name_is_ambiguous`/`context_terms`/`is_relevant_news`) + `events/data/common_words.txt`(Norvig top-50k — chroma ~41k라 50k 컷, plan open item 확정). 행동변경 0.
  - ✅ **C2** (#82) — 게이트를 floor evidence + B2 본문 게이트 + `score_buying_signal`에 연결(`enrichment.news_entity_gate.enabled`, 기본 on). **점수/티어 의도적 변경(승인 범위)** — Dust/Ramp/Chroma류 거짓양성 차단. 9-cell green(변별적 이름 무변화 — plan 예측 적중).
  - ✅ **N4** (#83) — LLM query-rescue rung(최후 수단): official 0+news 0+**degraded**인 회사만(진짜 부재 미발동), LLM이 **쿼리만 제안**(현지어/법인격/산업 변형, `prompts/{en,ko}/query_rescue.txt`) → 동일 결정론 lane 재실행(N1·N3·B1·C2 전부 적용). 회수 시 resume 최종화(run2 LLM 0콜). 캡 5사×3쿼리.
  - ✅ **G1** — gold pair 3종 **전부 키리스 캡처** (operator-capture 0 — 사용자 north star, 메모리 `no-operator-capture-flow`): Big Data LDN(공개 exhibitors-sitemap, 179사) / AI EXPO KOREA(정적 페이지+EUC-KR 교정, 197사) / Hannover Messe(A–Z 인덱스 크롤 2,885사 — **ladder 챌린지 휴리스틱이 로그인 오버레이 `:has-captcha` 속성에 오탐**한 것이지 실제 봇월 아님). holdout 비접촉.
  - ✅ **버그픽스 3건 (라이브가 잡음)**: #86 테스트 홈 격리(pytest fake가 실홈 진단 오염 — autouse `Path.home()` 격리) / #87 **meta-charset 스니핑**(`textenc.py` — EUC-KR 페이지가 utf-8 기본값으로 mojibake, 3개 디코드 지점 교정) / #88 **entity 게이트 self-token fix**(snippet의 회사명 토큰이 ctx를 자명 통과시켜 게이트 no-op — 이름 토큰 제외 + 다중토큰 풀네임 phrase-strength 규칙, `ENRICH_CACHE_VERSION` 7).
  - ✅ **CL 실증** — Pinecone×AIEWF 재빌드: **Dust A 6.41→C 3.75, Chroma B 4.17→C 3.27, floor 뉴스 0건**(동음이의 기사 전부 차단). 키 0(ddgs+RSS+OAuth)으로 금일 전 빌드 완주 — 성공 기준 ①(본문 73~78%) ②(게이트 활성) ③(연관도 산출, report-only) ④(dedup 활성) 충족.
  - ✅ **G2** (#89) — 카드 3종(Snowflake/Siemens DI/네이버클라우드) draft→스키마 교정→ingest(26/29/25 chunks). R2 pairs를 본 게임 조합으로 전환.
  - ✅ **G3** (#90/#91/#92) — **3 pair 전부 20/20 gold** (L0–L6 멀티벤더: GPT 초안→Claude 독립(SHA 증명)→웹 refine→seal). 분포 T/C/B/N: p5=10/3/0/7, p6=8/1/1/10, p7=4/2/2/12. 합의율 0.5/0.5/0.7. 대표 판정: Domo 양벤더 오판→파트너 증거로 target, self-부스→neutral, T3Q=competitor(업스테이지 리셀러), Aras=PLM competitor. **revenue_tiers.json 신설**(기준 ⑤ 분모). 택소노미 규율: bad_fit=카드 사유 매칭만, 대학·재단=neutral.
  - ✅ **G4** (#91/#92) — `eval/news_capture.py` **advisory** 메트릭(freeze 규율상 게이트 승격은 차기 freeze) + measure 3 pair: **P@10 0.5/0.4/0.0**(p7 0.0은 2,885사 head-truncation 샘플링 왜곡 — 대형 roster 샘플링 전략 필요 교훈), eligibility fail(coverage, DEV 예상). **기준 ⑤ 정량 결론: 대기업 본문뉴스 ≥10건 달성률 0~20% → count_news 12→20+ 상향 필요(사용자 결정 대기)**.
  - ✅ **R2 인프라+캠페인** (#85) — `benchmarks/r2_pairs.yaml` + `scripts/r2_smoke.py` + Task Scheduler `EventIntelR2Smoke`(4h 간격, 사용자 지정). 첫 배치 3 pair 완주. **❌ 캠페인 취소(2026-06-13 사용자 결정)** — 누적 1/10에서 중단, 크론 `EventIntelR2Smoke` 삭제. 뉴스-retry-stats 수집이 목적이었으나 evidence lane이 homepage로 확정되며 목적 소멸. retry-playbook 재집계 트리거도 함께 해제. **homepage smoke(1회, 사용자 지시)로 대체** — 위 진행 중 항목 참조.
  - ✅ **R3** (#93) — **1,141 events 기반 retry policy 명문화** + `docs/retry-playbook.md` 신설(8번째 standing doc): 검색 lane rate-limit 0/900(스로틀-우선 CONFIRMED, 재시도 상한은 PROVISIONAL 정직 표기) / 본문 lane 403=16/16 결정적(재시도 0회) vs 429·5xx·transport(1회 재시도) **코드 반영**.
  - **결과**: 키 0으로 뉴스 서치+본문 크롤링 전 체인 동작·실증. 실패는 ①스테이지를 못 죽이고 ②캐시/resume에 고착 안 되고 ③폴백·본문·rescue가 차례로 발화하고 ④degraded로 가시화되고 ⑤retry 상수는 데이터로 명문화. gold DEV pair 4종(GTC+신규 3) 확보 → **Y1D 재개 조건 충족 여부 = 사용자 결정**(backlog #1/#6). 테스트 +~150, 전 슬라이스 CI green.

- **BD Critique Harness — 호스트-판정 다중렌즈 진단 ✅ 전체 완료·아카이브 (2026-06-10~11, plan `snoopy-weaving-robin.md` — S1~S4 #69~#72 + 후속 #73/#74, 라이브 e2e 검증)**
  - **계기.** 정확도가 독립 gold 대비 검증된 적 없음(진짜 빈틈), 정식 gold/holdout은 데이터 블로커로 보류. 그 사이 **"엔진이 BD적으로 말 되나"를 싸게·대량 진단**. **핵심 제약**: silver-grade DEV 자문(엔진↔판정자 불일치 = 사람 spot-check 후보), gold 인증 아님(순환성). scoring **무변경**(직교 레인). ddgs+OAuth로 **크레딧 0**.
  - ✅ **S1** (#69) — `eval/smoke_batch.py` + `benchmark smoke-batch`: N pair 배치 빌드(부분실패 격리) → tier_list+run_summary 수집.
  - ✅ **S2** (#70) — `eval/critique_packet.py`: tier_list → S/A 픽 비평 패킷(+packet_sha 결속) + 비평 JSON 스키마(dataclass 검증, blind-first+다중렌즈).
  - ✅ **S3** (#71) — `prompts/{en,ko}/critique_panel.txt` + `eval/critique_panel.py`(brief 렌더) + `benchmark critique-brief`.
  - ✅ **S4** (#72) — `eval/critique_report.py` + `benchmark critique-stats`: 비평 집계 대시보드(pair별 defensibility + 사람 triage 후보). silver 명시. **후속**: ragged CSV crash fix(#73) + 점수 기반 critique selection(고점 non-S/A 거짓양성 포착, #74).
  - **결과**: 크레딧 0으로 N pair 스모크 → 호스트 다중렌즈 비평 → triage 대시보드, 라이브 e2e 완주. **라이브 비평이 Pinecone×AIEWF의 Dust/Ramp/Chroma 동음이의 거짓양성을 적발 → ZNC entity 게이트(C1/C2)로 직결.** 점수 경로 불변.

- **Zero-config 검색 provider — Brave → 플러그블(DDGS 기본) ✅ 완료 (2026-06-10, plan `~/.claude/plans/zero-config-search-provider.md` v2 — blind review R1 8건 전부 반영. S1~S4 머지 #65~#68)**
  - **계기.** enrichment의 web/news 검색이 Brave API 키 필수라 비개발자 zero-config에 걸림돌. backlog #2(provider swap)로 **키리스 기본(DDGS)** + 플러그블(SearXNG·Brave 선택). **scoring/evidence floor 무변경** — 검색 소스만 교체. (사용자 결정: 플러그블 다중·DDGS 기본 / GitHub·Reddit 범위 밖.)
  - **설계.** Codex blind review R1(8건 전부 HEAD 대조 후 수용): cache/resume provider-aware(같은 query라도 백엔드 다르면 결과 다름) · graceful empty는 rate-limit 한정 + degraded 가시화 · DDGS lang→region 매핑 · throttle process-wide thread-safe · SearXNG json/403 · ping best_effort · provider-neutral config 키.
  - ✅ **S1** (#65) — `make_search_provider` 팩토리 + `search.provider` config + preflight provider-게이팅 + **[R1#1] cache/resume provider 무효화**(`cache_signature` + `ENRICH_CACHE_VERSION` 5) + **[R1#6] check_runtime search 블록**(active provider·status·warnings, live query 없음). 기본 brave 유지(무행동변경).
  - ✅ **S2** (#66) — `DdgsSearchProvider`(키리스, 기본 flip). lang→region · days→timelimit · **[R1#4] module-level thread-safe throttle** · **[R1#2] backoff→rate-limit 한정 graceful empty + degraded run-warning** · ping best_effort. `ddgs>=9.14,<10` 핀.
  - ✅ **S3** (#67) — `SearxngSearchProvider`(self-hosted JSON lane). **[R1#5] 403/비-JSON → ping missing_config(CONFIG_ERROR+fix)** · 관대한 파싱 · time_range 매핑.
  - ✅ **S4** (#68) — **[R1#7] config 키 rename**(`brave_count_*`→`count_web/news` + legacy alias+경고) · **[R1#1] Brave 마이그레이션 테스트**(키 있어도 explicit brave만) · 문서(provider 매트릭스·zero-config·live smoke) · close-out. (`.mcpb` provider 셀렉터는 backlog — 기본 ddgs가 zero-config라 비차단.)
  - **결과**: 키 없이 enrichment 동작(ddgs 기본), rate-limit는 throttle/backoff/degraded로 흡수, 깨지면 config 한 줄로 SearXNG/Brave 폴백. 점수 불변.

- **Y2.2 — 원격 transport + 인증 + provider 확장 (✅ remote-ready 토대 완료 2026-06-10; OAuth 인증·운영강화는 실배포 시점 deferred. plan `~/.claude/plans/y2.2-remote-transport-auth.md`; 게이트 D1 small-team single-tenant·D2 Remote Claude Desktop·D5 provider 3-lane 확정)**
  - **계기.** Y2.1이 경로-비의존 I/O를 깔았고, Y2.2는 그 위에 원격 노출(Streamable HTTP)+표준 인증+provider 확장. **여기서부터 서버가 네트워크-도달 가능**(outward-facing). 두 갈래: provider(자율 루프 가능·로컬 검증) / transport·auth(spec-bound·outward).
  - ✅ **Y2.2a** (#59) — 공식 `OpenAIProvider`(key 기반 chat/completions, httpx lazy) + `make_llm_provider` `openai` 분기 + preflight `_VALID_LLM_PROVIDERS`. 비공식 Codex OAuth와 별개(max_tokens/temperature 정상 수용, remote-safe).
  - ✅ **Y2.2b** (#60) — `EVENT_INTEL_DEPLOY_MODE`(personal-local 기본|remote). remote에서 `chatgpt_oauth` 선택 시 CONFIG_ERROR(load_config 게이트). personal-local은 무료 OAuth 보존.
  - ✅ **Y2.2c** (#61) — opt-in `EVENT_INTEL_TRANSPORT=streamable-http`. **127.0.0.1 루프백 기본 + DNS-rebinding/Origin 보호 ON → 머지로 노출 0.** `runtime/transport.py`(env 검증·apply_to_app). stdio 기본 무변경. 실노출은 host 변경 + d 인증 필요.
  - ✅ **Y2.2d-1** (#62) — 원격 tool allowlist(default-deny strict). http 표면=per-user BD 11개만, setup/host-bound(prepare_models·login_chatgpt) withhold. 표면 제거 방식이라 새 error_code 불필요. 미분류 도구 자동 withhold(테스트가 분류 강제).
  - ⏸️ **Y2.2d-2 (OAuth 2.1 resource-server) — 실배포 시점까지 defer (2026-06-10, 사용자 결정).** 토큰 발급자 topology(self-issued vs 외부 IdP)가 plan에 없는 design fork + MCP Authorization spec 정독 + 실키 테스트 불가(크레딧 없음) + **현재 노출 0** → 인증은 실제 원격 배포의 last-mile로 미룸. 재개 시 발급자 확정 → token verifier·audience 검증·Protected Resource Metadata. backlog Y2 절에 박제.
  - ⏳ **Y2.2e** — 비로컬 smoke + secrets/ops도 실배포 시점 deferred(d-2와 함께). **버전 핀은 선반영**: `mcp[cli]>=1.0.0,<2.0.0`(major cap, 로드맵이 명시한 부채; dev 1.27).
  - **결과(remote-ready 토대 완료)**: provider 갈래 완료(3-lane + remote OAuth 게이팅) + transport 토대(loopback opt-in) + 원격 표면 allowlist + mcp 버전 핀. **아직 실제 원격 노출 없음**(인증 deferred — d-2 전까지 public host 금지). MCP 13 tools(stdio)/11(remote). 다음 원격 작업은 실배포 의향 시 d-2부터 재개.

- **Y2.1 — Remote I/O + Job 모델 ✅ 전체 완료 (2026-06-09, plan `~/.claude/plans/y2.1-remote-io-job.md` v0.2 — Y2.0 게이트 + packet QA 각 2라운드 후, 자율 루프로 a~e 머지 #51~#56)**
  - **계기.** 원격 배포(Y2)의 근본 차단은 transport가 아니라 **도구가 서버 로컬 경로를 주고받는 것**. Y2.1은 노출 전에 I/O 계약을 경로-비의존(content/artifact-ID + job)으로 바꿈. **아직 원격 노출 안 함(stdio 유지)** → 인증·access-control floor는 Y2.2. 점수/로컬 동작 불변.
  - ✅ **Y2.1a** (#51) — `storage/artifact_registry.py`: content↔**opaque random artifact_id**(sha256는 metadata만·capability token 누수 차단)·workspace-scoped·TTL+pin·size cap.
  - ✅ **Y2.1b** (#52) — `runtime/io_contract.materialize_input`(path|content|artifact_id **정확히 하나**, inline cap, temp 브리지). validate/ingest cards 3-way, preflight보다 먼저 fast-fail.
  - ✅ **Y2.1c** (#54) — `runtime/job_store.py` file-backed JobStore + `run_as_job` + **`get_job` 도구(13번째)**. 재시작 시 running→interrupted(boot_id, compute resume OOS), 결과 artifact는 job TTL까지 pin.
  - ✅ **Y2.1d** (#55) — build(`tier_list_md/yaml_artifact_id`)·draft(`draft_artifact_id`) 산출물 artifact_id 병행 반환(경로 병행, 로컬 무회귀).
  - ✅ **Y2.1b-2** (#56) — build source 3-way(`source_content`/`source_artifact_id`, file kinds; inline kinds는 source_ref). 원격 CSV 갭 해소.
  - ✅ **Y2.1e** — surface **13 tools** 갱신(CLAUDE/architecture/mcpb/README) + path-free stdio e2e(업로드→artifact로 validate/ingest→job 결과 artifact 회수).
  - **결과**: 전 워크플로가 **서버 로컬 경로 없이**(content/artifact-ID + job) stdio로 동작 → Y2.2(transport/인증)가 위에 얹기만 하면 됨. MCP **13 tools**. 다음 = **Y2.2**(Streamable HTTP + 표준 MCP 인증; 게이트 D1·D2·D5 확정 선행) 또는 Y2.1 잔여 후속(acquire artifact 연계 등).

- **인앱 셋업 패리티 (#14) — 비개발자 첫 실행 셋업을 앱 안에서 ✅ 전체 완료 (2026-06-09, plan `snoopy-weaving-robin.md` 상단 v1, P1.0–P1.3 머지 #46/#47/+1)**
  - **계기.** 비개발자 타깃인데 첫 실행 2단계(bge-m3 ~1.3GB 다운로드 · ChatGPT 로그인)가 CLI 전용 → "Claude Desktop 단일 surface" north star 모순([[inapp-setup-parity]]). 둘 다 무겁고(다운로드·OAuth 브라우저 왕복) 동기 tool은 앱 타임아웃 → **비동기 start→poll** 패턴 필요.
  - ✅ **P1.0+P1.1** (#46) — `runtime/async_job.py` 범용 비동기 매니저(`BackgroundJob.start(fn, block=)`/`status()`/`reset()`, 멱등·실패캡처, warmup.py 패턴 일반화하되 stdio-critical warmup.py는 보존). `prepare_models` MCP 도구(11th) — is_ready→ready 단락, 아니면 background 다운로드 start→`downloading` 즉시 리턴, force 재download. CLI `models prepare` blocking 유지.
  - ✅ **P1.2** (#47) — `login_chatgpt` MCP 도구(12th) — 기존 blocking `login()`을 async job으로 감싸 비동기화(`_pkce_login` 내부 무수정 = OAuth 경로 무위험). `auth_status()`(side-effect-free 공개 API) 추가로 이미 로그인 시 브라우저 skip. `pending` 즉시 리턴→승인→poll. CLI `login-chatgpt` blocking 유지.
  - ✅ **P1.3** — `check_runtime`가 성공·실패 envelope 모두에 `setup` 블록(`model_prep`: model_cached + download job 상태 / `chatgpt_login`: logged_in + login job 상태) 부착(W5 `paths`와 동일 철학, best-effort). surface **12 tools** 갱신(CLAUDE.md/architecture/mcpb/README) + cold-start 가드 3모듈.
  - **결과**: 비개발자가 터미널 없이 앱에서 모델 다운로드·ChatGPT 로그인 → check_runtime로 진행 폴링. ChatGPT OAuth = 무료 체험 온보딩 메인 경로 완성. **770 passed, ruff clean.** Non-goals: warmup.py DRY 통합(후속)·원격 인증(Y2).

- **Workspace & Source Library RAG (WSL) — 신규 제품 역량 ✅ 전체 완료 (2026-06-08~09, plan `snoopy-weaving-robin.md` 상단 v0.2, W0–W5 전 슬라이스 머지 #39~#44)**
  - **계기.** 사용자 작업폴더의 PDF/MD/TXT/CSV를 증분 인덱싱 → (a) 카드 초안을 raw source에서, (b) S/A rationale에 파일·페이지 출처. raw source는 **점수에 절대 미반영**(evidence-floor) — drafting·rationale 보조용. card collection(`product_{ws}`) ↔ raw source collection(`product_sources_{ws}`) 분리. Y1D(rerank)·holdout과 직교한 의식적 detour(= 카드/rationale 품질 개선이지 capability_fit 평탄 fix 아님).
  - **설계.** Codex blind review(roadmap 5건 전부 HEAD 대조 후 수용): WSL을 로드맵 순서에 명시(bench 정리 후·holdout 전 DEV 품질개선) / holdout 전 `threshold-freeze --gates-file` 완전 재freeze=차단 hard-gate / Y2.0 target client matrix 선결 / run_summary에 source-manifest fingerprint / stale 로드맵 숫자 제거→status.md 단일출처.
  - ✅ **W0 — `runtime/paths.py` ResolvedPaths + 경로 버그 2개 fix** (PR #39 머지, main `6278223`): 중앙 경로 resolver(stdlib·side-effect-free·cold-import). workspace_root(~/EventIntel, 기존 checkout은 `<repo>/outputs` back-compat fallback) ↔ data_root(`~/.event-intel`: chroma/artifacts/cache/resume/source-index). per-leaf 우선순위(세부 env→coarse env→config→default). 3개 ad-hoc resolver(_outputs_base/artifacts._base_dir/ChromaProvider) + draft_labels 전부 위임. **버그 fix (a)** draft_capability_cards cwd-상대 `outputs/`→workspace_dir, **(b)** ChromaProvider가 `config.paths.chroma_dir` 무시→honor(preflight이 요구하던 키). 신규 테스트 17건(우선순위 매트릭스·fallback·Windows 경로 공백/한글/OneDrive·cold guard·버그 회귀). CI 양쪽 green. **681 passed, ruff clean.**
  - ✅ **W1 — Source indexer + `product_sources_{ws}` collection** (PR #40 머지, main `7dedf57`): 신규 `sources/indexer.py`(cold·pypdf lazy·providers 주입). 파서 PDF(페이지, OCR 제외)/MD·TXT(utf-8→cp949→cp932 best-effort)/CSV(헤더 보존·행 패킹·행범위 provenance). 청커=`extraction._split_chunks` 재사용 + 슬라이딩 overlap(4000/400). 제한 enforced(500파일/25MiB·파일/300pg/50k행/250MiB). 증분=CS7 atomic(upsert 먼저→full-clean scan시에만 orphan prune, 부분실패=`partial`로 prune 건너뜀·기존 chunk 보존). manifest(`source-index/{ws}/manifest.json`): per-file sha + chunk_ids + pipeline_fingerprint(parser/chunker/embedding 변경시 전체 재인덱싱) + 결정적 content_fingerprint(ts-free, CS7 receipt/fingerprint 분리) → collection metadata에도 기록(drift). os.walk(followlinks=False)+symlink skip. 신규 테스트 23건. CI 양쪽 green. **700 passed.** raw source는 점수 미반영(W3 drafting·W4 rationale 전용).
  - ✅ **W2 — `sync_product_sources` MCP 도구(10번째) + `event-intel sources sync` CLI** (branch `feat/wsl-w2-sync-tool`): W1 indexer를 표면에 노출. ResolvedPaths로 sources_dir(`kind`=all/product/company, 또는 `--source-dir` override)·manifest 경로 결정 → lightweight preflight(require_product_context=False, config 전달) → `sync_sources`. MCPError envelope(`Stage.INGEST` 재사용 — 새 stage/code 없음)·module-ref import·cold guard. 결과=전체/변경/실패/삭제 파일수 + chunk수 + 경고 + collection·manifest·sources_dir + partial. 빈 dir=ok+안내. `sources_root(ws)` accessor 추가. surface **10 tools** 갱신(CLAUDE.md/architecture/mcpb). 신규 테스트 10건(경로 resolution·kind/slug 검증·preflight 실패 envelope·empty 안내·서버 등록 smoke·CLI smoke; 모두 cold-start purge 안전하게 live 모듈 import+string-target patch — playbook #2). CI 양쪽 green. **710 passed, ruff clean.**
  - ✅ **W3 — Workspace drafting (`draft_capability_cards source_kind="workspace"`)** (branch `feat/wsl-w3-workspace-draft`): 신규 `sources/retrieval.py` — 카드 facet별 고정 질의 6종(en/ko: 요약/기능/고객/pain·use case/buying trigger/경쟁·부적합) → `product_sources_{ws}` 검색 → id 중복제거(최소 distance) → **문서별 round-robin spread**(한 문서 독점 방지) → 텍스트 중복제거 → ≤60,000자 cap(truncated flag) + 파일·페이지·행범위 provenance 헤더. 빈 collection→INVALID_INPUT(“`sources sync` 먼저”). draft 핸들러: `source_kind="workspace"`면 preflight(model+chroma)→retrieval blob→기존 drafter에 `text`로 주입(text/file/files 경로 불변), 응답에 `source_retrieval` 메타. CLI `draft-cards --from-workspace`(--source/--text와 상호배타). 신규 테스트 11건(dedup·round-robin·cap·empty envelope·provenance·핸들러 wiring·text 경로 불변·CLI). CI 양쪽 green(help-text 단언 폭 의존 1건 수정 — lesson 기록). **721 passed, ruff clean.** raw source는 drafting 텍스트 보조일 뿐 점수 미반영.
  - ✅ **W4 — ingest sync 훅(opt-in) + rationale provenance(schema bump) + run_summary `source_index_fingerprint`** (branch `feat/wsl-w4-ingest-sync-rationale`): **3파트.** **(A) ingest 훅**: `ingest_product_context(sync_sources=False, force_source_sync=False)` — True면 card 검증→source sync→ingest 순, **partial sync면 card collection 미변경**(degraded-safe, 두 RAG 스토어가 반쪽 갱신으로 어긋나지 않게). 기본 False=기존 동작 완전 보존. indexer에 `force` 추가(manifest 무시 full re-index). MCP/CLI(`ingest --sync-sources`/`--force-source-sync`) 노출. **(B) rationale provenance**: `gather_exhibitor_provenance`(S/A 행 query→`product_sources_{ws}` top-3 chunk, 파일·페이지·행 provenance; **graceful — 빈/실패=`{}`, build 절대 안 깨짐**). report `REPORT_SCHEMA_VERSION` 3→4: 행별 `source_provenance` 필드(YAML) + MD “source grounding” 블록. build 7b단계: scoring **후** 별도 collection 조회→report 필드에만 기록(점수/티어로 가는 경로 없음). **(C)** run_summary `source_index_fingerprint`(product_sources manifest의 결정적 지문, source 미사용=null; **run_fingerprint엔 미포함** — 커밋된 baseline churn 방지). 신규 테스트 13건 — **핵심: provenance 부착 전후 모든 scoring 필드 동일** + schema bump + MD 렌더 + provenance helper + run_summary 필드 + ingest 훅(default/happy/partial-abort/force) + indexer force. CI 양쪽 green. **734 passed, ruff clean.**
  - ✅ **W5 — MCPB folder config + `storage migrate` + check_runtime 경로 노출 + 문서** (branch `feat/wsl-w5-mcpb-migrate-docs`): **(설치)** mcpb manifest에 `Working folder`(workspace_dir)·`Local data & RAG folder`(data_dir) user_config + `EVENT_INTEL_WORKSPACE_DIR`/`_DATA_DIR` env 전달. manifest_version 0.2 유지 → directory 타입 대신 **string fallback**(blank=기본값; 0.3 directory picker는 미채택). **(가시성)** `check_runtime`이 성공·실패 envelope 모두에 `paths` 블록(workspace/cards/sources/reports/chroma/artifacts/manifest 실경로 + exists/writable + is_legacy) 부착 — 모델/키 미준비여도 경로 확인 가능. **(마이그레이션)** 신규 `storage/migrate.py`(stdlib·cold): `<repo>/outputs`→`~/EventIntel` **copy→checksum 검증→목적지 확인, 원본 미삭제, 동일 skip·충돌 미덮어쓰기, symlink 미추적**. CLI `storage migrate`(기본 dry-run / `--apply`). Chroma/artifacts(data_root)는 미이동 → 서버 종료 불필요(명시 note). **(문서)** README(folders & migration 절·10 tools·source library step)·commands.md·architecture. 신규 테스트 13건(plan 분류·apply 검증/원본보존/충돌·default roots·check_runtime paths 성공/실패·CLI dry-run/apply·manifest). **747 passed, ruff clean.**
  - **WSL 결과**: card collection(`product_{ws}`) ↔ raw source collection(`product_sources_{ws}`) 분리 완성. 사용자 작업폴더 PDF/MD/TXT/CSV → 증분 인덱싱 → 더 나은 카드 초안(`--from-workspace`) + S/A rationale provenance(report v4). **점수/티어 경로 불변**(structural + headline 테스트). 경로는 ResolvedPaths 단일 resolver.
  - **⏸️ Y1D + holdout 장기 보류 (2026-06-09, 사용자 결정)**: 적합한 gold 데이터(대표 pair·정식 holdout pair) 미확보 → 정합성(`capability_fit`) 정확도 개선(rerank/retrieval)과 holdout 본측정을 **무기한 보류**, backlog #1/#6 장기 과제로 박제. 측정 인프라(CS1–CS9)·라벨링(L0–L6)은 완성·머지 상태로 보존(데이터 확보 시 재개). **다음 작업 = #14 인앱 셋업 패리티**(비개발자 진입장벽, `prepare_models`/`login_chatgpt` 인앱화).

- **Y1 라벨링 시스템 — 멀티벤더 gold 생산 (2026-06-08, plan `snoopy-weaving-robin.md` 상단 v3, **L0–L6 전체 완료**, PR #29~#34)**
  - **계기.** Y1 측정 인프라(CS1–CS9)는 완성됐지만 진짜 병목은 **gold 라벨 생산**. 수작업 전수는 비현실적(HCR 296사), 사람 전수검수는 "순서만 바뀜". AI는 이 분류를 충분히 함 — GTC 20사에서 다른벤더 AI(Claude) vs 사람 **95% 일치, competitor 100% 일치** 실증. 목표=개별정확도가 아니라 **싸고 반복가능하고 감사가능한 멀티벤더 gold 생산 시스템**(엔진=GPT-OAuth ↔ 라벨러=Claude → blind-spot 독립성 + 비용차익 + 불일치=불확실 신호).
  - **설계.** plan v1→v3 (Codex blind review 2라운드, 11건 전부 HEAD 대조 후 수용). 핵심 교정: silver(단일벤더 자동채택)/gold(교차합의·검색·사람) 분리·holdout=gold만 / 미측정 required 게이트=ineligible(≠pass) / pair별 게이트 applicability / waived≠pass / 교차합의 독립성 증빙(SHA) / 데이터 위생. **R2-3: 이미 라벨 본 P4는 재freeze로 holdout 복구 불가 → P4=DEV 강등, 새 blind pair 사전지정.**
  - **아키텍처**: company-packet → **Draft(GPT-OAuth, silver)** → **Flag(competitor/bad_fit·저확신)** → **Refine(Claude+웹서치, gold)** → Seal(grade 보존) → measure(holdout=gold만). 검색은 MCP 서버 아닌 호스트(Claude app/agent)가.
  - ✅ **L0 게이트 적격성 fix** (`cd1b6a2`) — eligibility {pass/fail/ineligible/waived} + pair별 applicability(required/optional/not_applicable) + waiver(사유·승인자, ≠pass). partner competitor 자동 not_applicable. freeze/load 4-튜플. blind review가 잡은 "미측정=통과" silent-validity 버그 차단. **617 passed.**
  - ✅ **L1** (PR #30) — `label_draft.py` GPT-OAuth 배치 초안(silver): suggested_label/confidence/rationale, 실패행 needs_review. prompts/{en,ko}/draft_labels.txt.
  - ✅ **L2** (PR #30) — grade를 sheet→`SealedLabels`(행단위 grades/provenance)→measure까지 전달. flag_for_review(게이트클래스·저확신 플래그, 비플래그=silver 자동채택) + extract_sealed_inputs + **measure(holdout=True) silver 거부**.
  - ✅ **L3** (PR #31) — gold 승격: 교차벤더 합의(GPT∧Claude 일치=gold, **independent_input_sha로 GPT-blind 증명**, 불일치→플래그) + 검색 refine(apply_refinements, 플래그행만) + CLI(independent-view/cross-vendor/apply-refinements).
  - ✅ **L4** (PR #31) — `benchmark label-stats`: gold_rate/cross_agree_rate/flag_rate/flip_rate(gold 신뢰도 정량).
  - ✅ **L5** (PR #32, docs #33) — `benchmark draft-labels`(L1+L2 통합 CLI) + `threshold-freeze --gates-file`(완전 freeze) + **데이터 위생**(sheet/worksheet/ai_labels gitignore + `benchmarks/_local/`로 migration + git check-ignore 테스트). gold/엔 sealed_labels·roster·measure만 커밋. commands.md에 benchmark CLI 카탈로그.
  - ✅ **L6** (PR #34) — **`draft_labels` MCP 도구(9번째)**: Claude Desktop이 초안+플래그 받아 자기 웹서치로 refine(inapp-parity #14). 서버=silver 초안+플래그, 검색=호스트. surface 9 tools 갱신(CLAUDE.md/mcpb/architecture).
  - **코드 페이즈 종료**: L0–L6 전부 머지. **MCP 도구 9개**(5 core + 3 acquisition + 1 labeling). **658 passed.** 남은 건 **데이터-ops뿐**(실 pair gold 생산 → measure; `docs/commands.md` "Y1 benchmark" 절).
  - **사용자 결정 확정(2026-06-08)**: competitor holdout = **MongoDB × AI Expo Tokyo**(E5). 정식 holdout 절차 전까지 **blind 유지**(메모리 [[y1-competitor-holdout]]). 현 thresholds.json은 불완전 freeze라 정식 게이트 전 `threshold-freeze --gates-file` 재freeze 필요.
  - **기 생산물(로컬, `benchmarks/_local/`)**: GTC(P1) 사람 gold 20 + measure(P@10 0.3, no-enrich), HCR(P4→DEV) AI 라벨 296. 카드 3종 ingest + CS7 receipt(drift=match) 검증 완료.
  - **라이브 데이터-ops 검증(2026-06-08)**: GTC(DEV)로 멀티벤더 전 파이프라인 라이브 완주 — draft(GPT-OAuth silver 7/flag 13) → cross-vendor(Claude 독립, 합의 13 gold) → 웹서치 refine(7 gold, **검색이 GPT bad_fit 오판을 target으로 교정** — Together/Anyscale/Modal=MongoDB 생태계 파트너) → seal(20 gold) → measure. L0 게이트·독립성 SHA·gold-only 전부 라이브 동작.
  - **측정-등급 baseline(GTC×MongoDB, Brave enrich, OAuth)**: extraction_coverage 0.88(cap 30) · **P@10 0.4** · competitor/bad_fit leakage 0.0(단 S/A 0개라 자명) · **AUC 0.69→0.81**(enrich가 변별력 개선) · eligibility=**fail**(P@10 게이트). DEV 1 pair, holdout 아님.
  - **🔬 Y1D 진단(근본 원인, 데이터)**: `capability_fit` RAG 코사인이 **전 라벨 ~0.5로 평평**(target 0.54 ≈ bad_fit 0.50) → 엔진이 penalty로 나쁜 fit 배제는 하나 **target 양성 식별 실패**(target final 4.16 ≈ neutral 3.93) → P@10 약점. **가중치 재조정 무의미(평평한 신호); 처방=retrieval/rerank 품질**(backlog #6 1순위, #1). S/A 0개 → tier 캘리브레이션도 검토. **선빌드 금지 — 별 phase plan 통제 실험으로.**
  - **bench 산출물 정리(plan v0.2)**: GTC DEV 기준선을 **최초 커밋**으로 고정 — `gold/p1_mongodb_gtc/{roster, sealed_labels(멀티벤더 canonical, 20/20 gold), measure_no_enrich(P@10 0.4·AUC 0.69), measure_enriched(P@10 0.4·AUC 0.81)}` + `runs/.../run_result.json`×2 + `gold/p4_hyodol_hcr/roster.json`. 사람 라벨·legacy silver·company_packet·불완전 thresholds는 `_local/`(gitignore). canonical 근거(human↔AI 3 diff + evidence)는 `benchmarks/README.md`. AI Expo Tokyo holdout 전 universe+coverage 포함 재freeze 필요.

- **Y1 실데이터 벤치마크 — 코드 페이즈 완료 (2026-06-08, plan `y1-execution-v4.md` / 상위 `snoopy-weaving-robin.md`, branch `feat/y1-benchmark`)**
  - **계기.** 엔진은 빌드·내부정합성 완료지만 **독립 gold label 대비 정확도 미검증**. 현 `eval/harness.py`는 fit/sim/news 주입 + `cards=None`으로 scorer만 돈다. Y1은 실 카드 3종 × 실 이벤트 6종 × 독립 blind 라벨로 그 빈틈을 닫는다. **이 plan의 핵심 = 측정 자체의 타당성** — blind 경계·cohort·재현성을 코드 경계로 강제.
  - **설계.** plan v1→v4 (Codex 3라운드 + Gemini context-starved skeptic, 전부 HEAD 대조 후 수용). 측정 타당성 버그 다수 차단: blind 경계 자기모순, P@10 `/len` 부풀림, leakage 분모 게이밍, partner competitor 게이트 충돌, cap-0 sentinel, holdout 순서 모순, evidence 1건-통과.
  - ✅ **CS1 run-summary** (`5b0ad12`) — `run_id`(고유·immutability) / `run_fingerprint`(결정적·재현검증) 분리 + build hook + immutable run dir.
  - ✅ **CS6 metrics** (`777a164`) — `precision_at_k` `/k`(미추출=miss) · 3분리 leakage(end_to_end 진단/conditional 게이트/coverage 게이트) · evidence_precision `min_items` 게이트 + `evidence_yield` · `N/A` vs `insufficient_n`. 기존 9-cell 함수 unchanged.
  - ✅ **CS2 roster** (`4c2c835`) — `match_norm`(NFKC + 한·일 법인격/punct) · cardinality taxonomy(1:1 / N:1 dedupe / 1:N materialize-only / ambiguous) · `extraction_coverage` vs `mention_coverage` 분리(R3-4).
  - ✅ **CS3/CS3b blind packet** (`9d39f12`) — pair별 cohort(full / top10+고정 decoy) · packet≠sealed labels(별 파일) · evidence packet은 labels 봉인 *후*만 생성(R2-2 순서 강제) · item 스키마 tier/score 은닉.
  - ✅ **CS4 run/measure 분리** (`b8f71bf`) — **gold-blind 경계를 코드로**: `run`은 gold 파라미터 없음 + gold-ish 키 거부, `measure`는 `SealedLabels` 아니면 `TypeError`. measure가 roster_id 공간으로 join → CS6 지표 + D6 게이트(N/A·insufficient_n는 실패 아님).
  - ✅ **CS7 ingest receipt** (`8c6b0a1`) — `content_fingerprint`(정렬 chunk_id+doc_hash+model+collection, **ts 제외**) vs `ingest_receipt.json`(instance, ts) 분리 · Chroma collection metadata에 기록 → `verify_collection_fingerprint`로 measure-time drift 감지 · build hook이 receipt fingerprint를 run_fingerprint에 folding.
  - ✅ **CS8 benchmark CLI** (`795544e`) — `threshold-freeze`(immutable manifest, step 1) / `run`(gold-blind) / `company-packet`(cohort) / `evidence-packet`(labels 봉인 후만) / `measure`(join→게이트, 실패 시 exit 1). 상태머신 순서를 CLI가 강제.
  - ✅ **CS5 contract-replay** (`14ce6ab`) — raw는 gitignore(`benchmarks/_raw/`), 커밋 fixture는 **구조보존 synthetic**(길이·줄경계·Unicode script·중복 보존 → 청킹/매칭 경로 실제로 태움, Q4). `FakeReplayLLM` 결정적 재생 + 커밋 corpus `benchmarks/replay/p_static_demo`.
  - **테스트**: **598 passed**, ruff clean. 신규 eval 모듈(run_summary/roster/blind/benchmark/replay) 전부 cold-import 가드 통과(Q2). 9-cell scoring 회귀 유지.
  - **gotcha(정정)**: CS7 첫 커밋에서 테스트가 `tests/fixtures/cards/`에 receipt를 써 fixtures 오염 → 테스트를 tmp 복사본으로 고치고 파일 제거 후 커밋 amend(push 전).
  - **남은 것**: PR→main 후 **데이터/ops(사용자 차단)** — 잔여 5개 이벤트 캡처 → roster → 카드 ingest(`models prepare` 선행) → DEV blind 라벨 → measure → holdout 게이트(threshold freeze 최우선). 약점 발견 시에만 Y1D 조건부 fix.

- **Agentic Acquisition Ladder — 무개입 source 수집 (2026-06-08, plan `agentic-acquisition-ladder-impl-v1.md`, branch `feat/acquisition-ladder` → merged PR #26)**
  - **계기.** Y1A 벤치마크용 H.C.R.(일본 B2G 케어로봇 전시회) 수집이 operator-capture(수동 Ctrl+S)로만 가능하다고 판정됐는데, UX가 나빴다. 실제로는 Vue SPA가 외부 `<script src>` 번들 안에서 `axios.get('_ajax/exhibitor/get_exhibitor_data/')`로 296사 JSON을 부르는 깨끗한 엔드포인트가 있었음 — `<base href>` 해석 누락 + analyzer가 inline script만 스캔 + regex가 document-relative URL을 거부해서 놓쳤던 것. → "AI 판단은 prior로만 쓰고, 결정적 ladder가 여러 전략을 예산 안에서 시도"하는 구조로 재설계.
  - **설계.** design v1→v2→v2.1(blind review 2라운드 + 18T-conflict grep), impl plan C1–C7 슬라이스. LangGraph는 명시적으로 기각(재현성 — 제어흐름은 결정적, AI는 판단만).
  - ✅ **C1 streaming byte cap** (`cf537f0`) — `raw_fetch`가 `client.stream()`+`iter_bytes()`로 진짜 대역폭 cap(사후 길이검사 아님). `RawResponse.truncated/byte_count`.
  - ✅ **C2 endpoint regex** (`c066c2d`) — fetch/axios/$.get url 그룹이 document-relative 리터럴 허용(`_ajax/...`) + `_is_static_url_literal`(슬래시 필수·`${}`/concat 거부).
  - ✅ **C4 언어중립 JSON roster validator** (`53be972`) — JSON은 구조(bounded nested DFS로 최대 list-of-dicts + 회사신호 키/필드 + 고유 회사명 수), HTML은 키워드. EN/KO/JP roster 통과 + product/staff/menu/config JSON 거부.
  - ✅ **C5 winner provenance + redaction** (`5c06095`+`f850e13`) — 채점 response 보존(재fetch 제거), `RequestSpec`은 pagination/provenance 전용, token/key/secret/Authorization/Cookie redact. (cold-start fix: string-path patch.)
  - ✅ **C6 content-type 인지 artifact** (`a23550c`) — JSON winner → `source.json`/`text_file`(본문 sniff 가드), else html_file.
  - ✅ **C3 `<base>`+번들 discovery + analyze_response 분리** (`1e07826`) — `analyze_response(resp)` 분리(landing 1회 공유), `resolve_base_href`/`extract_script_srcs`/`discover_endpoints_from_bundles`(동일출처 1단계, 재귀 금지), prompt verdict-gated empty 규칙 제거(verdict 무관 hints 보고).
  - ✅ **C7 ladder + budget + early-exit + manifest provenance** (`2411c2a`) — verdict switch → 고정 rung 집합(static/embedded/xhr/bundle/operator), verdict는 순서 prior. `AcquireBudget`(per-response/누적 byte cap + 호출 cap + monotonic deadline + bundle rung 예약). early-exit: landing 401/403/404/5xx만 fatal(LLM 전), 파생 실패는 skip. manifest에 selected_rung/winning_request(redacted)/analysis_fp/config_fp(`.get()` 하위호환). `acquisition:` config 블록 신설.
  - **테스트**: **515 passed**, ruff clean. DoD e2e(operator-prior→bundle→JP JSON→source.json+provenance, landing 1회) green. SPA-shell-not-static / budget-deadline-blocks-bundle / operator-prior-doesn't-block-static / landing-401→LOGIN_REQUIRED / manifest backward-compat.
  - **gotcha(lesson 기록)**: 합성 JSON roster fixture가 <1KB면 http_status_map의 short-body operator heuristic에 먹힘 — 실 HCR은 577KB라 무관하나 fixture는 1KB 초과 필요.
  - **남은 것**: PR→main 머지 후 Y1A 재개(나머지 이벤트 수집→roster→gold label→ingest→측정).

- **Phase 18W — 범용화 잔여 (2026-06-07, plan `phase-18w-p2.md`)**
  - ✅ **9-셀 full eval matrix** — 제품(DB/부품/B2B) × 행사(AI/제조/일반) 9셀 labeled fixture(`tests/fixtures/eval/*.yaml`). 단일 gold set 과적합 한계 보완 — 9셀 모두 AUC 1.0 / competitor leakage 0 / evidence-FP 0. parametrized 게이트 9셀 자동 커버. backlog #13에서 promote. (PR #17)
  - **P2 4건 — 정적 blind review 2라운드(14건, 전부 HEAD 대조 후 수용) 후 실행. 각 항목 CI 게이트 통과 머지.**
  - ✅ **P2-1 캐시 TTL + fingerprint + 진짜 refresh** (PR #18) — `ENRICH_CACHE_VERSION 4`(캐시 `cached_at` 래핑 + resume `enriched_at`/`input_fp`). 공유 `_is_fresh` 계약(None=무기한/0=항상stale/양수=ttl). `--refresh`가 resume+cache 읽기 **둘 다** 우회(쓰기는 유지). `input_fp=sha1(name|url|snippet|confidence|config_fp)` → 입력 변경 시 TTL 무관 재enrich. `config_fp`는 enrichment 필드만(scoring weight 격리). `now` 주입. config `cache_ttl_days`/`resume_ttl_days`(7).
  - ✅ **P2-2 round-robin evidence 할당** (PR #19) — `allocate_round_robin` 순수 함수(event cap 설정 시 공정 분배, cap=0 기존 동등). enrich 대상만 사전 할당(skip 회사 예산 미소모). 회사별 즉시 resume.append 유지 → 후반 실패가 앞 회사 유실 안 함(내구성 테스트). 2-pass 재작성 회피(review r2 #4).
  - ✅ **P2-3 leakage 분리 + mode 정책** (PR #20) — `bad_fit_leakage_rate` 신설(competitor와 별도 denominator, review r2 #5). 모드 정책표 확정: competitor는 customer만 negative(partner neutral/ecosystem positive), bad_fit은 전 모드 negative. `ecosystem.bad_fit_penalty_factor 0.0→1.0`(B안, 사용자 결정). 게이트: bad_fit_leakage=0 전 모드. 직접 mode 테스트(BASELINE_CELL 재채점, 신규 fixture 0).
  - ✅ **P2-4 cards-backed CJK category-fit 측정** (PR #21, report-only) — `score_category_fit`이 `cards=None`에선 항상 0이라 eval harness로 측정 불가했던 한계(review r2 #1) 해소: 실제 `CapabilityCards`(CJK industries)로 직접 호출. **측정 결과: bigram이 적대적 케이스 false-overlap 100%(4/4)** — JP 半導体↔指導体制, ZH 半导体↔领导体系, KO 이차전지↔전지적(전부 0.5 오탐). positives 7건 전부 category_fit>0. CI 게이트 아님(report-only). → Step 1 도입 정당화.
  - ✅ **P2-4 Step 1 CJK morphological 백엔드** (plan `phase-18w-cjk-lib.md`, blind review 2라운드 v1→v3) — pluggable `scoring.cjk_tokenizer.mode: bigram|morphological`. `scoring/cjk.py`가 **호출당 1회** segmenter 결정(가나→janome/한글→bigram/순수Han→`han_default`) → **needle·haystack 동일 segmenter로 대칭**(2-char-window 오탐 제거의 전제). language는 출력 `lang`과 독립(config). lazy import(`@lru_cache` factory, cold-start 가드에 janome/jieba 추가, `[cjk]` extra) + bigram fallback(warn 1회) + 잘못된 config→CONFIG_ERROR. **acceptance: JP/CN 적대적 오탐 3건 제거 + positives 무회귀**(`[dev,cjk]` 전용 CI job). KR은 bigram 유지(오탐 1건 known-limitation, kiwipiepy 별도 phase). Step 0 dependency spike로 janome/jieba 분할 사전검증. 커밋 4개 분리.
  - **테스트**: 470 passed(+10 morphological acceptance), ruff clean, CI `pytest`+`cjk` 양 job.
  - ✅ **P3 known-limitation 3건** (2026-06-07, branch `phase-18w-p3`) — generic 단일토큰(r3 #3): `name_tokens` len>=2로 짧은 distinctive 토큰 보존(단일 generic-word는 수용). same_site(r3 #5): `_TWO_LEVEL_SUFFIXES` 확장(관리형 호스팅+ccTLD, PSL은 계속 defer). lint: ruff에 ANN + 자동수정 D(D208/209/413) 추가(ANN401·tests 제외, D 34 자동수정 + ANN 29 수동, 전체 docstring은 미채택). **473 passed**. KR 형태소(kiwipiepy)는 네이티브 의존으로 사용자 결정 deferred 유지.
  - **남은 18W(backlog #13)**: 없음 — P2 4건 + P3 4건 전부 완료.

- **Phase 18X — KR morphological tokenizer (2026-06-07, plan `phase-18x-kr-morphological.md`)**
  - **계기.** Phase 18W P2-4에서 KR만 bigram 잔류(유일한 미적용 CJK 언어). kiwipiepy가 네이티브 휠이라 별도 phase로 분리됐던 것.
  - ✅ **kiwipiepy ko 백엔드** (opt-in `[kr]` extra — `[cjk]` 순수 파이썬 유지, JP/CN 사용자는 네이티브 의존 안 받음). `_kiwi_segment` lazy `@lru_cache` + content-morpheme 필터(NNG/NNP/NNB/SL/SN/XR). `resolve_segmenter` ko 분기 + bigram fallback(warn `.[kr]`). cold-start 가드 kiwipiepy/kiwipiepy_model 추가.
  - **Step 0 dependency spike가 설계를 정정**: ① bigram-윈도우 인공물 오탐 제거(자동차부품↔수동차단기 via 동차) ② **헤드라인 동음이의 케이스(이차전지↔전지적)도 해결** — score_category_fit가 단어별로 분할하므로 고립된 `전지적`을 kiwi가 `{지적}`로 파싱(전지 미산출) → 충돌 없음. (문장 전체를 넣은 spike에선 전지적→전지+적로 쪼개졌으나 per-word 파이프라인은 다름.) ③ 비용: ~109MB 모델 → opt-in이라 비-KR 영향 0.
  - **테스트**: 479 passed(+6 KR acceptance). ruff clean. `cjk` CI job → `[dev,cjk,kr]`로 KR도 게이트.
  - **결정(사용자)**: 별도 `[kr]` extra(추천) + opt-in 진행(동음이의 spike 결과 확인 후).

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
  - **round-3 정제 (2026-06-06)** — 3차 5건(전부 valid). P1 2건이 *직전 라운드 수정의 빈틈*: #1 카드 replace 비원자성(delete→upsert) → **upsert-first + orphan prune**(PR #14, `existing_ids`/`delete_ids`), #2 buying_signal이 floor와 다른 substring matcher 사용 → **evidence.mentions_name로 단일화**. #4 top-N eval이 tier 변화 미단언 → A/B 경계 단언. #3(generic 단일토큰)·#5(same_site allowlist) → backlog #13 P3 known-limitation. **439 passed**.
  - **round-4 fresh-vendor skeptic (2026-06-07)** — 같은 벤더 3라운드의 mutual blind spot 차단 위해 **다른 벤더**에 context-starved skeptic 1회. 3라운드+내 수정이 놓친 실버그 1건 발견(PR #15): `score_buying_signal`이 unmatched news에 base만 반감하고 **recency·trigger 보너스는 전체 news에서** 계산 → 최신·trigger 포함 무관 기사가 signal을 1.0까지 올림. **보너스도 name-matched news로 게이트**. skeptic은 packet anchoring(SHA 미고정·평가성 표현)도 정확히 지적 — 코드 아닌 packet 이슈. **440 passed**. → 수렴 신호(material 1건), 18V.1 종료.
  - **남은 갭(18W)**: backlog #13 참조 (전부 P3 known-limitation + #2 캐시TTL/#6 round-robin).

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
5. ✅ Phase 18U/18V/18V.1/18W/18X — 스코어링 변별력 + 범용화 + P2·P3 + CJK 3언어 (479 green, backlog #12/#13 종료)
6. **다음 큰 방향 — 로드맵 Y1 → Y2** (`~/.claude/plans/snoopy-weaving-robin.md` v3, `backlog.md#다음-큰-방향---로드맵`):
   **Y1 실데이터 정확도 검증**(gold-label 벤치마크 — 아직 미검증) → **Y2 원격 배포**(Streamable HTTP + 표준 MCP 인증; remote I/O 선결). 각 phase 착수 시 별도 상세 plan.

세션 간 재개: `docs/status.md` + 로드맵 `~/.claude/plans/snoopy-weaving-robin.md` 먼저 읽기.
