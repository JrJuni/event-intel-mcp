# Notion DB Schemas

**event-intel-mcp v0는 Notion을 사용하지 않는다.** 모든 산출물은 `outputs/{workspace_id}/{event_slug}/` 하위 yaml / markdown / json artifact로 저장된다.

이 파일은 향후 backlog #7 (bd-coldcall-agent bridge) 진입 시 참조용 placeholder.

---

## v0 Notion 미사용 결정 사유

| 영역 | event-intel-mcp v0 | bd-coldcall-agent (참고) |
|---|---|---|
| Product Context | `capability_cards.yaml` + Chroma "product_{ws}" | Notion `Customer / Product` page tree |
| Event evidence | `enriched_exhibitors.yaml` + Chroma "event_{ws}_{slug}" | 해당 없음 |
| Output (tier list) | `tier_list.md` + `tier_list.yaml` | Notion `BDINT_Teamspace` Evidence Hub DB |
| Review workflow | 사람이 yaml/md 직접 보기 | Notion 양방향 sync + promote |

v0 scope에서 Notion sync를 OOS로 둔 이유는 plan v0.5의 OOS 섹션 참조 — standalone repo + MCP-first + artifact-only 결정과 정합.

---

## 향후 bd-coldcall-agent bridge 시 (backlog #7)

bridge 진입 시 event-intel-mcp의 tier_list 결과를 bd-coldcall-agent의 `Targets` 테이블 (또는 Notion `BDINT_Publicspace`) 로 push할 수 있다. 그 경우 schema 정합 가이드를 여기 추가할 것:

- `ScoredExhibitor` → bd-agent `Target` 매핑 (fit_score → priority, tier → status, recommended_angle → bd_note 등)
- Notion property mapping (필요 시)
- sync_map 패턴 (bd-agent의 `notion_sync_map` 참조)

---

(현재 상태: placeholder. v0.4+ bd-agent bridge 진입 시 본 섹션 채움.)
