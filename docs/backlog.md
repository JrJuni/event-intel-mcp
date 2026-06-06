# Backlog

장기 계획, 큰 그림, v0 scope 외 항목. 진행 중/최근 완료는 [status.md](status.md).

`status.md`와의 분리 원칙: **status는 "지금 또는 직전"**, **backlog은 "아직 안 시작 또는 의도적 defer"**.

---

## P1 — v0 진입 후 가장 먼저 검토

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

- **형태소 분석 라이브러리 (P2)** — 규칙기반 CJK bigram이 JP/CN 셀에서 미흡할 때만 janome/jieba 도입. cold-start lazy-import + 패키징 부담 평가 동반.
- ~~**9-셀 full eval matrix (P2)**~~ ✅ 완료 (2026-06-07, Phase 18W) — 제품(DB/부품/B2B) × 행사(AI/제조/일반) 9셀 fixture 전부 작성(`tests/fixtures/eval/*.yaml`). 9셀 모두 AUC 1.0 / competitor leakage 0 / evidence-FP 0 통과. harness가 `*.yaml` glob → 자동 게이트.
- **ecosystem 셀 leakage 재정의 (P2)** — partner/ecosystem 모드에서 competitor leakage 지표 의미 반전 → mode별 positive label·기대치 fixture 정비.
- **캐시 TTL / resume 신선도 (P2, blind review r2 #2)** — 검색 캐시 키에 만료(주차 버킷 등) 없음 → "최근 180일" 결과가 수개월 뒤 재사용. resume도 event_slug+회사명 기준이라 같은 이벤트 재실행 시 변경된 뉴스/snippet/confidence 무기한 skip. → 캐시 만료 정책 + resume `--refresh`/변경감지.
- **evidence 예산 round-robin (P2, blind review r2 #6)** — 현재 per-company 예산(default event cap 0)이라 starvation 없음. 단 event cap을 다시 켜면 순차 루프상 뒤 후보가 굶음 → 전역 round-robin 분배로 재설계해야 cap+공정성 양립.
- **generic 단일토큰 회사명 floor 오탐 (P3, blind review r3 #3)** — `mentions_name`이 토큰경계+generic guard로 강화됐지만, 토큰이 단일 generic 단어뿐인 회사명("Data"/"Cloud")이나 distinctive 토큰이 전부 <3자라 `name_tokens`가 떨군 경우("Data AI"→["data"])는 여전히 느슨하게 매칭. 단일 generic-word 회사명은 본질적 모호 — 추후 phrase 요구/사전 보강 검토.
- **same_site allowlist 한계 (P3, blind review r3 #5)** — `registrable_domain`이 알려진 멀티테넌트 suffix(github.io/vercel.app 등) 목록 기반. 목록 밖 호스팅 도메인은 동일 회사로 오판 가능. cold-start 제약상 PSL 라이브러리 미도입(현 절충 수용). 필요 시 목록 확장 또는 lazy PSL.
- **lint 추가 룰 (P3)** — 현 ruff select(E/F/I/W/B/UP)에 D(docstring)·ANN(type annotation) 등 점진 도입 검토.

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

### #2 Provider 교체 구현
v0는 인터페이스만 두고 default 구현 1개씩 (Anthropic / bge-m3 / Chroma / Brave / httpx). v0.4+에서 OpenAI/Voyage embedding, Tavily search 등 교체 가능하게.

### #3 Rate-limit / backoff
Brave / Anthropic 대상. v0는 per-call cache로 충분하지만 large-scale 사용 시 본격 exponential backoff 필요.

### #4 운영자 brief export 자동화
v0에 `product_brief.md`를 capability cards에서 자동 생성하는 export view는 있음 (S5 `brief_export.py`). v0.4+에서 PDF / Notion / Slack post export로 확장.

### #5 Resume granularity 강화
v0는 per-row resume (enrichment 실패 row만 재시도). v0.4+에서 stage 단위 + per-call cache 결합으로 더 세밀한 재개.

---

## P3 — v0.5+ 영역

### #6 Cross-encoder rerank
bge-m3 only로 시작. 정확도 부족 검증 시 reranker 도입.

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
