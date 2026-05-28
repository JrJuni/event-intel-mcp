# Lessons Learned

Append-only log of approaches tried, failure causes, and validated know-how, accumulated by date. **Failures only** — successes that became reusable patterns belong in [playbook.md](playbook.md).

## Entry format

```
## [YYYY-MM-DD] One-line topic

**Tried**: which approach was taken
**Result**: success / failure + observed behavior
**Lesson**: what to do next time
**Related**: file paths / commit hashes / linked playbook entry
```

---

## [2026-05-28] `fresh_sys_modules` 픽스처의 snapshot+restore 가 pydantic 의 lazy `__getattr__` 캐싱과 충돌

**Tried**: cold-start 회귀 가드를 위한 pytest fixture 를 sys.modules snapshot 후 teardown 에 "snapshot 에 없던 모듈 전부 pop" 패턴으로 작성. S1 단독 실행 (테스트 4건) 에선 문제 없음.

**Result**: S2 의 `test_cards_*` 모듈을 추가하자 pytest collection 단계에서 pydantic 모델 import 가 일어나면서 fixture 의 snapshot 시점이 달라짐. 두 번째 cold-start 테스트 실행 시 `importlib.import_module("event_intel.mcp_server")` 가 mcp.types 의 `class JSONRPCMessage(RootModel[...])` 평가 도중 `KeyError: 'pydantic.root_model'` 로 폭사.

근본 원인: pydantic 은 `RootModel` 을 `__getattr__` lazy import 로 노출하면서 부모 패키지 (`pydantic`) 에 attribute 를 캐싱한다. 첫 lazy load 후 `pydantic.root_model` 이 sys.modules 에 들어가지만, fixture 가 teardown 에서 그걸 pop 해버리면 후속 `from pydantic import RootModel` 은 캐시된 attribute 만 반환하고 **lazy load 를 재실행하지 않는다**. 결과적으로 `pydantic.root_model` 이 sys.modules 에 없는 상태에서 `RootModel[...]` 의 `create_generic_submodel` 이 `sys.modules[created_model.__module__]` 을 조회하다 KeyError. 같은 부류 문제가 `from <pkg> import <symbol>` 패턴을 쓰는 모든 lazy-load 패키지에서 발생 가능.

**Lesson**: cold-start / import-pollution 테스트에서 "snapshot+restore" 는 위험. 차라리 **명시적으로 purge 할 prefix 만 화이트리스트** 로 두기. event-intel-mcp 의 경우 `event_intel.*` + `FORBIDDEN_HEAVY` (torch / transformers / sentence_transformers / chromadb / bitsandbytes) 만 teardown 에서 purge. pydantic / mcp / 기타 인프라 모듈은 그대로 둠. 디테일은 `tests/test_mcp_cold_start.py::fresh_sys_modules` 픽스처 docstring 참조.

**Related**: `tests/test_mcp_cold_start.py` (commit 13178e2 에서 fixture 재작성). 같은 부류를 향후 어떤 헬퍼에 또 넣고 싶을 때는 `docs/playbook.md#3` 의 cold-start guard 섹션 마지막 단락 (fixture 함정 주의) 참조.

