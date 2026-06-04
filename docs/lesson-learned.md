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

## [2026-06-04] `check_runtime` 4분 타임아웃의 진짜 원인 = FastMCP worker thread에서의 첫 `import chromadb` 행 (warm-up/stdout 아님)

**Tried**: Claude Desktop에서 `check_runtime(warm_up=true)`가 4분 타임아웃("서버 무응답"). 재시작해도 재현. 초기 유력 가설 2개: (C2) bge-m3 로드가 stdout을 오염시켜 stdio JSON-RPC를 깨뜨림, (warm-up) 비동기 워밍업이 응답을 막음. 외부 AI도 "warm-up 아니라 첫 호출 문제 + Chroma cold path" 방향으로 동의(코드 기반).

**Result**: 프로브로 가설을 하나씩 닫으니 **3번 뒤집힘**:
1. stdout/stderr 분리 측정 → 모델 로드 출력("Loading weights"+HF 경고)은 **전부 stderr**, stdout 0 bytes. **C2 반증.**
2. subprocess로 실 MCP 서버 stdio 관측 → 비-JSON 줄 0개(스트림 깨끗)인데 응답이 **안 옴**. C2 프로토콜 레벨도 반증.
3. **단일 콜드 `warm_up=false`도 240s 무응답** (모델 로드 0). → warm-up 무관. stderr 타임라인: Brave ping 200 OK 직후 침묵 = 다음 단계인 **product_context check(첫 `import chromadb` + `PersistentClient`)에서 행**.
4. **확정 실험**: 서버를 메인 스레드에서 `import chromadb` 먼저 한 뒤 띄우니 같은 콜드 호출이 **240s 행 → 1.8s**. 한편 단독(메인 스레드) `collection_info("product_smoke")`는 0.81s(빠름), 단순 asyncio+executor 하니스의 worker import도 정상(0.78s) — **즉 "느림"도 "아무 worker import"도 아니고, FastMCP가 sync tool을 실행하는 worker-thread 맥락 특유의 조건에서만 첫 chromadb import가 데드락**.

**Lesson**:
- **FastMCP sync tool 핸들러는 worker thread에서 실행된다.** 무거운 native dep(chromadb/onnxruntime 등)의 **첫 import를 worker thread에서** 하면 stdio 서버에서 행할 수 있다. → **무거운 deps는 `main()`에서 `app.run()` 전에 메인 스레드에서 pre-import**. (cold-start 계약은 모듈-top import 검사이므로 main() 런타임 pre-import는 위반 아님.)
- **추정으로 고치지 말 것의 교과서 사례.** 가장 그럴듯했던 stdout 오염·warm-up 가설이 둘 다 틀렸고, 진짜 원인은 "worker-thread 첫 import 행"이었다. 프로브로 하나씩 close하지 않았으면 엉뚱한 fix(타임아웃 ↑, 백그라운드 cold-init — 둘 다 worker thread라 무효)를 했을 것.
- **재현 환경이 충실해야 한다.** 단독 `collection_info` 측정(메인 스레드, 0.81s)이나 단순 asyncio 하니스(0.78s)는 데드락을 **재현 못 함 → false negative**. 유일하게 충실한 재현은 **실제 `python -m event_intel.mcp_server` subprocess**. 회귀 테스트도 반드시 subprocess로.
- **WHY 불완전해도 FIX는 확정 가능.** 데드락의 정확한 메커니즘(import lock vs onnxruntime 스레드 vs anyio)은 미확정이지만, 메인 스레드 pre-import가 고친다는 건 3중으로 실증됨. 완벽한 근본원인 규명보다 실증된 fix + 회귀 가드를 우선.

**Related**: `src/event_intel/mcp_server.py::_preimport_heavy_deps` (main()에서 chromadb + sentence_transformers pre-import; ST는 build/ingest 동일 실패모드 방어용). `tests/test_stdio_integrity.py` (subprocess 콜드 check_runtime 응답 회귀 가드, `slow`). 진단 프로브는 일회성(scratch, 삭제).

---

## [2026-06-04] 무거운 워밍업을 MCP tool 호출 안에서 *동기*로 하면 Claude app 자체 타임아웃에 걸린다

**Tried**: Phase 18T.1에서 첫 `build_event_tier_list`의 bge-m3 콜드 로드(~10-20s) 지연을 줄이려 `check_runtime(warm_up=true)`가 **동기로** `embedding_provider.warm_up()`을 호출하도록 구현(`run_preflight` 본문에서 `checks["warm_up"] = embedding_provider.warm_up()`). 터미널 측정으론 풀 check_runtime이 ~12s라 "타임아웃 안에 들어오겠지" 가정.

**Result**: 사용자가 즉시 지적 — **Claude Desktop의 MCP request 타임아웃 값은 환경마다 다르고 우리가 통제 못 함**. 콜드 디스크/리부트 직후엔 로드가 20s+로 늘어 client가 먼저 포기 → 서버는 아직 로딩 중인데 사용자에겐 opaque failure로 보임. 게다가 동기 warm은 "warm 호출이 build만큼 무거운 또 하나의 블로킹 호출"이 되어버려 문제를 옮긴 것에 불과.

sibling project **coldcall도 설계 단계에서 같은 벽**에 부딪혔고, 결론은 동일했음: 무거운 준비작업은 tool 호출 안에서 동기로 하지 말고 — (1) 호출 시 백그라운드로 *시작*만 하고 "워밍업 중, 약 N분 후 준비됨"을 **보수적 ETA**와 함께 즉시 반환, (2) 사용자(또는 agent)가 나중에 다시 호출하면 **status를 폴링**하는 비동기 잡 패턴.

**Lesson**:
- **MCP tool은 client 타임아웃을 1급 제약으로 놓고 UX를 설계**한다. "우리 측정상 X초니까 괜찮다"는 금물 — client 타임아웃은 우리가 모르고, 디스크/네트워크 상태로 출렁인다. 수 초를 넘길 수 있는 작업은 **절대 tool 호출 본문에서 동기로 블로킹하지 말 것**.
- **무거운 준비작업 = 비동기 잡 + status 폴링**. trigger 호출은 즉시 리턴(start만) + 보수적 ETA 메시지("보통 1분 이내, 최대 ~2분"처럼 실제보다 넉넉히). 같은 도구를 다시 부르면 `not_started → warming → ready/failed` 상태를 보고. 우리 구현: `runtime/warmup.py`(프로세스 전역 thread-safe 상태기계) + `check_runtime`이 항상 `checks.warm_up` 보고 + `warm_up=true`는 백그라운드 start.
- **터미널 CLI와 MCP 서버의 적정 동작이 다르다**. CLI는 한 번 실행하고 끝나는 단명 프로세스라 백그라운드 스레드가 같이 죽는다 → CLI는 **inline blocking**(`warm_up_block=True`, 사용자가 터미널에서 대기)이 맞고, 장수하는 MCP 서버는 **비동기**가 맞다. 같은 코드 경로에 `block` 플래그로 분기.
- **백그라운드 로드 + 동시 build = 캐시 중복 로드 위험** → 프로세스 모델 캐시(`BgeM3Provider._MODEL_CACHE`)를 `threading.Lock`으로 double-checked 보호. warm 스레드가 로딩 중이면 build는 같은 로드를 기다리고 재로드하지 않는다.
- **검증**: trigger가 1.27s에 status=warming 반환, 14s 뒤 폴링에서 ready(load_seconds 14.1) 확인. tool 호출은 로드를 한 번도 블로킹하지 않음.

**Related**: `src/event_intel/runtime/warmup.py` (신규), `runtime/preflight.py::run_preflight(warm_up=, warm_up_block=)`, `tools/check_runtime.py`, `providers/embedding.py::BgeM3Provider._CACHE_LOCK`. `tests/test_warmup.py` 8건(매니저 5 + 시작 훅 3) + `test_runtime_preflight.py` warm 3건. 364/364 green. (Phase 18T.1 후속.)

---

## [2026-05-29] ChatGPT OAuth → Codex backend 통합에서 5단계 누적 실패

**Tried**: ChatGPT Plus 구독을 LLM provider로 끌어다 쓰기 위해 OAuth 경로 구현. Codex CLI / OpenClaw / Warp 가 같은 방식으로 인증한다는 사용자 정보 기반으로 `auth.openai.com` PKCE flow → access_token → `api.openai.com` Responses API 호출 시도.

**Result**: 한 가지 가정으로 시작했지만 실제로는 5개의 독립적 backend 제약이 누적 발견됨 (각 단계마다 한 사이클씩 디버깅 필요했음):

1. **인증 URL state 파라미터 누락** → 로그인 직후 "인증 오류" 페이지. 처음에는 scope 문제로 의심해서 scope만 바꿔봤지만 해결 안 됨. 결국 Codex CLI (`~/.codex/`) 의 실제 PKCE 호출을 reverse하니 `state=<random>` + `originator=codex_cli_rs` + `codex_cli_simplified_flow=true` + `id_token_add_organizations=true` 다 필수였음.

2. **`api.openai.com` 거부 (401 token_invalidated)** — OAuth access_token이 정식 api.openai.com 엔드포인트에서 안 먹음. ChatGPT 구독은 별도 backend (`chatgpt.com/backend-api/codex/responses`) 를 거치며 `chatgpt-account-id` (JWT의 `https://api.openai.com/auth.chatgpt_account_id` claim에서 추출) + `OpenAI-Beta=responses=experimental` + `originator=codex_cli_rs` + `OAI-Product-Sku=codex` + `accept=text/event-stream` 헤더가 모두 필요. 한 번에 알기 어렵고 한 헤더씩 추가하며 디버깅함.

3. **추측한 모델명 거부 (400 model not supported)** — `gpt-5.1-codex-mini` / `gpt-5-codex` / `gpt-codex` 등 "Codex" 이름이 들어간 그럴듯한 변종들 모두 거부됨. 사용자가 "모델명 자체가 너무 위화감"이라 지적. 결국 `~/.codex/config.toml` + `~/.codex/models_cache.json` 에서 실제 동작 모델 확인 → `gpt-5.5` / `gpt-5.4` 둘뿐.

4. **토큰 cap 필드 전부 거부 (400 Unsupported parameter)** — Plan v3 R3 round에서 외부 AI도 "max_output_tokens 추가하라"고 권고. 실제로 추가하니 `max_output_tokens` / `max_tokens` / `max_completion_tokens` 모두 backend가 400으로 거부. payload에서 제외하고 회귀 테스트로 lock (`test_payload_omits_max_tokens_due_to_codex_backend_limitation`).

5. **`temperature` 도 거부** — 위와 같은 패턴. payload에서 제외.

각각이 한 가지 가정 (Codex CLI 패턴이라 그대로 따르면 됨) 위에 쌓인 backend-specific 제약이고, 사전에 한 번에 알 수 있는 문서는 없었음 (Codex source + 7shi/codex-oauth + numman-ali/opencode-openai-codex-auth 세 군데를 합쳐야 전모가 드러남).

**Lesson**:
- **공식 backend 통합 = SDK 우회는 거의 항상 더 비쌈**. ChatGPT Plus를 LLM provider로 끌어 쓰는 경로는 reverse engineering이지 정식 통합이 아님. 비용은 0이지만 backend 제약 변경에 무방비. 실험/prototype에만 권장, production은 정식 Anthropic/OpenAI API.
- **AI에게 모델명/API 필드 추측 시키지 말 것**. plan v3에서 외부 AI가 `max_output_tokens` 추가 권고했지만 실제 backend는 거부. 마찬가지로 처음 추측한 `gpt-5.1-codex-mini` 도 헛수고. **CLI/SDK의 실제 cache나 config 파일에서 ground truth를 먼저 확인**한 뒤 코드 작성.
- **Backend integration은 cascade 디버깅을 가정하고 들어가야**. 한 단계 fix → 다음 에러 노출 → 다시 fix 패턴. 한 사이클당 30분~1시간씩 5회 = 한나절. 시간 박스로 미리 잡아놓을 것.
- **회귀 테스트로 lock**: payload에서 빠진 필드 (`max_output_tokens`, `temperature`) 가 미래에 우연히 다시 들어가지 않도록 absence-assert 테스트 작성 필수.

**Related**: `src/event_intel/providers/llm.py::ChatGPTOAuthProvider` (commits `11ff813`, `f066e21`, `65cc407`). `tests/test_chatgpt_oauth_provider.py` 18건 (payload absence locks + SSE seen_completed + JWT account_id extract). 회복된 reusable 패턴은 `docs/playbook.md#11` (Codex backend integration recipe) 참조.

---

## [2026-05-29] `urllib.robotparser.read()` 의 silent `disallow_all=True` trap

**Tried**: Phase 18T T0.5 에서 `acquisition/robots.py` 를 stdlib `urllib.robotparser` 의 `RobotFileParser.set_url() + read()` 패턴으로 구현. "표준 라이브러리니까 robots.txt 잘 읽겠지" 가정.

**Result**: 첫 실 사이트 (`https://smarttechkorea.com/`) 호출 시 `ROBOTS_DISALLOWED` envelope 받음. 그런데 같은 URL을 브라우저나 curl로 fetch하면 robots.txt 자체는 `User-agent: * / Allow: /` (정상). 진단해보니:

- `urllib.robotparser.read()` 가 내부적으로 `urllib.request.urlopen()` 호출 → Python-urllib 기본 UA 사용
- 일부 사이트(Cloudflare 등)가 Python-urllib UA를 403 Forbidden으로 차단
- robotparser는 fetch 실패 (예외 + 4xx + 5xx 구분 안 함) 를 그냥 **`disallow_all=True`** 로 처리 — 호출자에게 fetch 실패를 알리지 않음
- 우리 코드는 robotparser 가 disallow_all 이면 ROBOTS_DISALLOWED envelope 반환
- 결과: 우리가 정책적으로 deny 받았다고 잘못 보고

**Lesson**:
- **stdlib의 high-level convenience method가 transport 실패를 silent default로 매핑하면 절대 그냥 쓰지 말 것**. 항상 transport 레이어를 분리해 status code를 직접 받아서 정책 결정해야.
- robots fetch는 별도의 HTTP 호출이고 fetch 정책도 별도다 — robots fetch 자체는 robots check를 우회해야 (논리적으로 circular). 또한 robots fetch의 UA는 실제 페이지 fetch와 같은 UA여야 정책 일관성 유지.
- 404/410 = robots 부재 = allow (RFC 9309), 401/403 = robots 숨김 = allow (관용), 5xx + transport failure = deny (보수적) — 이 매핑을 직접 코드에 박아넣을 것.

**Lesson 2 (외부 AI 협업)**: Plan v3 round 1에서 외부 AI가 같은 사이트에 대해 robots-allow 가정으로 분석해놨었는데, 실제로는 우리 코드가 ROBOTS_DISALLOWED를 받는 상태였음. 외부 AI에게 보내는 packet의 "운영 불변조건" 섹션에 "robots fetch는 stdlib robotparser로 한다" 같은 구현 디테일도 적었어야 caught됐을 듯. **외부 AI packet은 의도뿐 아니라 *현재 구현의 약한 가정*도 명시해야 corner case 발견률 향상**.

**Related**: `src/event_intel/acquisition/robots.py` (commit `f066e21` 에서 httpx 직접 fetch + status별 매핑으로 재작성). `tests/test_robots.py` 17건 (httpx mock으로 status별 매핑 직접 검증). `docs/playbook.md#12` (robots.txt 정책-decoupled fetch) 참조.

---

## [2026-05-28] `fresh_sys_modules` 픽스처의 snapshot+restore 가 pydantic 의 lazy `__getattr__` 캐싱과 충돌

**Tried**: cold-start 회귀 가드를 위한 pytest fixture 를 sys.modules snapshot 후 teardown 에 "snapshot 에 없던 모듈 전부 pop" 패턴으로 작성. S1 단독 실행 (테스트 4건) 에선 문제 없음.

**Result**: S2 의 `test_cards_*` 모듈을 추가하자 pytest collection 단계에서 pydantic 모델 import 가 일어나면서 fixture 의 snapshot 시점이 달라짐. 두 번째 cold-start 테스트 실행 시 `importlib.import_module("event_intel.mcp_server")` 가 mcp.types 의 `class JSONRPCMessage(RootModel[...])` 평가 도중 `KeyError: 'pydantic.root_model'` 로 폭사.

근본 원인: pydantic 은 `RootModel` 을 `__getattr__` lazy import 로 노출하면서 부모 패키지 (`pydantic`) 에 attribute 를 캐싱한다. 첫 lazy load 후 `pydantic.root_model` 이 sys.modules 에 들어가지만, fixture 가 teardown 에서 그걸 pop 해버리면 후속 `from pydantic import RootModel` 은 캐시된 attribute 만 반환하고 **lazy load 를 재실행하지 않는다**. 결과적으로 `pydantic.root_model` 이 sys.modules 에 없는 상태에서 `RootModel[...]` 의 `create_generic_submodel` 이 `sys.modules[created_model.__module__]` 을 조회하다 KeyError. 같은 부류 문제가 `from <pkg> import <symbol>` 패턴을 쓰는 모든 lazy-load 패키지에서 발생 가능.

**Lesson**: cold-start / import-pollution 테스트에서 "snapshot+restore" 는 위험. 차라리 **명시적으로 purge 할 prefix 만 화이트리스트** 로 두기. event-intel-mcp 의 경우 `event_intel.*` + `FORBIDDEN_HEAVY` (torch / transformers / sentence_transformers / chromadb / bitsandbytes) 만 teardown 에서 purge. pydantic / mcp / 기타 인프라 모듈은 그대로 둠. 디테일은 `tests/test_mcp_cold_start.py::fresh_sys_modules` 픽스처 docstring 참조.

**Related**: `tests/test_mcp_cold_start.py` (commit 13178e2 에서 fixture 재작성). 같은 부류를 향후 어떤 헬퍼에 또 넣고 싶을 때는 `docs/playbook.md#3` 의 cold-start guard 섹션 마지막 단락 (fixture 함정 주의) 참조.

---

## Blind Review 판정 누적

### Phase 18T.2 (무마찰 .mcpb 설치) 라운드 1 — 2026-06-04

| # | 카테고리 | 점수 | Novelty | 판정 | 사유 |
|---|---|---|---|---|---|
| 1 | architecture | 80% | 3(R1) | accepted | warm-on-start를 18T.2와 분리 커밋 (c9b8f1a / cf19080). 단, 리뷰어가 "timeout 진단 섞임"이라 한 건 부정확 — 진단은 코드 0줄, 섞인 건 warm-on-start 기능 |
| 2 | corner-case | 72% | 3(R1) | accepted | boolean form env가 .env를 shadow → "폼 체크박스 authoritative" 정책 명시(`_env.py` 주석) + 회귀 테스트(`test_form_boolean_is_authoritative_over_dotenv`). 실해는 낮음(opt-in no-op + 이 .env엔 boolean 키 없음) |
| 3 | documentation | 68% | 3(R1) | accepted | plan 스니펫의 둘째 `load_dotenv()` cwd search 제거(코드와 정합) |
| 4 | documentation | 64% | 3(R1) | accepted | 번들 버전 ≠ 패키지 버전(별도 트랙)을 `mcpb/README` 버전-범프 섹션에 명시 |

**메타**: 총평 71%. **Factual 우수**(전 항목 5점 — 외부 AI가 실제 코드 file:line 정확 인용). **Context 약함**(opt-in no-op 중립화 + 의도적 버전 분리를 모름). nit/over-engineering 0건 — 건강한 리뷰.
**종료 판정**: D(정상 다음 라운드 가능)이나 4건 모두 accept/doc·논쟁 0이라 **적용 후 종료**. Skeptic(7.5)은 A/B에서만 트리거 → 미실행. echo chamber 신호 없음(라운드 1).
**Keep 신호(외부 AI에 전달 시)**: corner-case(#2)·architecture(#1) 환영. Context 차원 보강 요청 — 다음 packet에 "opt-in env 중립화" 같은 설계 의도를 명시하면 Context 점수 향상 예상.

### check_runtime 4분 타임아웃 진단 라운드 1 — 2026-06-04

| # | 카테고리 | 점수 | Novelty | 판정 | 사유 |
|---|---|---|---|---|---|
| 1 | architecture | 84% | 3(R1) | accepted | "warm_up 아니라 첫 호출 문제" — 내 실험과 일치(warm_up=false도 행). 검증됨 |
| 2 | corner-case | 84% | 3(R1) | accepted | Chroma cold path(`collection_info`→`PersistentClient`) 지목 = 수정 타겟 확정 |
| 3 | architecture | 56% | 3(R1) | refined | "worker/event-loop 경합=보조가설·근거부족" → 실은 **주 메커니즘**(worker-thread chromadb import 행, 실증). 코드-only라 저평가 |
| 4 | corner-case | 48% | 3(R1) | refined | "collection_info 단독 측정 >30s면 범인" → 메인 스레드라 0.81s **false negative**. 올바른 판별자는 subprocess(worker) — 정정 후 채택 |

**메타**: 총평 68%. **위치(WHERE) 정확**(Chroma cold path, warm_up 아님 — 둘 다 실험 일치) / **Calibration 약함**(실증된 주 메커니즘을 보조로 저평가 + false-negative 테스트 제안). "fresh server + single-call" 권고는 정확.
**종료 판정**: D이나 진단은 리뷰가 아니라 **실험으로 closed** → 수정 진입. Skeptic 미실행(A/B 아님). 수정: `_preimport_heavy_deps`(commit), 회귀 가드: `test_stdio_integrity.py`.
**핵심 교훈(외부 AI 협업)**: 코드-only 리뷰는 "WHERE"는 잘 짚지만 "HOW(데드락 vs 느림)"는 실험 없이 못 가른다. 리뷰어의 false-negative 테스트 제안을 그대로 따랐으면 chromadb를 무죄방면할 뻔 — **제안 테스트도 1:1 검증 대상**.

