# Architecture

## Big picture

```
   ┌──────────────────────────────────────────────────────────┐
   │  Claude Desktop  (primary surface)                       │
   │     └─ 5 MCP tools:                                      │
   │         check_runtime / draft_capability_cards /         │
   │         validate_capability_cards /                      │
   │         ingest_product_context / build_event_tier_list   │
   └──────────────────────────┬───────────────────────────────┘
                              │  stdio JSON-RPC
                              ▼
   ┌──────────────────────────────────────────────────────────┐
   │  event_intel.mcp_server (FastMCP)                        │
   │     └─ 5 tool handlers ───→ envelope-wrapped responses   │
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
        └─────────────────────┼──────────────────────────┘
                              ▼
                     providers/ (ABC + default impl)
                       ├ llm.py        — Anthropic Sonnet 4.6
                       ├ embedding.py  — bge-m3 (lazy)
                       ├ vectorstore.py— Chroma persistent (lazy)
                       ├ search.py     — Brave Web/News
                       └ fetch.py      — httpx + trafilatura
```

The MCP surface (Claude Desktop stdio) is the day-to-day driver. A thin CLI (`event-intel`) re-uses the same tool handlers for smoke / debug. No FastAPI, no Web UI, no DB — artifacts under `outputs/{workspace_id}/{event_slug}/` are the durable record.

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
   │ enrichment (Brave web+news per exhibitor, cached, max 30 companies)
   │   ├ deterministic official URL rule (Levenshtein + keyword overlap)
   │   ├ news_snippets (180-day window)
   │   └ fetch body (top 3, 2000 chars cap)
   ▼
enriched_exhibitors.yaml  (with verification_status, evidence packets)
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
    brave/{sha256(query+kind)}.json  # per-call Brave response cache

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

## Error model

10 stable error_codes × 6 stages, defined in `event_intel/errors.py`. Every tool returns either `{"ok": True, ...}` or the `MCPError` envelope:

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

3-tier:
- **`.env`** — secrets only. `ANTHROPIC_API_KEY` + `BRAVE_API_KEY`. Gitignored.
- **`config/defaults.yaml`** — shipped defaults (extraction caps, scoring weights, tier rules, model names).
- **`~/.event-intel/config.yaml`** (optional) — user overrides per workspace. (Not in v0; loader added in S2/S4.)

## What's deliberately not here

- No FastAPI / no Web UI — artifacts under `outputs/` are the durable record.
- No SQLite / Postgres / Alembic — Chroma is the only persistent store.
- No Notion sync — bd-coldcall-agent's territory.
- No background worker process — single-process MCP server with lazy ML load.
- No bd-coldcall-agent imports — completely standalone repo.

See `~/.claude/plans/tender-mixing-badger.md` (Plan v0.5 final) for the full Contract list and rationale for each "not here" decision.
