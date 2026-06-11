# p5 진단 — gold target 5사 누락 원인 (2026-06-11)

run `big_data_ldn_2026-20260611125128-68d1b8ea` (measure_llm.json: tgt_cov 5/10)에서
누락된 gold target 5사: count technologies ltd / euno / astronomer inc / celebal technologies / lakefs.

## 방법

run이 166사 추출 목록을 영속하지 않아, 동일 조건 replay로 분리:

1. **청킹 재현(결정론, LLM 0회)**: `capture_source(csv_file)` + `_split_chunks(max_chars=8000)`
   → **5 청크 (cap 12 미달, truncation 없음)**. roster 179사 전원 + 누락 5사 전원이 cap 안에서 extraction에 노출.
2. **extraction + triage replay(OAuth gpt-5.5, 무료)**: 원 run과 동일 모델·프롬프트
   (provider=chatgpt_oauth일 때 factory가 model 파라미터를 무시하므로 원 run의 extraction도 실제로는 gpt-5.5).

## 결과

| 단계 | 결과 |
|---|---|
| extraction | 162/179 추출 (원 run 166 — 비결정 jitter). **누락 5사 전원 추출됨** |
| triage (30/162, 2 calls) | **누락 5사 전원 triage에서 탈락** |

- triage 선별 위치 분포: median 50 (0..145 전 구간) → 단순 first-N tie-break 아님, 실제 점수 선별.
- 선별 30사 면면: snowflake 본인 + Databricks/Dremio/Starburst/Firebolt/ClickHouse/Cloudera 등
  **직접 경쟁사·대형 플랫폼 벤더 위주**. euno(추출 idx 3), celebal(idx 25)처럼 앞쪽에 있어도 탈락.

## 결론

**p5 병목은 extraction이 아니라 triage 선별 기준.** "제품 도메인 관련도" 채점은 제품과 가장
*닮은* 회사(=경쟁사·동종 플랫폼)를 상위로 올리고, 실제 BD 타깃(고객형 — 소형 데이터 도구·컨설팅:
Astronomer, lakeFS, euno, Celebal, Count)을 밀어낸다. 경쟁사 통과는 설계 의도(패널티는 본
스코어링 책임)지만, 고객형 타깃이 lookalike에 **밀려나는** 건 의도 밖 — capability_fit 평탄성과
별개의, triage 깔때기 고유의 선택 편향.

처방 후보(차기 phase, D3에서는 진단만): triage 프롬프트를 "도메인 관련도" 단일 축에서
"target_mode 기준 잠재 고객/파트너 적합" 축으로 보강하거나, 경쟁사-lookalike와 고객형을
분리 쿼터로 선별. **스코어링 로직 변경이므로 자율 변경 금지 — 사용자 결정 사안.**
