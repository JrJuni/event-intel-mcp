# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project north star

**event-intel-mcp** turns exhibitor lists (URL / HTML / CSV / pasted text) into evidence-backed BD target tier lists via a standalone MCP server. Single product surface (Claude Desktop), 10 MCP tools (5 core + 3 acquisition layer + 1 labeling + 1 source library), local mini-RAG (bge-m3 + Chroma), zero dependency on the sibling bd-coldcall-agent repo.

## Dev environment

Conda-based on Windows. The env is named `event-intel` and lives at `~/miniconda3/envs/event-intel/`. Use its Python directly — never `python` / `py`, which hit the Microsoft Store stub on a fresh Windows box.

```bash
# One-time setup
~/miniconda3/Scripts/conda.exe create -n event-intel python=3.11 -y
cd /c/Users/JuniBecky/Downloads/event-intel-mcp
~/miniconda3/envs/event-intel/python.exe -m pip install -e ".[dev]"

# Always run via the env's Python
~/miniconda3/envs/event-intel/python.exe -m <module> [args]
```

## Common commands

```bash
# Tests
~/miniconda3/envs/event-intel/python.exe -m pytest

# Cold-start regression only (fast)
~/miniconda3/envs/event-intel/python.exe -m pytest tests/test_mcp_cold_start.py -v

# Runtime preflight + bge-m3 download
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli models prepare
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli check-runtime --workspace default

# Product context lifecycle
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli draft-cards --source docs/brief.md --workspace default
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli validate --cards outputs/default/capability_cards.yaml
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli ingest --cards outputs/default/capability_cards.yaml --workspace default

# Event tier list build
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli build-event \
  --workspace default --event-name "Sample Expo" --event-slug sample_expo \
  --html-file path/to/exhibitors.html

# MCP server (stdio, normally spawned by Claude Desktop)
~/miniconda3/envs/event-intel/python.exe -m event_intel.mcp_server
```

The full catalog (debugging snippets, all flags, MCP config) is in `docs/commands.md`.

## Architecture — the big picture

10 MCP tools, single FastMCP process, local mini-RAG:

```
Claude Desktop (stdio JSON-RPC)
   │
   ▼
event_intel.mcp_server (FastMCP) — 10 tools
   │
   ├─ check_runtime              (runtime/preflight.py — bge-m3 cache / Chroma / API keys / product context)
   ├─ draft_capability_cards     (cards/drafter.py — Sonnet chunked draft from source docs)
   ├─ validate_capability_cards  (cards/validator.py — pydantic schema v1)
   ├─ ingest_product_context     (cards/ingest.py — bge-m3 → Chroma upsert)
   ├─ build_event_tier_list      (events/* + rag/* + scoring/* + report/*)
   ├─ analyze_event_page         (acquisition/analyzer.py — verdict + hints, 1 Sonnet call)
   ├─ probe_exhibitor_endpoint   (acquisition/probe.py — XHR + embedded_json probes)
   ├─ acquire_exhibitor_source   (acquisition/acquire.py — orchestrator → artifact + manifest)
   ├─ draft_labels               (tools/draft_labels.py — Y1 labeling: GPT silver draft + flag → host refines)
   └─ sync_product_sources       (sources/indexer.py — WSL: raw source library → product_sources_{ws}, never scored)
```

Two-flow model:

1. **Product Context lifecycle** (one-time per product): draft → human edit → validate → ingest → Chroma `product_{workspace_id}` collection.
2. **Event Tier List pipeline** (per exhibitor list): source_capture → extraction (chunked, snippet-anchored) → enrichment (Brave web/news + fetch + cache) → fit retrieval (event → product, single direction) → scoring (7 dimensions + tier rules + evidence floor) → report (`tier_list.md` + `tier_list.yaml`).

Full pipeline diagram + evidence floor lifecycle: `docs/architecture.md`.

## DO NOT

- **MCP server import path stays cold.** `event_intel.mcp_server` (and every `providers/*.py` module) MUST NOT import `torch` / `transformers` / `sentence_transformers` / `chromadb` / `bitsandbytes` at module load. All heavy imports go inside method bodies. `tests/test_mcp_cold_start.py` regression-guards this. See `docs/playbook.md#3`.
- **Adapter / orchestration layers import via module reference, not symbol.** `from event_intel.providers import llm as _llm` + `_llm.AnthropicProvider(...)`, NOT `from event_intel.providers.llm import AnthropicProvider`. Symbol import binds at import time and silently slips false-greens in monkeypatched tests. Applies to events/, cards/, tools/, scoring/. See `docs/playbook.md#2`.
- **Cards schema authority is pydantic, sole.** `cards/schema.py` is the SSOT. Do NOT add a hand-maintained `config/capability_cards.schema.yaml` — JSON schema is generated on demand via `event-intel export-schema`. `tests/test_cards_schema_drift.py` snapshot-guards the schema. See `docs/playbook.md#8`.
- **User-provided slugs must be sanitized at every entry point.** `workspace_id` / `event_slug` / `event_name` all flow into filesystem paths and Chroma collection names. Every MCP tool entry runs `storage/identifiers.sanitize_slug(s)` first. Violations return `INVALID_INPUT` envelope with `hint.suggested_slug`. See `docs/playbook.md#7`.
- **Tool ok=false responses use the MCPError envelope, always.** 14 stable `error_code` values × 7 `stage` values (Phase 18T added 4 codes + `acquisition` stage). New error scenarios reuse the enum; don't invent ad-hoc error strings. Envelope shape is snapshot-tested. See `docs/playbook.md#6`.
- **Never call `urllib.robotparser.read()` directly.** It silently maps fetch failures (incl. 403 to Python-urllib UA) to `disallow_all=True` and surfaces them as a real `Disallow: /` policy. Use httpx with the shared `raw_fetch.get_user_agent()` + explicit status→policy mapping (200→parse, 404/410/401/403→allow, 5xx/transport→deny). See `docs/playbook.md#12` + `docs/lesson-learned.md` 2026-05-29 entry.
- **Event extraction caps are enforced, not advisory.** `max_chunks_per_event` (default 12) prevents 200k-char HTML pages from triggering 25 Sonnet calls. `max_companies` (default 30) caps enrichment. Both are yaml-driven (`config/defaults.yaml`).
- **Evidence floor is a 3-state lifecycle, not a single rule.** raw_extraction drops snippet-less candidates entirely; enriched layer tries to attach official_url + news; scoring uses `has_url + has_news` count to ceiling tier (2 → S/A, 1 → A max, 0 → B max). `needs_review` is orthogonal (extraction_confidence low OR enrichment hard fail), NOT a tier ceiling. See `docs/architecture.md`.
- **No FastAPI / no Web UI / no SQLite / no Notion / no separate ML worker.** Single-process FastMCP + Chroma + filesystem artifacts. The simpler architecture was an explicit choice for v0 (see plan v0.5 OOS list and `docs/backlog.md` for the deferred items). Don't introduce them without a new phase plan.

### Config is 3-tier — do not collapse

- **`.env`** → secrets only (`ANTHROPIC_API_KEY` or none for OAuth path, `BRAVE_API_KEY`). Gitignored. `.env.example` is the committed template. Auto-loaded via `python-dotenv` at `cli.py` + `mcp_server.py` module top.
- **`config/defaults.yaml`** → shipped non-secret defaults (extraction caps, scoring weights, tier rules, model names, `llm.provider: anthropic`). Committed.
- **`~/.event-intel/config.yaml`** (optional, active) → per-workspace user overrides, deep-merged over defaults. Most common use: `llm.provider: chatgpt_oauth` for cost-free experimentation. Not committed. See `docs/playbook.md#14`.

## Project docs convention

`docs/` has seven standing files — keep them current, don't let them rot:

- **`status.md`** — progress snapshot of what's **in flight or recently done**. Long-term plans live in `backlog.md`.
- **`backlog.md`** — long-term plan / out-of-scope / deferred items. P1/P2/P3 prioritized.
- **`architecture.md`** — two-flow pipeline + evidence floor + storage layout + error model. Updated when structure changes.
- **`lesson-learned.md`** — append-only, **failures only**.
- **`playbook.md`** — append-only, **successes only**. Patterns that survived a hard problem and are reusable elsewhere. Check first when stuck — grep the keyword index at the top.
- **`commands.md`** — full operational command catalog.
- **`security-audit.md`** — checklist + audit history.
- **`notion_db_schemas.md`** — currently a placeholder. v0 doesn't use Notion. Will be populated if backlog #7 (bd-coldcall-agent bridge) lands.

Before a significant commit, update whichever of these are affected. `README.md` should only describe what already works, not roadmap.

## Repository layout

```
event-intel-mcp/
  pyproject.toml
  README.md
  .env.example
  config/defaults.yaml
  src/event_intel/
    mcp_server.py        — FastMCP entry, 10 tool registrations
    cli.py               — typer thin wrapper (added in S2/S6)
    errors.py            — MCPError + 14 error_code × 7 stage enums
    runtime/             — preflight + models prepare (S1) + user config deep-merge
    providers/           — LLM (Anthropic | ChatGPTOAuth) + factory / Embedding / VectorStore / Search / Fetch ABCs
    cards/               — schema + drafter + validator + ingest (S2)
    events/              — source_capture + extraction + enrichment (S3+S4)
    acquisition/         — analyzer + probe + acquire + url_safety + robots + raw_fetch (Phase 18T)
    sources/             — source library indexer → product_sources_{ws} (WSL W1; raw source RAG, never scored)
    rag/                 — store + retriever + chunker
    scoring/             — dimensions + rules + compute (S4)
    report/              — tier_list_md + tier_list_yaml + brief_export (S5)
    tools/               — MCP tool handlers (one file per tool, 10 total)
    storage/             — workspaces + artifacts (atomic + sha256 manifest) + identifiers (sanitize_slug)
    prompts/{en,ko}/     — LLM prompt templates
  outputs/               — per-workspace per-event artifacts (gitignored except .gitkeep)
  tests/
    fixtures/events/     — sample HTML / CSV / Korean exhibitor pages
    fixtures/cards/      — sample capability_cards.yaml + source.md
```

## Relation to bd-coldcall-agent

event-intel-mcp is a **sibling project**, not a fork. The two share design DNA (3-tier config, monkeypatchable module imports, MCP-first surface, evidence-anchored LLM outputs) but no code. Bridge between them is `docs/backlog.md#7` (P3).

The lesson-learned + playbook content here was selectively pruned from bd-coldcall-agent's equivalents during S0 — only patterns that re-validate in this project's context were retained. Don't import bd-agent lessons wholesale.

## Planning

Active plan: `~/.claude/plans/tender-mixing-badger.md` (Plan v0.5 final, 3-round blind review history embedded). Resume context for any new session: read `docs/status.md` + the plan file before starting work.
