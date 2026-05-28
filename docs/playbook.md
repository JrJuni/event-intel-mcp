# Playbook

The single source of truth for **patterns judged reusable** after solving a hard problem in this project (or imported from a sibling project that survives the same scrutiny here).

- **Relation to `lesson-learned.md`**: lessons are the "never make this mistake again" axis; playbook is the "this approach also works elsewhere" axis.
- **Lookup trigger**: when you hit an error or get stuck, **grep the keyword index here first**.
- **Inclusion bar**: (1) actually validated working in this project (or carried over with explicit re-validation), AND (2) reusable outside this project. One-off bug fixes don't qualify.
- **vs memory `feedback_*.md`**: playbook is for **project code/structure** patterns. Memory feedback is for **user collaboration style/preferences**. Keep the two stores distinct.

---

## Keyword Index

| Tags | Title | One-line summary |
|------|-------|------------------|
| `windows` `stdout` `cp949` `framework-help` | [1. Declarative CLI frameworks: stdio UTF-8 at module load](#1-declarative-cli-frameworks-stdio-utf-8-at-module-load) | Typer/Rich/FastMCP render help before command body → in-body reconfigure is too late |
| `monkeypatch` `testing` `module-access` `false-green` | [2. Orchestration layers import via module](#2-orchestration-layers-import-via-module-for-testability) | Use `from pkg import mod as _mod` + `_mod.Y` runtime attribute, not `from pkg.mod import Y` |
| `cold-start` `lazy-import` `mcp` `stdio` `ml-process` | [3. MCP server stays import-cold, ML lazy-loads on first call](#3-mcp-server-stays-import-cold-ml-lazy-loads-on-first-call) | Heavy ML deps (torch / sentence_transformers / chromadb) imported inside method bodies, not at module top. AST/regression test guards |
| `opt-in-flag` `envelope-additive` `backward-compat` `side-effect` | [4. Opt-in side-effect flag with envelope-additive contract](#4-opt-in-side-effect-flag-with-envelope-additive-contract) | New side effects gated behind `flag: bool = False`. Envelope additive (new keys, no key drops). Failures surface in envelope but never mask in-memory output |
| `provider-abstraction` `single-default` `swap-cost-zero` `lightweight` | [5. ABC + single default implementation = swap-ready without YAGNI](#5-abc--single-default-implementation--swap-ready-without-yagni) | When you might swap an integration later (LLM / embedding / vectorstore / search / fetch), ship an ABC with exactly one concrete impl. Zero feature bloat; trivial to swap |
| `error-envelope` `taxonomy` `error-code-enum` `hint-with-action` | [6. Stable error envelope with error_code enum + actionable hint](#6-stable-error-envelope-with-error_code-enum--actionable-hint) | Tool ok=false response = `{error_code, stage, message, hint, retryable}`. Snapshot-test the envelope shape so callers can branch on it |
| `slug-sanitize` `path-safety` `collection-name` `suggested-slug` | [7. Sanitize external slugs + return a suggested_slug on violation](#7-sanitize-external-slugs--return-a-suggested_slug-on-violation) | Any user-provided ID that becomes a path component or Chroma collection name must pass `^[a-zA-Z0-9_-]{1,64}$`. On violation, INVALID_INPUT envelope's `hint` includes a transliteration / hash-fallback suggested value |
| `capability-cards` `structured-context` `ssot-yaml` `llm-grounding` | [8. Capability cards YAML as structured product context SSOT](#8-capability-cards-yaml-as-structured-product-context-ssot) | For accuracy-critical retrieval/scoring, freeform brief drifts; structured YAML with `schema_version` + pydantic SSOT keeps scoring reproducible and supports `draft → human edit → ingest` lifecycle |
| `schema-drift` `pydantic-ssot` `snapshot-test` `schema-version-bump` | [9. JSON Schema snapshot drift test forces SCHEMA_VERSION discipline](#9-json-schema-snapshot-drift-test-forces-schema_version-discipline) | Lock `Model.model_json_schema()` against a committed snapshot file. Any drift fails CI with a one-liner refresh command; refresh requires bumping `SCHEMA_VERSION` in the same commit |
| `idempotent-upsert` `content-derived-ids` `vector-store` `re-ingest` | [10. Content-derived stable chunk IDs make re-ingest in-place](#10-content-derived-stable-chunk-ids-make-re-ingest-in-place) | When you upsert structured records into a vector store, derive each chunk's id from its content position (`cap:{i}:{name}`, `trigger:{i}`, ...) so re-ingest of the same source updates rows instead of appending duplicates |

When entries grow, re-sort by tag alphabetical order. Remove only when a pattern is invalidated (and record why).

---

## 1. Declarative CLI frameworks: stdio UTF-8 at module load

**Tags**: `windows` `stdout` `cp949` `framework-help`

**Problem**: Typer / Rich / FastMCP render help text and tool registration output during the framework's *own* startup — before the user's command body runs. Calling `sys.stdout.reconfigure(encoding="utf-8")` inside `main()` is too late: the framework has already streamed em-dashes / Korean to the cp949 console and crashed with `UnicodeEncodeError`.

**Solution**: Reconfigure stdio at the *very top* of the entry module, before any framework import:

```python
import sys
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, AttributeError):
            pass

# Now safe to import the framework
from mcp.server.fastmcp import FastMCP
```

**Why it works**: `sys.stdout.reconfigure` (Python 3.7+) flips the underlying text wrapper before the framework writes a single byte. The try/except handles edge cases (pytest capture, embedded interpreters) where streams don't support reconfigure.

**Reusable in**: Every Python CLI / TUI / MCP / framework-driven app on Windows. Module-load reconfigure is also harmless on Unix.

---

## 2. Orchestration layers import via module for testability

**Tags**: `monkeypatch` `testing` `module-access` `false-green`

**Problem**: Bind via `from event_intel.providers.llm import AnthropicProvider`, then have a test do `monkeypatch.setattr("event_intel.providers.llm.AnthropicProvider", FakeProvider)`. The consuming module already has the original class reference pinned at import time and keeps instantiating it. Tests "pass" but real Anthropic calls leak through (false green; network and cost leak).

**Solution**: Orchestration layers do **runtime attribute lookup** via module reference:

```python
# In events/extraction.py
from event_intel.providers import llm as _llm

def extract(raw, *, lang, ...):
    provider = _llm.AnthropicProvider()  # dict lookup on _llm at call time
    ...
```

Now `monkeypatch.setattr(_llm, "AnthropicProvider", FakeProvider)` propagates.

**Why it works**: In Python, `from X import Y` pins Y in the current module's namespace as a new binding. Changing `Y` on the original module does not propagate. Holding a module reference means each attribute access is a dict lookup — always fresh.

**Reusable in**: Every Python project that monkeypatches external calls / LLM / DB clients in tests. Apply by default to thin orchestration / pipeline / tool wrapper layers. Constants / types / exception class imports are exempt (they don't get monkeypatched).

---

## 3. MCP server stays import-cold, ML lazy-loads on first call

**Tags**: `cold-start` `lazy-import` `mcp` `stdio` `ml-process`

**Problem**: A stdio-based MCP server that imports `sentence_transformers` / `torch` / `chromadb` at module load takes 30+ seconds to start, which Claude Desktop perceives as a hang. The first tool call also gets billed for the cost. Worse, if the server hosts ML in the same process as stdio JSON-RPC, `model.encode()` can starve the asyncio event loop and make tool calls hang for minutes (bd-coldcall-agent Phase 14C lesson).

**Solution**: Two locks:

1. **Lazy import inside method bodies.** Every heavy dep is imported the first time it's used, not at module top:
   ```python
   class BgeM3Provider(EmbeddingProvider):
       def _get_model(self):
           if self._model is None:
               from sentence_transformers import SentenceTransformer  # ← here
               self._model = SentenceTransformer(...)
           return self._model
   ```
2. **Regression test** that snapshots `sys.modules` after `import event_intel.mcp_server` and asserts the forbidden set (`torch`, `transformers`, `sentence_transformers`, `chromadb`, `bitsandbytes`) is empty:
   ```python
   def test_import_mcp_server_does_not_load_heavy_ml(fresh_sys_modules):
       importlib.import_module("event_intel.mcp_server")
       leaked = [m for m in FORBIDDEN_HEAVY if m in sys.modules]
       assert not leaked
   ```

For a `check_runtime`-style preflight tool, expose a separate path that *does* trigger the load on demand, so users can opt in to cold-start cost.

**Why it works**: Python imports are cached per module. As long as the trigger point is inside a function that the import-time path doesn't call, deferring is free. The test catches any regression where someone adds an inadvertent module-top heavy import.

**Reusable in**: Any stdio / IPC-based server hosted in the same process as expensive ML. Also applicable to CLI tools where `--help` should be instant.

**Fixture pitfall — don't snapshot+restore sys.modules**: The naive cold-start fixture pattern is "snapshot `set(sys.modules)` at setup; on teardown pop everything not in the snapshot." It looks correct but breaks subtly: pydantic exposes `RootModel` via `__getattr__` lazy import AND caches the attribute on the parent `pydantic` package. Once cached, `from pydantic import RootModel` no longer re-triggers the lazy load — but the snapshot teardown has already popped `pydantic.root_model` from sys.modules. The next test's `mcp.types` re-import (which executes `class JSONRPCMessage(RootModel[...])` at module body) crashes with `KeyError: 'pydantic.root_model'` inside pydantic's `create_generic_submodel`. Same trap exists for any package that combines lazy `__getattr__` + parent-package attribute caching. Instead, **whitelist the prefixes you actually want to reset** (`event_intel.*` + the forbidden heavy modules) and leave infrastructure alone:

```python
@pytest.fixture
def fresh_sys_modules():
    yield
    _purge("event_intel")
    for heavy in FORBIDDEN_HEAVY:
        _purge(heavy)
```

See `tests/test_mcp_cold_start.py` and `docs/lesson-learned.md` (2026-05-28 entry).

**Linked lesson**: (bd-coldcall-agent's Phase 14C-B documented the failure mode of the alternative architecture — process separation. We adopted lazy-load + cold-start guard as the simpler v0 solution.)

---

## 4. Opt-in side-effect flag with envelope-additive contract

**Tags**: `opt-in-flag` `envelope-additive` `backward-compat` `side-effect`

**Problem**: A previously read-only tool (e.g. `validate_capability_cards`) grows a side effect (writes to disk, mirrors to a vector store). Naively flipping behavior breaks every existing caller. A versioned `v2` tool doubles the API surface for years.

**Solution**: Gate new side effects behind a `flag: bool = False` argument. Default stays False so legacy callers see byte-identical envelope. Opt-in adds NEW keys to the envelope (`*_id`, `*_status`, `*_error`) without dropping pre-existing keys. Side-effect failures surface in the envelope (`status="failed"` + `error`) but never mask the in-memory output the tool was already producing.

```python
@app.tool()
def ingest_product_context(
    workspace_id: str = "default",
    cards_path: str = "",
    extra_source_paths: list[str] | None = None,
    persist: bool = True,           # default behavior unchanged
    refresh: bool = False,           # new opt-in
) -> dict:
    result = {"ok": True, "chunks_indexed": ...}
    if refresh:
        try:
            cleared = _reset_collection(workspace_id)
            result["refresh_status"] = "success"
            result["chunks_cleared"] = cleared
        except Exception as e:
            result["refresh_status"] = "failed"
            result["refresh_error"] = str(e)
            # in-memory result still returned
    return result
```

**Why it works**: Additive contracts are forward-compatible. Old clients ignore new keys; new clients opt in by setting the flag. Failures are diagnosable without changing `ok` semantics for the primary purpose of the tool.

**Reusable in**: Every long-lived tool / API surface. Especially MCP tools where `@app.tool()` signatures are part of the user-visible contract.

---

## 5. ABC + single default implementation = swap-ready without YAGNI

**Tags**: `provider-abstraction` `single-default` `swap-cost-zero` `lightweight`

**Problem**: For integrations you *might* swap later (LLM vendor, embedding model, vector DB, search provider, HTTP fetcher), the choice is: (a) hardcode the chosen impl and pay the refactor cost when you swap, (b) build a full plugin system with config-driven loaders. Both feel wrong — (a) creates lock-in, (b) is YAGNI for v0.

**Solution**: Ship an ABC with one concrete impl. No registry, no config-driven loader. Zero feature bloat. When you need to swap, you add a second class — the consumers already depend on the ABC.

```python
# providers/llm.py
class LLMProvider(ABC):
    @abstractmethod
    def chat_cached(self, *, system, cached_context, ...) -> LLMResponse: ...

class AnthropicProvider(LLMProvider):
    ...  # the only impl in v0
```

Consumers type-annotate with `LLMProvider`. Tests inject fakes by subclassing the ABC. When OpenAI support lands, it's a sibling subclass — no churn elsewhere.

**Why it works**: The ABC is the only API consumers see; concrete impls are an implementation detail. Adding a second impl is purely additive. The cost of defining the ABC upfront is one class with `@abstractmethod` decorators — trivial. The cost of skipping it and refactoring later is every consumer touching the concrete class name.

**Reusable in**: Any integration boundary with a non-trivial chance of vendor swap. Don't apply to stable boundaries (stdlib, Python itself) or to integrations you're certain will never swap.

---

## 6. Stable error envelope with error_code enum + actionable hint

**Tags**: `error-envelope` `taxonomy` `error-code-enum` `hint-with-action`

**Problem**: A tool returns `{"ok": false, "error": "something went wrong"}`. Callers can't distinguish "you forgot to ingest product context" from "the embedding model isn't downloaded" from "Brave is rate-limiting us". Users open the same support ticket for all three.

**Solution**: Define an `error_code` enum + `stage` enum + machine-readable `hint`. Every `ok=false` returns the same shape. Snapshot-test the envelope so a code change can't silently break callers.

```python
class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    MODEL_NOT_READY = "MODEL_NOT_READY"
    PRODUCT_CONTEXT_MISSING = "PRODUCT_CONTEXT_MISSING"
    ...  # ~10 codes

class MCPError(Exception):
    def to_envelope(self) -> dict:
        return {
            "ok": False,
            "error_code": str(self.error_code),
            "stage": str(self.stage),
            "message": self.message,
            "hint": self.hint,          # str or {action: ..., suggested_slug: ...}
            "retryable": self.retryable,
        }
```

`hint` can be a string for simple cases or a dict for complex cases (e.g. `INVALID_INPUT` on a Korean slug returns `{"suggested_slug": "seoul-its-2026", "rule": "^[a-zA-Z0-9_-]{1,64}$"}`).

**Why it works**: Callers branch on `error_code` (stable enum), users read `message` (human), retry logic reads `retryable` (bool), recovery steps read `hint` (machine or human). The snapshot test guards the envelope shape across releases.

**Reusable in**: Every tool API surface with non-trivial failure modes. MCP tools especially, because Claude itself is the caller and benefits from machine-readable errors.

---

## 7. Sanitize external slugs + return a suggested_slug on violation

**Tags**: `slug-sanitize` `path-safety` `collection-name` `suggested-slug`

**Problem**: User-provided IDs (`workspace_id`, `event_slug`) end up as filesystem path components AND Chroma collection names. `../../etc` is the obvious risk; `"서울 ITS 2026"` is the realistic one (Korean events are common, contain spaces and non-ASCII, and break most downstream tooling).

**Solution**: Two helpers in `storage/identifiers.py`:

```python
SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

def sanitize_slug(s: str) -> str:
    """Raise INVALID_INPUT if s doesn't match SLUG_RE. Return s unchanged."""
    if not SLUG_RE.match(s):
        raise MCPError(
            error_code=ErrorCode.INVALID_INPUT,
            stage=Stage.PREFLIGHT,
            message=f"slug '{s}' violates {SLUG_RE.pattern}",
            hint={"suggested_slug": suggest_slug(s), "rule": SLUG_RE.pattern},
            retryable=False,
        )
    return s

def suggest_slug(s: str) -> str:
    """Best-effort ASCII slug. Korean → romanization → lowercase + hyphens.
    Falls back to event-{sha1[:8]} on transliteration failure."""
    ...
```

Every MCP tool that accepts a slug runs `sanitize_slug` at entry. Failures return a friendly envelope; the user copies `hint.suggested_slug` and retries.

**Why it works**: Hard regex prevents the obvious attack and the realistic typo. `suggested_slug` turns a hostile error into a guided correction — the user doesn't have to learn the regex. The two functions stay testable in isolation.

**Reusable in**: Any tool that accepts free-text IDs and stores them as path / collection / table names.

---

## 8. Capability cards YAML as structured product context SSOT

**Tags**: `capability-cards` `structured-context` `ssot-yaml` `llm-grounding`

**Problem**: For an LLM-driven scoring / retrieval system, the input "what is our product" matters as much as the model. A freeform `product_brief.md` lets the LLM grep loosely — but scoring becomes irreproducible, RAG quality drifts, and "why was this candidate scored A?" loses an answer.

**Solution**: Define product context as a structured pydantic schema (`schema_version: 1`):

```yaml
schema_version: 1
product_name: "Acme Inference"
one_liner: "Drop-in inference accelerator for edge AI workloads."
capabilities:
  - name: "low-power inference"
    keywords: [edge, batteryDevice, npu]
    buyer_pains: ["device thermals", "battery life"]
    evidence_queries: ["embedded NPU evaluation", ...]
ideal_customer:
  industries: [semiconductor, automotive, IoT]
  company_signals: ["hiring embedded engineers", "uses TensorRT"]
buying_triggers:
  - signal: "raised series B"
    weight: 0.7
bad_fit:
  - reason: "cloud-only workload"
    keywords: [datacenter, cloud-native]
competitors:
  - name: "QuantizeCo"
    keywords: [quantizeco, quantize.co]
```

Lifecycle: `draft_capability_cards` (LLM drafts from source docs) → human edits → `validate_capability_cards` (pydantic) → `ingest_product_context` (chunker → embeddings → vectorstore). Authoring stays cheap (LLM does first draft) but the SSOT stays structured (human reviews before ingest).

The optional `product_brief.md` is a *generated view* for human reading — never the scoring source.

**Why it works**: Each field is a deterministic input to scoring (capability keywords boost retrieval; bad_fit drives penalty; competitors trigger exclusion). Reproducibility comes from `schema_version` — changing the schema requires a bump and a migration plan. Authoring friction stays low because the LLM drafts most of it.

**Reusable in**: Any LLM application where retrieval / scoring / matching depends on a structured "what we offer" frame. ABM tools, sales intelligence, BD discovery, recommender systems with editorial input.

---

## 9. JSON Schema snapshot drift test forces SCHEMA_VERSION discipline

**Tags**: `schema-drift` `pydantic-ssot` `snapshot-test` `schema-version-bump`

**Problem**: A pydantic model becomes the SSOT for some structured user content (yaml / json config / API payload). Over months, contributors add / rename / re-type fields without bumping `SCHEMA_VERSION`. Old user content silently starts failing in obscure ways. Downstream code (`if cards.schema_version == 1: ...`) keeps running against drifted shapes.

**Solution**: Export the model's JSON Schema once, commit it as `schema_snapshot.json`, and write a test that re-exports + compares:

```python
# In cards/schema.py
SCHEMA_VERSION = 1

class CapabilityCards(BaseModel):
    schema_version: Literal[1] = 1
    ...

# Snapshot generation (one-time + on each intentional bump)
# event-intel export-schema --out src/event_intel/cards/schema_snapshot.json

# In tests/test_cards_schema_drift.py
def test_schema_snapshot_matches_current_model():
    snapshot = _SNAPSHOT_PATH.read_text(encoding="utf-8").strip()
    current = json.dumps(
        CapabilityCards.model_json_schema(),
        indent=2, sort_keys=True, ensure_ascii=False,
    ).strip()
    assert snapshot == current, (
        "Schema drifted. If intentional:\n"
        "  1. Bump SCHEMA_VERSION in cards/schema.py\n"
        "  2. event-intel export-schema --out src/event_intel/cards/schema_snapshot.json\n"
        "  3. Re-run pytest."
    )

def test_schema_version_is_one():
    schema = CapabilityCards.model_json_schema()
    assert schema["properties"]["schema_version"].get("const") == 1
```

The drift test failure message itself is the migration instruction. Contributors can't sneak in a field rename — the snapshot diff is loud and the refresh path is documented.

**Why it works**: `model_json_schema()` is deterministic per pydantic version + model definition. `sort_keys=True` removes dict-ordering noise. The literal `schema_version: Literal[1]` makes pydantic emit `const: 1` in the schema, so accidental version mismatch surfaces in the drift test too (`test_schema_version_is_one`). Refresh is one CLI call (`event-intel export-schema --out ...`), so the friction is low enough that nobody is tempted to delete the test.

**Caveats**: Pydantic version upgrades can change schema emission (added optional keys, defaults, etc.) and force a refresh that isn't a real schema change. Document this in the test's failure message so the next person knows whether to bump `SCHEMA_VERSION` or just refresh. The snapshot file is small (~5 KB) and lives next to the model — no separate fixtures dir.

**Reusable in**: Any pydantic-as-SSOT scenario where humans hand-write the source content (yaml configs, capability cards, API request payloads, plugin manifests). Especially valuable when the schema is consumed across processes or projects.

---

## 10. Content-derived stable chunk IDs make re-ingest in-place

**Tags**: `idempotent-upsert` `content-derived-ids` `vector-store` `re-ingest`

**Problem**: A user edits `capability_cards.yaml`, re-runs `event-intel ingest --cards ...`. Naive ingest generates fresh `uuid4()` ids for each chunk → Chroma `upsert` treats them as new rows → collection grows linearly with every re-ingest → retrieval precision collapses (same capability returned 4x with slightly different vectors after 4 ingests).

**Solution**: Derive each chunk's id from its position in the structured input, NOT from random uuid:

```python
# cards/ingest.py — flatten yields stable ids per logical role + index
chunks.append(_Chunk(id="product:summary", text=..., ...))

for i, cap in enumerate(cards.capabilities):
    chunks.append(_Chunk(id=f"cap:{i}:{cap.name}", text=..., ...))

chunks.append(_Chunk(id="ideal_customer:industries", text=..., ...))
chunks.append(_Chunk(id="ideal_customer:signals",    text=..., ...))
if ic.geo:
    chunks.append(_Chunk(id="ideal_customer:geo",    text=..., ...))

for i, trig in enumerate(cards.buying_triggers):
    chunks.append(_Chunk(id=f"trigger:{i}", text=..., ...))
```

Re-ingest of the same yaml → identical id list → Chroma's `upsert` updates the existing rows in place. Re-ingest after a real edit (`cap[2].keywords` extended) → same id (`cap:2:{name}`) → updated text + new embedding overwrite the old row. Collection size matches the logical card count regardless of how many times you re-ingest.

**Why it works**: Vector stores like Chroma treat `upsert` as "replace if id exists, else insert." When ids are content-deterministic, the operation is naturally idempotent. The id scheme also makes ad-hoc debugging tractable (`col.get(ids=["cap:0:Quantization"])` instead of grepping uuids).

**Test pattern** (cheap to add): re-ingest twice with the same input, assert chunk count is identical:

```python
def test_reingest_is_idempotent_no_duplicates(repo_root):
    cards = _load_fixture_cards(repo_root)
    emb, vs = FakeEmbedding(), FakeVectorStore()
    r1 = ingest_cards(cards=cards, workspace_id="default",
                      embedding_provider=emb, vectorstore_provider=vs)
    r2 = ingest_cards(cards=cards, workspace_id="default",
                      embedding_provider=emb, vectorstore_provider=vs)
    assert r1["chunks"] == r2["chunks"]
    assert vs.collection_info("product_default")["count"] == r1["chunks"]
```

**Caveats**: If the id encodes mutable user-visible fields (`cap:{i}:{name}`), renaming `name` changes the id and creates an orphan row. Two ways to handle: (a) accept the cost and `delete + upsert` on each ingest (simple, fine for small collections), or (b) drop the human-readable suffix from the id (`cap:{i}` only) and store the name in metadata (more robust to renames, but breaks ad-hoc grep). v0 picks (a) — collections are small (< 100 chunks).

**Reusable in**: Any pipeline that periodically rebuilds a vector store from a structured source (capability cards, knowledge base articles, product catalog rows). Avoid for pipelines where the source has no stable position (e.g. de-duplicated web crawl) — those need content-hash ids instead.

---

When entries grow, re-sort by tag alphabetical order. Remove only when a pattern is invalidated (and record why).
