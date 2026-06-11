# Retry Playbook — 사이트·실패 형태별 수집 전략 (ZNC R3)

수집(검색·본문 fetch·acquisition) 실패에 대한 **운영 전략 카탈로그**. `playbook.md`(코드 패턴)와 달리
이 문서는 "어떤 실패 형태에 어떤 대응이 실측으로 정당한가"를 기록한다. **모든 상수는 근거 데이터와
함께 적고, 미검증 상수는 PROVISIONAL로 명시한다** (사용자 원칙: 상수는 선험이 아니라 데이터에서).

- **근거 데이터**: `~/.event-intel/diagnostics/**/*.jsonl` (R1 계측) → `benchmark retry-stats`.
- **개정 트리거**: R2 크론 캠페인(`EventIntelR2Smoke`, 4h 간격) 누적 ≥10런 시점마다 재집계·갱신.
- 최초 작성 2026-06-11: **1,141 events / 12 파일** (스모크 1배치 + AIEWF 재빌드 2회 + gold-blind 런 3회).

---

## 1. 검색 lane (ddgs auto + Google News RSS 폴백)

### 실측 (2026-06-11, ~900 검색 쿼리)

| outcome | 건수 | 해석 |
|---|---|---|
| ok | 811 | |
| no_results (genuine) | 89 | 빈 결과 = 정답. 캐시됨, 재타격 없음 (N2) |
| **degraded (rate-limit)** | **0** | 재시도·폴백·rescue가 한 번도 발화하지 않음 |

### 전략

| 형태 | 대응 | 근거 |
|---|---|---|
| 1차 방어: **process-wide 스로틀 1100ms + backend=auto(호출별 엔진 셔플)** | 현행 유지 — **CONFIRMED** | 빌드당 30~100쿼리 프로파일에서 rate-limit 0/900. 이 사용량에선 스로틀이 한도를 완전히 회피 |
| rate-limit/transport 재시도 상한 `search.max_retries: 5` | **PROVISIONAL** 유지 | 발화 사례 0건 — 검증 불가. 상한은 안전망으로 보존; 회복 곡선 데이터가 생기면 재결정 |
| backoff 곡선 `min(2^n, 15s)` | **PROVISIONAL** | 동상 |
| RSS 폴백 발화 조건: degraded일 때만 (genuine empty 미발화) | 현행 유지 — 설계 원칙 | 폴백 발화 0건 (degraded가 없었으므로); 예산·결정성 원칙 유지 |
| genuine empty 분류 (`"No results found"`) | **CONFIRMED** | 89건 전부 정상 분류·캐시. 사전엔 이 클래스가 스테이지 abort를 유발했음 (lesson 2026-06-11) |

**주의**: rate-limit 0건은 "재시도가 불필요"가 아니라 "이 볼륨에선 스로틀이 충분"이라는 뜻.
병렬 빌드(프로세스 분리 — 스로틀 미공유)나 대량 캠페인에서는 발화할 수 있다 → 그때의
회복-vs-시도 곡선이 상한 확정의 데이터다.

## 2. 뉴스 본문 fetch lane (B1)

### 실측 (241 fetch events)

| outcome | 건수 | 비율 |
|---|---|---|
| ok | 176 | 73% |
| too_short (본문 빈약 — 결정론, 캐시됨) | 42 | 17% |
| **HTTP 403/405 (봇 차단·페이월)** | **16** | 7% |
| HTTP 429 (rate-limit) | 2 | <1% |
| robots 거부 | 5 | 2% |

403 도메인 표본: thestreet.com, darkreading.com, investing.com, financialexpress.com 등 —
**대형 뉴스사이트의 봇 방어. 동일 UA 재시도는 무익** (방어는 결정적).

### 전략 (R3에서 코드 반영)

| 실패 형태 | 대응 | 근거 |
|---|---|---|
| **403 / 404 / 405 / 410** | **재시도 0회, 폴백 없음** — snippet-only 강등, 미캐시(다음 RUN에서 1회 재시도 기회) | 16건 전부 결정적 거부 — **CONFIRMED** |
| **429 / 5xx / transport 오류** | **1회 재시도 (2s 대기)** 후 강등 | 429 실측 2건 — 일시 형태 존재. 빈도(<1%)상 1회면 충분 — **CONFIRMED(소표본)** |
| robots 거부 | 재시도 없음 (정책 준수); 미캐시(5xx-deny는 일시일 수 있음) | 설계 원칙 |
| too_short | 부정 판정 캐시 (재fetch 안 함) | 결정적 콘텐츠 속성 — **CONFIRMED** |
| **Google News `/rss/articles/` 래퍼 URL** | **본문 fetch 불가 — robots 차단 30/30 (2026-06-11 실측).** listed 뉴스로는 유효하나 기준 ①에 기여 못 함 → **Bing RSS(publisher 직접 URL)를 1순위 lane으로** (defaults 반영) | **CONFIRMED** |

## 3. 사이트 형태별 (acquisition — G1 실측)

| 형태 시그니처 | 사례 | 권장 rung |
|---|---|---|
| **서버렌더 + charset 헤더 누락(EUC-KR/SJIS)** | AI EXPO KOREA | static rung + meta-charset 스니핑(#87). 검증기 키워드 스코어가 실제 한국어 roster를 0.325로 거부한 false-negative 있음 → **검증기 개선 후보(데이터 누적 후)** |
| **JS 셸 + 공개 sitemap** | Big Data LDN | 페이지 fetch 대신 `sitemap.xml` → `*-sitemap` 자식에서 엔터티 열거 — **ladder 신규 rung 후보** |
| **A–Z 인덱스 분할(letter 파라미터)** | Hannover Messe | 쿼리 파라미터 열거 크롤(예의 간격 1s). 챌린지 휴리스틱이 로그인 오버레이 `:has-captcha` 속성에 오탐 → **휴리스틱 fix 후보** |
| 진짜 봇 챌린지 / 로그인 wall | (미조우) | operator-capture는 **개발 전용 탈출구** — 사용자 플로우에 노출 금지 (north star) |

## 3.5 유료 lane 통제 실험 (Brave, 2026-06-11 — p5 단일 pair)

키리스 점수가 낮은 원인이 "테스트 조건"인지 "키리스 공급"인지 가르는 대조군. **결과: 공급이 원인.**

| variant | 대기업 ≥10 bodied met | bodied 평균 | listed 평균 |
|---|---|---|---|
| ddgs cn12 | 2/10 | 2.0 | 5.7 |
| ddgs cn20 | 3/10 | 2.0 | 6.0 |
| +Google RSS 보충 | 1/10 | 1.9 (래퍼 robots 차단) | 8.2 |
| **Brave (유료, 단일)** | **7/10** | **5.7** | 8.8 |

- 같은 회사·같은 게이트·같은 본문 lane에서 Brave만 ≥10 bodied 10/30 달성 → **회사들의 뉴스는 존재한다. 키리스 검색의 색인 질·결과 수가 격차의 원인.**
- Brave 결과는 publisher 직접 URL이라 본문 fetch도 정상(161 ok / 41 봇월 에러 — 키리스와 동일한 봇월 비율).
- **운영 권고**: zero-config 기본은 키리스 유지(무료 약속), 단 **무료 Brave 키(월 2k 쿼리)를 "품질 업그레이드" 경로로 문서화** — config 한 줄(`search.provider: brave`)로 이미 전환 가능. 키리스 lane의 다음 카드: Bing-first(#99, 측정 대기) + GDELT lane.

## 3.6 LLM extraction lane (D3에서 코드 반영, 2026-06-12)

| 실패 형태 | 대응 | 근거 |
|---|---|---|
| chat_once 예외 (transient 추정) | **청크당 1회 재시도 (5s 대기)** 후 UPSTREAM_ERROR | 실측 1건: p7 fullx 64청크 빌드가 chunk 39/43에서 단발 LLM 오류로 사망 — **75분 작업 전량 손실**. 빈도 데이터 부족이라 상한·대기 모두 **PROVISIONAL**; 근본 처방은 backlog #16-⑤ (chunk_hash 키 LLM 결과 캐시) |

## 4. 미해결 — 데이터 대기

- **기준 ⑤ 미달**: 대기업 본문뉴스 ≥10건 달성률 0–20% (G4 measure 3 pair). `count_news` 12→20+ 상향
  또는 보조 뉴스 쿼리 필요 — **검색 예산 trade-off라 사용자 결정 필요**.
- 검색 재시도 상한·backoff 확정 — rate-limit 발화 데이터 필요 (병렬/대량 시나리오).
- roster 검증기 키워드 스코어 임계 — false-negative 사례 1건뿐, 누적 후 처방.
