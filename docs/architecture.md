# Architecture

## Big picture

```
   ┌──────────────────────────────────────────────────────────┐
   │  Claude Desktop  (primary surface)                       │
   │     └─ 13 MCP tools:                                     │
   │         check_runtime / draft_capability_cards /         │
   │         validate_capability_cards /                      │
   │         ingest_product_context / build_event_tier_list / │
   │         analyze_event_page / probe_exhibitor_endpoint /  │
   │         acquire_exhibitor_source / draft_labels /        │
   │         sync_product_sources /                           │
   │         prepare_models / login_chatgpt / get_job         │
   └──────────────────────────┬───────────────────────────────┘
                              │  stdio JSON-RPC
                              ▼
   ┌──────────────────────────────────────────────────────────┐
   │  event_intel.mcp_server (FastMCP)                        │
   │     └─ 13 tool handlers ──→ envelope-wrapped responses   │
   └──────────────────────────┬───────────────────────────────┘
                              │
        ┌─────────────────────┼──────────────────────────┐
        ▼                     ▼                          ▼
   tools/check_runtime    cards/{drafter,validator,    events/{source_capture,
                          ingest}                       extraction,enrichment}
        │                     │                          │
        ▼                     ▼                          ▼
   runtime/preflight     rag/{chunker,store,           scoring/{dimensions,
        │                retriever}                     rules,compute}
        │                     │                          │
        └────┬────────────────┼──────────────────────────┘
             │                ▼
             │       acquisition/ (Phase 18T + ladder)
             │         ├ analyzer.py         — analyze_response (verdict + hints,
             │         │                        1 Sonnet call) + <base>/bundle discovery
             │         ├ probe.py            — XHR / embedded_json probe + roster validator
             │         ├ acquire.py          — orchestrator (strategy ladder + budget)
             │         ├ url_safety.py       — validate_url + host_relation
             │         ├ robots.py           — httpx-direct + status→policy
             │         ├ raw_fetch.py        — shared UA, streaming byte cap, status=0
             │         └ http_status_map.py  — HTTP → MCPError envelope
             ▼
                     providers/ (ABC + default impl)
                       ├ llm.py        — AnthropicProvider | ChatGPTOAuthProvider
                       │                  (factory: make_llm_provider(config))
                       ├ embedding.py  — bge-m3 (lazy)
                       ├ vectorstore.py— Chroma persistent (lazy)
                       ├ search.py     — Ddgs(default) | Searxng | Brave Web/News
                       │                  (factory: make_search_provider(config))
                       └ fetch.py      — httpx + trafilatura
```

The MCP surface (Claude Desktop stdio) is the day-to-day driver. A thin CLI (`event-intel`) re-uses the same tool handlers for smoke / debug. No FastAPI, no Web UI, no DB — artifacts under `outputs/{workspace_id}/{event_slug}/` are the durable record.

## Source acquisition layer (Phase 18T + agentic ladder)

`acquire_exhibitor_source` no longer branches one-to-one on the analyzer verdict.
The verdict is a **prior** that orders a fixed set of strategy rungs; every rung
is reachable, evidence-gated, and budget-bounded, so a wrong prior still recovers.

```
[URL]
  │ url_safety + robots (landing gates — fatal, never bypassed)
  │ fetch landing ONCE (streaming byte cap) ── shared across all rungs
  │ analyze_response(resp)  (1 Sonnet call; HTTP status mapped first →
  │                          401/403/404/5xx/transport raises BEFORE the LLM)
  ▼
verdict (prior) orders the rungs ─ same fixed set, never removed:
  ├ static    — the already-fetched landing body, IF it scores as a roster
  │             (an SPA shell scores below the floor → never a static success)
  ├ embedded  — embedded-JSON selectors against the shared landing body
  ├ xhr       — candidate endpoints from hints → probe_exhibitor_endpoint
  ├ bundle    — endpoints discovered inside same-origin external <script src>
  │             bundles, resolved against <base href> (the H.C.R. Vue-SPA case)
  └ operator  — terminal: OPERATOR_CAPTURE_REQUIRED (or LOGIN_REQUIRED when the
                prior was login_required)
  ▼
roster body → content-type-aware artifact:
  JSON-shaped → source.json (text_file) ·  else → source.html (html_file)
  ▼
~/.event-intel/artifacts/{workspace_id}/{event_slug}/source.{html,json}
        + manifest.json (sha256 + verdict + selected_rung + winning_request
                         (redacted) + analysis_fp + config_fp + meta)
  │
  ▼
(source_kind, source_ref) → build_event_tier_list
```

**Budget (`AcquireBudget`).** One run enforces a per-response byte cap
(`max_bytes_per_fetch`), a cumulative byte cap, a total HTTP-call cap, and a
wall-clock deadline, reserving calls for the bundle rung (`reserved_calls_bundle_rung`)
so a wrong prior can't starve it. Acquire-direct fetches (landing / bundle /
pagination) are charged per-byte; probe rungs are gated by a pre-check and
accounted in bulk (they fetch endpoints, not the landing, and the deadline still
bounds them). All values live in the `acquisition:` block of `config/defaults.yaml`.

**Determinism scope.** For a fixed stored analysis/hints + fixed HTTP responses,
the internal ladder run is deterministic (the analyzer LLM and the host retry
loop are not). The manifest records the inputs (`analysis_fp`, `config_fp`) and
the winning request so a run is reproducible.

**Roster validation** is language-neutral and structural for JSON (`probe.py`:
bounded nested search for the largest list-of-dicts + a company-specific signal +
a minimum unique-name count), keyword-based for HTML — so a JP/KO/EN roster is
accepted while a product/staff/menu/config JSON is rejected.

Cache hit re-verifies sha256 from manifest (0 fetch / 0 LLM). Corrupt artifact
triggers refetch + warning. Pre-ladder manifests (no provenance fields) still load
via `.get()` defaults. `EVENT_INTEL_ARTIFACTS_DIR` env override supported.

## Two-flow model

**Flow A — Product Context lifecycle** (one-time per product, occasional re-ingest)

```
[source docs / text]
   │ draft_capability_cards (Sonnet, chunked)
   ▼
capability_cards.draft.yaml
   │ human edits → capability_cards.yaml
   ▼
[validate_capability_cards]  pydantic schema v1, path-localized errors
   │
   ▼
[ingest_product_context]
   │ chunker → bge-m3 → Chroma upsert
   ▼
Product Context collection (`product_{workspace_id}`)
   ├ per-capability chunks
   ├ ideal_customer chunks
   ├ buying_triggers chunks
   └ bad_fit / competitor chunks
```

**Flow B — Event Tier List pipeline** (per exhibitor list)

```
[event source: url / html_file / csv_file / text]
   │ source_capture → raw_source artifact
   ▼
extraction (Sonnet, chunked, snippet-anchored, max 12 chunks/event)
   │ snippet ≥ 20 chars enforced. lang-specific name normalization (en / ko).
   ▼
exhibitor_candidates.yaml  (raw)
   │ enrichment (web+news search per exhibitor via search.provider, cached, max 30 companies)
   │   ├ deterministic official URL rule (Levenshtein + keyword overlap)
   │   ├ news collection ladder (zero-config news plan, ZNC):
   │   │    ddgs backend=auto → classified retries (genuine-empty ≠ failure)
   │   │    → Google News RSS fallback (news-only, fires on degraded queries)
   │   │    → article BODY fetch (robots-gated, byte-capped, trafilatura)
   │   │    → entity-relevance gate (homonym names need ctx co-occurrence)
   │   │    → near-dup removal → LLM query-rescue (blocked-and-empty rows,
   │   │      queries only — fetch stays deterministic)
   │   ├ degraded results never stick: not cached, resume rows flagged +
   │   │    re-enriched next run; failures degrade per-query, never abort the
   │   │    stage (MCPError/config errors stay fatal)
   │   └ failure-pattern diagnostics → ~/.event-intel/diagnostics/ (R1;
   │        `benchmark retry-stats` aggregates; R3 codifies the retry policy)
   ▼
enriched_exhibitors.yaml  (with verification_status, evidence packets,
                           per-news body_sha/body_chars)
   │ Event Evidence ingest → Chroma "event_{ws}_{slug}"
   │ Fit Retrieval (event → product, single direction)
   ▼
scoring
   ├ 7 dimensions (capability_fit, source_confidence, buying_signal,
   │   website_verification, category_fit, competitor_penalty, bad_fit_penalty)
   ├ weighted sum → final_score
   ├ tier rules (yaml-driven, S/A/B/C)
   └ Evidence floor (3-state lifecycle, see below)
   │
   │ Sonnet rationale (1-sentence, post-tier-decision)
   ▼
tier_list.md  +  tier_list.yaml  +  run_summary.json
```

## Evidence floor lifecycle (Contract #9)

| State | Constraint | Drop / Flag rule |
|---|---|---|
| **raw_extraction** | `source_snippet < 20 chars` → automatic drop (no `needs_review` label) | Hard filter in `events/extraction.py` |
| **enriched** | All survivors carry `source_snippet`. `official_url` + `news_snippets` may or may not be present | `verification_status` = verified / weak / unknown |
| **scoring** | `has_url + has_news` ∈ {0, 1, 2}. Floor rule: 2 → S/A allowed, 1 → A max, 0 → B max | `needs_review` separate state: extraction_confidence below threshold OR enrichment hard failure |

`needs_review` is **not** part of the floor — it's an orthogonal lifecycle for low-confidence candidates that bypass scoring entirely. Reported in a dedicated section of `tier_list.md`.

**Entity-relevance gate (ZNC C2)**: news only counts toward the floor (and buying_signal) if `is_relevant_news` passes — official-domain same-site bypass → whole-token name mention → for AMBIGUOUS names (single common-English-word distinctive token, e.g. Dust/Ramp/Chroma) an additional ≥1 context-term co-occurrence from the company's own snippet/description. Fail-open without context. Fetched article bodies are gated by the same predicate (B2), and near-duplicate bodies (wire syndication) are dropped. Per-article body↔product RAG relatedness is computed post-scoring and recorded in the report only (`news_relatedness`, REPORT_SCHEMA_VERSION 5) — no path to scores/tiers.

## Process model

Single-process FastMCP server. Heavy ML (bge-m3 inference, Chroma queries) runs in the same process as the MCP server but is **lazy-loaded** — `tests/test_mcp_cold_start.py` regression-guards that `torch / transformers / sentence_transformers / chromadb / bitsandbytes` do NOT enter `sys.modules` on import.

No separate ML worker (unlike bd-coldcall-agent). The complexity wasn't justified for a single-product surface, and lazy import + `check_runtime` preflight covers the cold-start UX. If long-term latency demands it, a worker split can be added later under the same provider ABC.

## Storage layout

```
~/.event-intel/
  chroma/
    {workspace_id}/
      product_{workspace_id}/        # capability cards chunks
      event_{workspace_id}_{slug}/   # per-event evidence chunks
  cache/
    search/{ws}/{sha1(provider+query+kind+lang+count+days)}.json  # per-call search response cache (provider-keyed)
  artifacts/
    {workspace_id}/{event_slug}/     # acquisition ladder output
      source.html | source.json      # captured page or extracted/probed JSON roster
      manifest.json                  # sha256 + verdict + selected_rung +
                                     # winning_request (redacted) + analysis_fp + config_fp
  resume/
    {workspace_id}.jsonl             # per-row enrichment resume artifact
  chatgpt_auth.json                  # ChatGPT OAuth tokens (refresh-rotated)
  config.yaml                        # optional user overrides

<repo>/outputs/
  {workspace_id}/
    capability_cards.draft.yaml      # drafter output
    capability_cards.yaml            # human-edited SSOT
    product_brief.md                 # optional generated view
    {event_slug}/
      raw_source.html                # captured source
      exhibitor_candidates.yaml      # extraction output
      enriched_exhibitors.yaml       # enrichment output
      enriched.partial.yaml          # resume artifact (per-row)
      tier_list.md                   # human-readable report
      tier_list.yaml                 # machine-readable
      run_summary.json               # counts, timings, warnings, errors
```

All Chroma collection names and filesystem paths pass through `storage/identifiers.py::sanitize_slug` (`^[a-zA-Z0-9_-]{1,64}$`). Invalid slugs return `INVALID_INPUT` envelope with `hint.suggested_slug`.

**Path resolution is centralized in `runtime/paths.py::ResolvedPaths`** (W0). Two roots: the **workspace root** (user-facing cards / sources / event reports) and the **data root** (`~/.event-intel`: chroma / artifacts / cache / resume / source-index). Per-leaf precedence: fine-grained env (`EVENT_INTEL_OUTPUT_DIR`/`_ARTIFACTS_DIR`/`_CHROMA_DIR`) → coarse env (`EVENT_INTEL_WORKSPACE_DIR`/`_DATA_DIR`) → `config.paths.*` → built-in default. The workspace-root default is `~/EventIntel`, but an existing checkout (where `<repo>/outputs/` exists) transparently keeps using `<repo>/outputs` (back-compat fallback) until the W5 `storage migrate` step. The resolver is stdlib-only (cold-import safe) and side-effect-free. This closed two bugs: `draft_capability_cards` wrote to a cwd-relative `outputs/` (unwritable under the server's foreign cwd), and `ChromaProvider` ignored `config.paths.chroma_dir` despite preflight requiring it.

## Error model

14 stable error_codes × 7 stages, defined in `event_intel/errors.py` (Phase 18T added 4 codes — `ACQUISITION_AMBIGUOUS`, `LOGIN_REQUIRED`, `OPERATOR_CAPTURE_REQUIRED`, `ROBOTS_DISALLOWED` — and 1 stage — `acquisition`). Every tool returns either `{"ok": True, ...}` or the `MCPError` envelope:

```json
{
  "ok": false,
  "error_code": "PRODUCT_CONTEXT_MISSING",
  "stage": "preflight",
  "message": "Workspace 'default' has no ingested product context yet",
  "hint": "Run `event-intel ingest --cards <path> --workspace default` first",
  "retryable": false
}
```

The envelope is snapshot-tested (`tests/test_mcp_error_taxonomy.py`) so callers can rely on its shape across versions.

## Configuration

3-tier (deep-merged: user overrides defaults, both validated against required-key list):
- **`.env`** — secrets only. `ANTHROPIC_API_KEY` (Anthropic path) or none (ChatGPT OAuth path); `BRAVE_API_KEY` only when `search.provider: brave` (default `ddgs` is keyless). Auto-loaded via `python-dotenv` at `cli.py` + `mcp_server.py` module top. Gitignored.
- **`config/defaults.yaml`** — shipped defaults (extraction caps, scoring weights, tier rules, model names, `llm.provider: anthropic`).
- **`~/.event-intel/config.yaml`** (optional, active) — user overrides per workspace. Deep-merged over defaults. Most common use: `llm.provider: chatgpt_oauth` to swap to subscription-based OAuth path for zero-cost experimentation. See playbook #14.

LLM provider is selected via `make_llm_provider(config)` factory — caller passes the deep-merged config, factory branches on `llm.provider` (`anthropic | chatgpt_oauth`). Anthropic path is byte-for-byte unchanged from v0.1; ChatGPT OAuth path uses Codex backend protocol (playbook #11).

## What's deliberately not here

- No FastAPI / no Web UI — artifacts under `outputs/` are the durable record.
- No SQLite / Postgres / Alembic — Chroma is the only persistent store.
- No Notion sync — bd-coldcall-agent's territory.
- No background worker process — single-process MCP server with lazy ML load.
- No bd-coldcall-agent imports — completely standalone repo.

See `~/.claude/plans/tender-mixing-badger.md` (Plan v0.5 final) for the full Contract list and rationale for each "not here" decision.
