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

## [2026-06-05] capability_fit가 모든 top-k kind를 평균 → 경쟁사가 자기 competitor 청크와 가까워 fit이 부풀었다 + category_fit substring 오탐

**Tried**: `rag/retriever.py`가 `capability_fit = avg(top_k 전체 히트의 cosine)`. `scoring/dimensions.py::score_category_fit`는 needle 토큰을 `n in haystack`(부분문자열)로 카운트.

**Result**: 둘 다 변별을 망침.
1. capability_fit: 단방향 검색 top-k는 capability뿐 아니라 competitor/bad_fit/summary 청크도 포함. 경쟁사(Snowflake)는 제품 카드의 `competitor:Snowflake` 청크와 의미적으로 매우 가까워, 그 높은 유사도가 평균에 섞여 **capability_fit 0.62(최상위)**. 정작 진짜 타깃 LlamaIndex는 capability 청크 2개로 0.56. **경쟁사 > 타깃**.
2. category_fit: needles에 geo "us", 산업 토큰 "ai" 등 짧은 토큰이 있는데 substring 매칭이라 "us"⊂"business", "ai"⊂"chair"처럼 무관 텍스트에 적중 + 불용어(and/or/the) 전부 매칭 → category_fit이 거의 모두에게 과대.

**Lesson**:
- **유사도 평균은 같은 종류(kind)끼리만.** capability_fit은 `kind=="capability"` 히트만 평균(없으면 0). competitor/bad_fit는 카운트로 penalty에만 기여. 그래야 "경쟁사 청크에 가까움"이 fit을 올리지 않고 penalty로 간다. (1-hit vs 3-hit 평균 동급 취급하는 표본수 편향은 알려진 한계 — count-weighting deferred.)
- **토큰 매칭은 substring 아니라 토큰 경계(집합 교집합).** 짧은 토큰은 불용어 제거 + 약어/지역 whitelist(ai/ml/us/eu…)로만 허용 — 길이<3 일괄삭제는 AI/ML/5G/US를 날리므로 금지.

**Related**: `rag/retriever.py`(capability-only 평균), `scoring/dimensions.py::_category_needles`/`score_category_fit`(토큰경계+불용어/whitelist). 테스트: `test_rag_ingest_retrieve.py`(capability-only 평균·전부-competitor→0), `test_scoring.py`(substring/불용어 오탐 방지·약어 매칭). Phase 18U Step 3.

---

## [2026-06-05] trafilatura는 전시회 디렉터리에서 헤딩·링크를 버린다 (회사명=도메인, url=None의 진짜 원인) + 캐시는 버전을 가져야

**Tried**: HTML 출품사 리스트를 `source_capture._strip_html`(trafilatura `extract(..., favor_recall=True)`)로 텍스트화. 회사명은 `<h2>`, 공식 URL은 `<a href>`에 있었다.

**Result**: 추출된 name이 **도메인**("llamaindex.ai")이고 `url=None`. 원인 = trafilatura 2.0.0이 **article 본문 추출기**라 헤딩과 링크를 boilerplate로 간주해 **둘 다 버린다**(`include_links=True`/`output_format="markdown"`/xml/html 다 시도 — 전부 `<p>`만 남김). GTC 34곳 페이지에선 trafilatura가 `<p>` + 앵커 텍스트(도메인)만 남겨, 회사 식별 토큰이 도메인뿐 → LLM이 도메인을 name으로 집음. 동시에 news 쿼리가 `"llamaindex.ai"`(도메인) 정확구문이라 recall도 바닥(166 vs 도메인 9). 추가로 **검색 캐시 키에 버전 성분이 없어**(`sha1("{kind}|{lang}|{query}")`) news 파서를 고쳐도 옛 빈-news JSON이 재사용될 위험.

**Lesson**:
- **도구를 용도에 맞게.** trafilatura는 기사용 — 디렉터리/리스트(헤딩=이름, 링크=URL이 곧 콘텐츠)엔 부적합. 링크가 여럿(`_DIRECTORY_MIN_LINKS`)이면 **구조보존 stdlib strip**으로 라우팅: 헤딩을 자기 줄로, `<a href>`를 `text (url)`로 보존. 블록 태그→개행으로 인접 항목이 붙지 않게.
- **identity(이름/URL)가 깨지면 하류 전부 오염**: news recall↓, 공식 URL을 웹검색으로 추정(엉뚱한 하위페이지), 점수 변별 불가. **튜닝(가중치/penalty)은 identity 정정 후에.**
- **on-disk 캐시·resume는 파싱 의미 버전을 키/행에 담을 것.** `ENRICH_CACHE_VERSION` prefix + resume 행 stamp → 파서 bump 시 stale 자동 무효(수동 rm 의존 제거).

**Related**: `events/source_capture.py::_structured_strip_html`/`_strip_html`(링크 라우팅), `events/extraction.py` 프롬프트(헤딩명·url), `events/enrichment.py::ENRICH_CACHE_VERSION`+`_SearchCache._key`+`load_done`. 테스트: `test_source_capture.py`(헤딩/href 보존), `test_enrichment.py`(캐시버전·비-기사 드롭·published_at). Phase 18U Step 2.

---

## [2026-06-05] Brave `/news/search`는 결과가 최상위 `results` — web 모양 가정 파서가 전원 news=0 (가짜 provider 테스트가 버그 통과시킴)

**Tried**: `providers/search.py::BraveSearchProvider._parse`가 news/web 공용으로 `data.get(kind, {}).get("results", [])` (news는 `data["news"]["results"]`)를 읽었다. 실사용 검증(MongoDB×GTC 34곳)에서 **전원 `news_count=0`**.

**Result**: 실패. Brave `/web/search`는 `{"web": {"results": [...]}}`이지만 `/news/search`는 **`{"type":"news","query":..,"results":[...]}` — 결과가 최상위 `results`**. 파서가 `data["news"]`를 찾으니 항상 `[]`. 단독 프로브로 확인: `q="Snowflake"` news 5건 정상인데 파이프라인은 0건. news=0 → evidence_floor가 2(url+news)에 못 미쳐 **S 티어 원천 불가**, A도 buying_signal 0으로 점수 미달. 수정(news=최상위 `results`, web=중첩) 후 동일 데이터 news 0→166, A 0→21.

**Lesson**:
- **외부 API 응답 모양은 엔드포인트마다 다르다 — 추측 말고 실제 payload로 단언하는 contract 테스트를 둘 것.** `tests/test_search_provider.py`는 실제 Brave news/web 응답 모양을 sample payload로 박아 `_parse`를 검증(네트워크·키 불필요).
- **가짜 provider만 쓰는 통합 테스트는 실제 파싱 모양을 검증하지 못한다.** enrichment 테스트가 `SearchProvider`를 monkeypatch한 가짜로 돌려서 진짜 `_parse`가 한 번도 실 응답 모양과 대조되지 않았고, 버그가 출시까지 통과했다. → 외부 경계(파서/직렬화)는 가짜 뒤에 숨기지 말고 별도 단위로 고정.
- **버그가 다른 단계의 천장으로 위장된다.** "S가 안 나온다"는 스코어링 문제처럼 보였지만 원인은 enrichment 파서였다. tier 분포 이상은 상류(파서/추출)부터 의심.

**Related**: `src/event_intel/providers/search.py::_parse` (+ `_parse_published`). `tests/test_search_provider.py` 4건. Phase 18U Step 1.

---

## [2026-06-04] MCP 서버는 임의의 cwd로 spawn된다 — 모든 파일 경로는 절대경로여야 (cwd 상대 금지, 두 번 물림)

**Tried**: `build_event_tier_list`이 출력을 `Path("outputs")`(cwd 상대)에 썼다. 터미널 CLI는 cwd=repo라 `repo/outputs`에 잘 써졌음.

**Result**: Claude Desktop에서 build 호출 시 `PermissionError: [WinError 5] 액세스가 거부되었습니다: 'outputs'`. Claude Desktop은 MCP 서버를 **repo가 아닌 임의 cwd**(예: Program Files)로 spawn하는데, 거기서 상대 `outputs` mkdir이 권한 거부됨. (같은 세션에서 `.env` 로딩도 동일 원인 — cwd 의존 → 패키지 위치 역산으로 이미 수정했었음. **같은 클래스에 두 번 물림.**)

**Lesson**:
- **MCP stdio 서버의 cwd는 통제 불가**(클라이언트가 정함). CLI(cwd=repo)에선 안 드러나고 Claude Desktop에서만 터진다. → **서버가 만지는 모든 파일 경로는 절대경로**여야: (a) 패키지 위치 역산(`Path(__file__).resolve().parents[N]`, `.env`/`outputs`가 채택) 또는 (b) `~/.event-intel/`(chroma/artifacts/cache/resume가 채택) 또는 (c) env override. **cwd 상대 `Path("...")`는 서버 코드에서 금지.**
- **CLI 통과 ≠ MCP 통과.** 같은 핸들러라도 실행 환경(cwd, 메인 vs worker 스레드)이 달라 CLI에서 멀쩡한 게 Claude Desktop에서 터진다. 검증은 반드시 실제 MCP 경로 또는 최소한 foreign-cwd에서.
- **회귀 가드**: `monkeypatch.chdir(tmp_path)`로 foreign cwd 흉내 → 경로가 cwd 밑이 아님을 단언(`tests/test_output_path.py`). 같은 패턴을 새 파일-쓰기 코드마다 적용.

**Related**: `src/event_intel/tools/build_event_tier_list.py::_outputs_base` (패키지 역산 repo/outputs + EVENT_INTEL_OUTPUT_DIR override). `src/event_intel/_env.py` (동일 클래스 선례). `tests/test_output_path.py` 3건.

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

## [2026-06-08] acquisition ladder e2e fixture의 JSON roster가 <1KB면 short-body operator heuristic에 먹힌다

**Tried**: C7 ladder의 HCR e2e 테스트에서 bundle→endpoint가 반환하는 JP JSON roster fixture를 8개 회사(~300자)로 만들었다. 번들 discovery·구조 validator는 단독 호출에서 통과(score 0.608)했는데, **전체 acquire 경로에서는 ACQUISITION_AMBIGUOUS로 떨어지고 결국 operator로 종료**.

**Result**: 실패. 원인은 `http_status_map.map_http_response`의 short-body 규칙: `200 + body < 1024 bytes + script/endpoint 힌트 없음 → OPERATOR_CAPTURE_REQUIRED`. JSON roster 본문에 `fetch(`/`axios`/`.json`/`api/` 같은 힌트 토큰이 없으니 inert shell로 오판 → probe가 should_proceed=False로 점수 0 → winner 없음. validator를 의심하느라 시간을 썼지만 validator는 정상이었고, 진짜 게이트는 그 **앞단의 status 매핑**이었다.

**Lesson**:
- **probe/acquire를 합성 fixture로 e2e 테스트할 때 응답 본문은 1KB를 넘겨라.** 실 HCR roster는 ~577KB라 현실에선 안 걸리지만, 작은 fixture는 short-body heuristic을 밟는다. (실데이터 크기를 fixture 주석에 명시해 의도를 남길 것.)
- **단계별 단독 검증이 통과해도 파이프라인 e2e는 그 사이의 게이트(여기선 status 매핑)를 또 밟는다.** "validator 통과 = 경로 통과"가 아니다 — 실패를 만나면 의심을 한 모듈에 고정하지 말고 호출 순서상 **앞단부터** 확인.

**Related**: `acquisition/http_status_map.py`(short-body 규칙), `tests/test_acquire.py::test_ladder_hcr_e2e_operator_prior_bundle_to_json`(fixture를 40개 회사로 상향 + 주석). C7 (commit 2411c2a).

---

## [2026-06-08] CliRunner `--help` substring 단언은 터미널 폭(CI 80 col)에서 깨진다

**Tried**: WSL W3에서 `draft-cards --from-workspace` 플래그가 help에 나오는지 `assert "--from-workspace" in res.output`로 단언. 로컬(넓은 터미널)에선 통과.

**Result**: 실패 — CI(pytest job)에서 빨강. rich가 옵션명을 **80 col 경계에서 줄바꿈**하고 ANSI 코드를 끼워넣어, 렌더된 출력에서 `--from-workspace`가 연속 substring이 아니게 됨(`\x1b[...]`로 토막). 로컬은 폭이 넓어 한 줄이라 통과 → 환경 의존 green. 1 CI 라운드 소모.

**Lesson**:
- **CLI `--help` 렌더 텍스트를 substring으로 단언하지 마라.** rich/Click 출력은 터미널 폭·색상에 의존한다. 정 필요하면 `CliRunner().invoke(app, [...], env={"COLUMNS":"200"})`로 폭을 넓히고 `re.sub(r"\x1b\[[0-9;]*m","",out)`로 ANSI를 벗긴 뒤 단언.
- **플래그 존재는 동작으로 증명하는 게 더 견고**: 상호배타 위반→exit 2 같은 behavior 테스트가 "플래그가 파싱된다"를 폭 무관하게 증명한다. help 텍스트 단언은 보조일 뿐.

**Related**: `tests/test_workspace_draft.py::test_cli_draft_help_lists_from_workspace`(COLUMNS=200 + ANSI strip). PR #42 (commit 5b31cef).

---

## Blind Review 판정 누적

### Y2.0 아키텍처 게이트 초안 라운드 1 (ChatGPT/Codex) — 2026-06-09

리뷰 대상: `~/.claude/plans/y2-architecture-gate.md` v0.1(원격 배포 결정 게이트, 코드 0줄). 사용자가 타깃을 "소규모 팀 + Anthropic API/OpenAI API/OpenAI OAuth"로 확장하며 리뷰 동반. 7건 전부 HEAD 대조 후 수용 → v0.2.

| # | 카테고리 | 판정 | 사유(HEAD 대조) |
|---|---|---|---|
| 1 | architecture | accepted | D1 binary(private/shared)→**3-tier**(personal-local/small-team single-tenant/public). 사용자 scope 확장이 중간 단계를 요구 |
| 2 | architecture | accepted | 3축 분리 명문화(tenancy/client/provider). v0.1이 D2 lean에 provider 새 흘림. OpenAI API provider lane 추가(미구현 — `make_llm_provider`는 Anthropic/OAuth 2개만) |
| 3 | corner-case(보안) | accepted | OpenAI OAuth≠OpenAI API. **우리 코드 독스트링이 직접 "personal local only, 공유 서버 배포 금지"(llm.py:148)** → 원격 disable. 리뷰가 코드 정확 인용 |
| 4 | corner-case(보안) | accepted | D8 신규 — 원격 tool surface allowlist(12도구 분류). 미세정정: `storage migrate`는 CLT(도구 아님)라 비대상 |
| 5 | architecture | accepted | D9 신규 — data governance(source/artifact/Chroma/log owner·retention·delete·export). WSL provenance로 팀 접근권한 부상 |
| 6 | documentation | accepted | Sources/spec evidence 섹션(OpenAI/MCP/Anthropic 공식 링크 + protocol pin 후보). "코드 0줄" 유지하되 근거 박음 |
| 7 | architecture | accepted | D10 신규 — billing/quota(server-key vs team BYOK vs per-user + rate/budget/request-id) |

**메타**: architecture/governance 통찰 우수 + **코드 file:line 정확 인용 + 공식 spec 링크** = 근거 최상. nit/style 0. 약점=`storage migrate` 원격 도구 오인(경미). **사용자 scope 확장(소규모 팀+멀티 provider)이 v0.1 전제를 깬 갭을 정확히 포착.**
**판정**: 라운드 1 — 전부 accepted, 게이트 7→10 결정으로 확장(scope 정당). v0.2 재합성(`Changes v0.1→v0.2` 표 + 인라인 마커). skeptic 미실행(라운드 1·draft). 다음: 원하면 라운드 2(small-team 최소 구현 경계 + 12도구 allowlist 실매핑 집중), 아니면 Y2 착수 세션 입력으로 사용.

### WSL plan v0.2 + 로드맵 라운드 1 (Codex) — 2026-06-08

리뷰 대상: Workspace & Source Library RAG plan v0.2 + 큰그림 로드맵. 5건 전부 HEAD 대조 후 처리.

| # | 카테고리 | 판정 | 사유 |
|---|---|---|---|
| 1 | architecture | **refined** | WSL을 로드맵 순서에 명시(bench 정리 후·holdout 전 DEV 품질개선). **단 리뷰의 "capability_fit 평탄(Y1D) fix" 연결은 과장** → "카드초안/rationale 품질개선이지 capability_fit 직접 fix 아님"으로 정정 수용 |
| 2 | corner-case | accepted | holdout 진입 전 `threshold-freeze --gates-file` 완전 재freeze를 **차단 hard-gate**로 격상(권장 순서 + Y1 Gate 양쪽). 불완전 thresholds로 holdout 측정 금지 |
| 3 | documentation | accepted | 로드맵 Context의 stale 수치(도구 수·테스트 수·P@10) 제거 → "현황은 status.md 단일 출처"(R1#5 재적용) |
| 4 | architecture | **deferred** | Y2.0에 target client matrix 선결 추가("Anthropic API 전제"는 잠정 가정). 비긴급 → Y2 착수 시 사용자 확정(Y2.0 결정으로 기록, 지금 미착수) |
| 5 | corner-case | accepted | WSL W4 run_summary에 `source_index_fingerprint` 기록(이미 cards_fingerprint 있음) → "어떤 product+source 상태로 측정했나"가 재현성에 포함 |

**메타**: roadmap 위생 + holdout hard-gate 중심. corner-case 2·architecture 2·doc 1, nit/style 0. #1의 Y1D 연결 과장만 정정, 나머지 정확. echo chamber 아님(신규 영역=배치순서·forward-requirement).
**판정**: 라운드 1로 종료(skeptic 생략 — plan이 코드 아닌 로드맵 위생 수준, 신규 P1 없음). v0.2 재합성 후 사용자 승인 → W0 착수.

### Y1 실행 plan 라운드 1+2 (v1→v2) — 2026-06-08

외부 AI: Codex. 측정 인프라 plan이라 "성능이 안 좋아져도 게이트가 통과하는" silent-validity 결함에 집중 요청.

| # | 카테고리 | 판정 | 사유(HEAD 대조) |
|---|---|---|---|
| R1-1 | architecture | accepted | CS4가 gold 받아 즉시 join → blind 경계 위반. `run/measure` 파일·프로세스 분리 |
| R1-2 | architecture | accepted | run-result packet은 추출 회사만 → 미추출 분모 불가. 상태머신 9단계로 |
| R1-3 | corner-case | accepted | `precision_at_10`이 `/len(ranked)`(metrics.py:58) → 추출실패가 precision↑. `/10`으로 |
| R1-4 | corner-case | **refined** | full-roster 분모 유지 + 3분리 명명. 원안(scored 분모 축소)은 reject(선택-누락 누수 은닉) |
| R1-5 | corner-case | accepted | P6=partner인데 D6가 competitor 게이트(metrics.py:71 partner=neutral, defaults:60 factor 0.0). **계약 버그** → P6 N/A |
| R1-6 | architecture | accepted | SHA freeze≠재현(LLM/Brave/Chroma 미저장). one-shot 명명 + replay corpus 슬라이스 신규 |
| R1-7 | corner-case | accepted | universe cap `0`: extraction 0=빈추출(extraction.py:351) / enrichment 0=30(enrichment.py:534) **불일치**. 0 금지 |
| R1-8 | corner-case | accepted | 날짜 dir 덮어쓰기 + cards SHA≠collection. run_id/fingerprint 분리 + Chroma receipt |
| R2-결정2 | corner-case | accepted | **내 Step6 자기모순 적발**: "full-roster 분모 유지" + "미추출 100건 비희석" 테스트는 모순. 3지표 분리(end_to_end/conditional/coverage)로 해소 |
| R2-1~5 | architecture/corner-case | accepted | 상태머신(packet≠sealed labels) · evidence packet은 company labels 봉인 후 · holdout 순서(freeze→packet→seal→reveal) · run_id(고유)/fingerprint(결정적) · Chroma receipt(런타임 re-hash 대신 ingest 시) |

**메타**: Codex가 실제 코드 file:line 정확 인용(8/8 Factual 최상). **corner-case·architecture 발견 최상, nit/style 0.** R2는 내 제안의 자기모순까지 잡음(높은 novelty). echo chamber 아님.
**판정**: 라운드 1+2 모두 강한 신호 → v2 재합성. **계약 수정 동반**(D6 competitor 게이트 P5만 + README universe 0 금지).

**Step 7.5 Skeptic (다른 벤더 Gemini, context-starved — 라운드 히스토리/판정 제외)**:
- 시작 라인: **"다음 점에서 다릅니다"**(3분리 메트릭·상태머신·SHA 봉인 정확 식별) → echo 신호 없음.
- 발견 3건: **SK-1 accepted·신규**(1:1 강제가 정당한 1:N 부스를 miss 처리 — Codex 2라운드가 못 봄, match cardinality taxonomy로) / **SK-2 refined**(evidence packet 내용 미정의 → item 스키마 명시; "cross-rank 합성" 프레이밍은 evidence_precision 오해) / **SK-3 rejected**(replay를 live Brave로 오독 — contract-replay는 고정 fixture 재생).
- 판정: 🔁 **종료 보류 + 반영** → v3. skeptic이 값 함(신규 corner-case 1건). 다음 phase는 mutual blind spot 대비 페어 로테이션 권장.

**라운드 3 (Codex, v3 대상)**: 6건 전부 valid(5 P1 + 1 P2) — **수정의 2차 버그**. R3-1 holdout 순서 자기모순(evidence packet이 run·labels보다 먼저 — **내가 v3에 직접 쓴 모순**) → hidden run 도입 / R3-2 manual match가 labels보다 먼저면 extracted 노출 → match를 봉인 후로 / R3-3 full roster packet vs "P5/P6만 완전라벨" 충돌 → pair별 cohort(top-10+decoy) / R3-4 1:N covered가 scoring entity 없는 회사까지 부풀림(SK-1이 과관대) → materialize만 covered / R3-5 evidence 1건 1.0 통과 → min_items+yield / R3-6 receipt ts가 자기 테스트와 모순 → instance/fingerprint 분리. Open Q 전부 정교화(Q3: **P1 GTC는 18U 튜닝 오염 → blind holdout 불가**, 내가 packet에 self-flag한 빈틈을 독립 확인). 결정 A=P4 zone 층화 competitor holdout.

### 라운드 추이 — Y1 실행 plan
| 라운드 | 벤더 | Plan | 신규성 | 종료 판정 |
|---|---|---|---|---|
| 1 | Codex | v1→v2 | (N/A) 8/8 valid | 정상 다음 |
| 2 | Codex | v2 | 높음(내 자기모순 적발) | 정상 다음 |
| Skeptic | Gemini | v2→v3 | 신규 1(SK-1 1:N) | 반영 후 속행 |
| 3 | Codex | v3→v4 | 높음(수정의 2차 버그 6) | ✅ **루프 종료** |
**진단**: 3라운드+skeptic 모두 silent-validity 결함에 수렴, nit/style 0 — 건강한 리뷰. 패턴: **각 라운드가 직전 수정의 edge를 잡음**(R2가 내 모순, skeptic이 1:N, R3가 holdout 순서·SK-1 과관대·receipt 모순) — 정교한 측정 프로토콜 설계의 자연스러운 수렴. **2회나 내 자기모순을 외부가 잡음 → 측정 인프라는 외부 리뷰 가치 큼.** skeptic(다른 벤더)이 같은 벤더 2라운드가 못 본 1:N을 잡아 페어 로테이션 가치 실증. v4에서 bounded edge만 남아 종료, 잔여는 구현 adversarial 테스트로.

**산출**: `~/.claude/plans/y1-execution-v4.md`(리뷰 종료, 구현 진입본. 인라인 마커 + Considered/Rejected + Changes v1→v2→v3→v4). v1/v2/v3 audit trail 보존.

### Y1 라벨링 시스템 plan 라운드 1+2 (v1→v3) — 2026-06-08

외부 AI: Codex. 멀티벤더(GPT-OAuth 초안 → Claude 검색보완) gold 생산 시스템 plan. "독립 gold 계약 vs AI 자동라벨·게이트 동작 충돌"에 집중.

| # | 카테고리 | 판정 | 사유(HEAD 대조) |
|---|---|---|---|
| R1-1 | architecture | accepted | 엔진·Stage A 둘 다 GPT-OAuth + 비플래그 자동채택 → 독립성 위반. **silver(단일벤더)/gold(교차합의·사람·검색) 분리**, holdout=gold만 |
| R1-2 | corner-case | accepted | `benchmark.py:226` `passed()=not gate_failures()` + N/A/insufficient_n=passed=None → **미측정 required가 통과로 둔갑**. L0 ineligible 도입 |
| R1-3 | corner-case | accepted | freeze가 universe={} + class-coverage 게이트 없음 → v4 계약 미반영. 재freeze |
| R1-4 | corner-case | accepted | gold 폴더 `labeling_sheet.json`에 원문 개요(README는 회사명+라벨만) → gitignore 로컬 분리 |
| R1-5 | documentation | accepted | 로드맵 v3 Context가 stale(479 tests/Y1 미착수) → 방향만 보존, 현황 status 위임 + Y2 transport/세션 |
| R2-1 | corner-case | accepted | grade가 `SealedLabels`(name→str)·measure까지 안 감 → holdout이 silver 거부 못함. **행단위 {label,grade,source,adjudicators}** + holdout measure 검증 |
| R2-2 | corner-case | accepted | 전역 required면 partner competitor·evidence 비대상이 ineligible → manifest pair별 **required/optional/not_applicable** |
| R2-3 | architecture | accepted | **P4는 이미 라벨/분포(competitor=3) 봤음 → 재freeze로 holdout 독립성 복구 불가**. P4→DEV 강등, 새 blind pair 사전지정 |
| R2-4 | corner-case | accepted | waiver→pass면 실제 통과와 구분 불가 → 상태 `pass/fail/ineligible/waived` + 사유·승인자·시각 |
| R2-5 | corner-case | accepted | 교차합의 gold는 2nd 라벨러가 GPT 제안 안 봤음 증명 필요 → input/prompt SHA·model ID provenance |
| R2-6 | documentation | accepted | 기존 sheet/ai_labels 로컬 이동 migration 없음 → 이동 + `git check-ignore` 테스트 |

**메타**: 2라운드 11건 전부 valid(oh/컨벤션충돌 0). 강점=계약을 **plan 산문이 아니라 코드 경계(sealed/measure/manifest)로 강제하라**는 corner-case + `benchmark.py:226` 정확 인용. R2가 v2의 "산문 수준 fix"를 정확히 재지적(높은 novelty). **가장 중요(R2-3)**: 라벨을 본 pair는 재freeze로 holdout 자격 복구 불가 — Y1 실행 plan의 Q3(P1 GTC 오염)와 동형 패턴, 이번엔 P4. echo chamber 아님.
**판정**: 라운드 1+2 모두 강한 신호 → plan v3 재합성(인라인 마커). **L0(게이트 적격성)은 선행 코드 fix로 분리 착수**(commit `cd1b6a2`, 617 passed). 잔여 L1–L6은 v3 슬라이스로 구현.
**핵심 교훈(반복 확인)**: 측정 인프라는 외부 리뷰 가치가 크다 — 이번에도 "미측정=통과" silent-validity 결함(R1-2)과 "본 pair는 holdout 불가"(R2-3)를 외부가 잡음. 계약은 문서가 아니라 **타입/검증 경계로 강제**해야 새지 않는다.

### Agentic Acquisition Ladder impl plan 라운드 1 (design v2→v2.1) — 2026-06-08

| # | 카테고리 | 판정 | 사유 |
|---|---|---|---|
| 1 | architecture | accepted | analyze_page(analyzer.py:236)가 자체 fetch(273) → "landing 1회" 모순, budget/결정성 어긋남. v2.1 §A: `analyze_response` 분리 + landing 공유 |
| 2 | corner-case | accepted | early-exit이 landing/파생후보 미구분 → 악성 bundle 하나가 전체 ladder 중단. v2.1 §B 오류 matrix(기존 probe candidate-local skip을 ladder로 일반화) |
| 3 | corner-case | accepted | validator가 `{"products":[{"product_name"}]}`를 roster 오인. v2.1 §C: bounded nested + 회사-특정 신호 + negative 4종 |
| 4 | corner-case | accepted | 총호출만 cap → 잘못된 prior가 예산 소진해 bundle rung 미도달. v2.1 §D: per-rung quota + bundle rung 최소예약 + cumulative byte |
| + | corner-case | accepted | winner=채점 response 보존 확정(§E) + 민감값(Authorization/Cookie/token) redaction(§F) |

**메타**: 8개 중 3 closed/4 partial/1 deferred로 정밀 진단 — 표면 동의가 아니라 "실행 계약 기준" 미충족분을 정확히 짚음. 전부 Factual 최상(analyzer.py:273 자체fetch·probe.py:222 candidate-local skip 확인). rejected 0. **2라운드 연속 코드기반 corner-case 우수** — 같은 외부 AI(Codex)지만 신규성 유지(design→impl로 대상 이동, 새 충돌면 발견). echo 신호 없음. **교훈**: 내 impl plan이 "landing 1회"를 적었지만 analyze_page 분리를 설계 안 해 실제론 2-fetch — *계약 문장과 실제 호출 경로의 정합*을 plan 단계에서 grep으로 검증했어야. 리뷰어 권고대로 full 재검토 없이 v2.1로 4건 closure 후 구현 진입.
**Skeptic(7.5)**: 미실시(사용자가 closure 후 구현 지시 흐름).

### Agentic Acquisition Ladder 18T 충돌 grep (impl 착수 직전) — 2026-06-08

외부 AI가 18T 스냅샷/계약 충돌을 직접 grep. **새 P1 발견**: EN prompt:68-69 / KO prompt:67-68이 verdict로 hints를
강제 비움(`verdict≠xhr→candidate_endpoints=[]` 등) → v2.1 "verdict는 prior, hints 독립"과 충돌. LLM이 operator
오판 시 관찰 hint 폐기 = false-operator 그대로. **검증**: 프롬프트 실재 확인 → accept, v2.1 §G로 규칙 제거 + C3 반영.
직접 깨지는 테스트(test_acquire.py:141/204/222/332 + `_patch_analyze`)는 §4b 전환 목록으로 커밋 매핑. **교훈**:
설계 계약("hints 독립")이 *데이터가 아니라 프롬프트 규칙*에 박혀 있을 수 있다 — 코드뿐 아니라 prompt도 계약면. 이로써
18T 충돌 closed, 추가 리뷰 없이 C1 진입 승인.

### Agentic Acquisition Ladder design v1 라운드 1 — 2026-06-08

| # | 카테고리 | 판정 | 사유 |
|---|---|---|---|
| 1 | corner-case | accepted | axios regex(analyzer.py:125)가 `_ajax/...` 상대URL 미탐 + 외부번들 미fetch **2겹**. v1이 외부번들만 짚고 regex 버그를 놓침 — 리뷰어가 한 겹 더 팜. v2 §5 |
| 2 | corner-case | accepted | 키워드 scorer(probe.py:77)가 구조적 JSON roster 미탐(`company_name`→"company" 토큰 미스 확인). v2 §6 언어중립 structural validator |
| 3 | corner-case | accepted | winner 재fetch(probe.py:267)가 params/body/Referer 유실. v2 §7 |
| 4 | architecture | accepted(refine) | 결정성 주장 과대 → "저장 analysis/hints + HTTP fixture 내부 run만 결정적"으로 범위축소. v2 §10 |
| 5 | corner-case | accepted | raw_fetch.py:119 전체 버퍼링 → 사후 길이검사는 cap 아님. v2 §9 streaming byte cap + 횟수/시간 budget + 재귀금지 |
| 6 | architecture | accepted | 순서/early-exit 불명확 → v2 §4 per-verdict 순서표(고정 집합·budget) + §11 early-exit 계약 |
| 7 | corner-case | accepted | acquire.py:215 xhr가 content-type 무관 html_file 오저장 → v2 §8 content-type 인지 artifact |
| 8 | over-engineering | deferred | session_emul + in-rung LLM은 v1 over-harness(호출마다 새 client라 쿠키 미연속, HCR 불필요) → Deferred |

**메타**: 8 findings 전부 Factual 최상(file:line 인용, grep 전수 검증 일치). rejected 0. **이 라운드가 본 phase 최고 품질** — 코드 직독 기반 corner-case 6 + architecture 2. echo 신호 없음(round 1). **교훈**: 외부 리뷰어가 내 design note(v1)보다 한 겹 더 깊은 버그(#1 regex 자체) 발견 — design note도 "주장이 HEAD 코드와 일치하는가"를 내가 먼저 grep으로 검증했어야. **핵심 재조정**: "더 시도하는 에이전트 < 발견을 언어·형식 무관 정확 검증" — validation correctness가 acquisition ladder의 본질.
**Skeptic(7.5)**: round 1 + 사용자가 즉시 v2 지시 → 미실행.

### Y1A Benchmark Contract plan v1 라운드 1 — 2026-06-07

| # | 심각도 | Finding | 판정 |
|---|---|---|---|
| 1 | P1 | 대형 이벤트(300~550)에서 `max_companies:30`만 점수화하면 "전체 top-10"이 아님 | **accepted** — HEAD 대조 시 `max_chunks_per_event:12`(추출 cap)도 2겹임을 추가 발견. D2.1 benchmark universe(전수 override A / 사전고정 subset B + 예산) 신설 |
| 2 | P1 | "출력 전 라벨" vs "엔진 top-10만 라벨" 모순, `rank`을 gold에 넣음 | **accepted** — D3 blind labeling protocol(freeze→run→blind packet→label→reveal), rank을 run-result로 이동 |
| 3 | P1 | gold가 name-string only → JP 법인접두어·영문표기·공동부스 join 불안정 | **accepted** — D3.1 roster_id/canonical_name/aliases + 매칭 provenance 분리 |
| 4 | P1 | 완전라벨 이벤트에 class 부재 시 leakage/AUC 미정의 | **accepted** — D4.1 사전 class 확인 + N/A 기록 + 대체규칙, partner target=mode-relative 정의 |
| 5 | P2 | "spot-check" 임의성 → 0.85 재현 불가 | **accepted** — D5.1 고정 sampling(P6 전수/holdout seed≤30) + item verdict 4분류 + holdout≥1 측정 |
| 6 | P2 | run-summary 재현 정보 부족 | **accepted** — §2b에 commit SHA·cards/source/config SHA-256·model ID·ref-timestamp·caps·mode·cache 추가 |
| OH | — | contract-replay 8개 전부 불필요·holdout snapshot CI 금지 | **accepted** — §3 DEV static+xhr 1~2개로 한정, holdout 원본 CI fixture 금지 |

**메타**: 6 findings(4 P1+2 P2)+over-harness 전부 HEAD 대조 후 accept, rejected 0. 닫힌 이전 피드백 6건(메트릭 적격성·통과기준 사전고정·replay/실모델 분리·관측성 선행·job deferred·문서중복)은 v1 유지. 리뷰어가 fresh-vendor skeptic 생략+반영 후 Y1A.0 착수 권고 → plan v2 합성. **교훈**: #1 검증 중 리뷰어가 짚은 `max_companies`보다 상위에 `max_chunks_per_event` 추출 cap이 먼저 후보를 자름을 실코드(`defaults.yaml` L7/L13)로 확인 — 외부 리뷰의 방향이 맞아도 **근본 원인은 HEAD에서 한 겹 더 파야** 정확.

### Phase 18U (스코어링 변별력) 라운드 1~3 — 2026-06-05

| 라운드 | 핵심 | 판정 |
|---|---|---|
| R1 | penalty 튜닝을 입력/신호 오염 상태에서 하면 과적합 → 순서 재배치(입력 identity→신호→penalty), category_fit substring·캐시버전·news품질 범위 추가, 수치 acceptance 도입 | 대부분 accepted, plan v2 |
| R2 | "범용 엔진"으로 확장(target_mode/evidence_types/다중도메인 eval/CJK) 주장 | **범위 결정은 사용자 몫** — MVP 선택, 4항목 backlog #12. echo 아님(Novelty 높음)이나 over-scope |
| R3 | **stale 스냅샷**(S1 직후) — 5개 지적 중 4개(도메인추출·category substring·capability 혼입·일부 news)는 이미 S2(`7e92f3e`)·S3(`93d8965`)서 수정됨. "A=Vespa, 랭킹 신뢰 불가"는 수정 전 상태 | Novelty≈0.7. **실측이 반증**: 현재 Vespa B#19, 경쟁사 S/A=0, 타깃 median 5 vs 경쟁사 25 |

**메타**: R3는 같은 외부 AI가 옛 코드를 재지적한 stale/echo 케이스. **교훈**: 외부 리뷰는 어느 커밋 기준인지부터 확인 — 이미 고친 걸 다시 만지거나 deferred 결정을 뒤집지 말 것. 반복 등장한 두 축(경쟁사 결정성·news 품질)은 진짜 약점이었으나 전자는 penalty+신호정확성으로 해결(hard-cap 불필요 실증), 후자는 backlog. **stale 리뷰 판별 = "주장이 현재 코드/실측과 일치하는가"를 Factual 차원에서 먼저 검증**.

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

