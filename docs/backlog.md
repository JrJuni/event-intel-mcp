# Backlog

장기 계획, 큰 그림, v0 scope 외 항목. 진행 중/최근 완료는 [status.md](status.md).

`status.md`와의 분리 원칙: **status는 "지금 또는 직전"**, **backlog은 "아직 안 시작 또는 의도적 defer"**.

---

## P1 — v0 진입 후 가장 먼저 검토

### #11 analyze_event_page prompt 튜닝 — Vue/React 감지 시에도 endpoint 패턴 우선
**증거**: Phase 18T Done When #4 smoke (2026-05-29) 중 tbse26.mapyourshow.com / directory.conexpoconagg.com 페이지가 본문에 `/ajax/remote-proxy.cfm?action=...` 엔드포인트 + `fetch(url, {X-Requested-With: XMLHttpRequest})` JS 코드 + `{{searchresults}}` placeholder + "No exhibitors could be found" fallback이 모두 명시되어 있음에도 analyzer가 `detected_framework=Vue` 기준으로 `operator_capture_required` (confidence 0.66~0.72) 권고.

**현재 동작**: framework=Vue/React 감지 → 본문 endpoint 패턴 무시 → capture로 escape.

**수정 방향**:
- `prompts/{en,ko}/analyze_event_page.txt` 결정 우선순위 재배열 — "framework 감지보다 본문 endpoint/placeholder 증거가 우선"을 prompt에 명시
- 또는 acquisition/analyzer.py에 deterministic pre-check 추가 (정규식으로 `/ajax/.*\.cfm`, `remote-proxy`, `searchresults` 등 강한 신호 감지 시 LLM에게 hint 주거나 LLM 우회)
- Map Your Show family는 known pattern이라 `acquisition/known_patterns.py` 신설하여 host suffix 기반 short-circuit 검토

**검증 acceptance**: tbse26.mapyourshow.com에 대해 verdict=`xhr_endpoint` + hints.candidate_endpoints에 `/ajax/remote-proxy.cfm?action=search&searchtype=exhibitorgallery` 류 항목 ≥1개.

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
