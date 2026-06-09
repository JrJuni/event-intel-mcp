# event-intel-mcp

Turn exhibitor lists into evidence-backed BD target tier lists via MCP.

## Status

Pre-alpha. v0 + acquisition layer (Phase 18T) + ChatGPT-OAuth install UX (18T.1) + zero-friction `.mcpb` install (18T.2) — 8 MCP tools live, 371/371 tests green. Real-exhibition smoke (≥2 verdicts) done; Claude Desktop registration via the `.mcpb` bundle (see `mcpb/`). See `docs/status.md` for stream-by-stream history.

## Install

```powershell
conda create -n event-intel python=3.11 -y
conda activate event-intel
pip install -e .
```

## Choosing an LLM provider

The LLM (used for capability-card drafting, exhibitor extraction, page analysis, and S/A rationales) runs through one of two providers. Pick one:

**Option 1 — Anthropic API key (default).** Put `ANTHROPIC_API_KEY=...` in `.env` (or supply it in the `.mcpb` install form). Nothing else to do.

**Option 2 — ChatGPT Plus/Pro subscription (OAuth).** No API key. Authenticate once with a browser, then tokens are cached at `~/.event-intel/chatgpt_auth.json` and auto-refreshed.

```powershell
# Turn it on (choose one):
#   - check "Use ChatGPT Plus/Pro subscription" in the .mcpb install form, OR
#   - set the env var:           $env:EVENT_INTEL_USE_CHATGPT_OAUTH = "true"
#   - or in ~/.event-intel/config.yaml:   llm: { provider: chatgpt_oauth }

event-intel login-chatgpt          # one-time browser login (run again with --force to re-auth)
```

Notes:
- `EVENT_INTEL_USE_CHATGPT_OAUTH` is **opt-in only** — a falsey/empty value never overrides an existing config; it just leaves your `config.yaml` / `.env` choice in place.
- Power users can set `EVENT_INTEL_LLM_PROVIDER=anthropic|chatgpt_oauth` for an explicit, authoritative override (an invalid value fails fast with `CONFIG_ERROR`).
- The OAuth path uses an unofficial Codex-CLI client and is intended for **personal local use only** — do not deploy it as a shared server.

## First-time setup

```powershell
event-intel models prepare          # download bge-m3 (~1.3 GB, one-time)
event-intel check-runtime           # verify model + vectorstore + APIs
event-intel check-runtime --warm-up # also preload bge-m3 into memory (optional)
```

**Avoiding first-build latency.** The bge-m3 model (~1.3 GB) loads on first use (~10–20s) and is then cached for the life of the server process. To pay that cost up front instead of on your first `build_event_tier_list`:

- **Terminal:** `event-intel check-runtime --warm-up` loads the model inline and waits, reporting `warm_up.load_seconds`.
- **Claude Desktop:** call `check_runtime` with `warm_up: true`. It returns *immediately* with `warm_up.status: "warming"` (it never blocks on the load, so it can't hit the MCP client timeout). Call `check_runtime` again after a minute — once `warm_up.status` reads `"ready"`, `build_event_tier_list` will reuse the cached model and run fast.
- **Automatic (opt-in):** set `EVENT_INTEL_WARM_ON_START=true` (or check "Preload the embedding model when Claude Desktop starts" in the `.mcpb` form) to warm in the background the moment the server starts — zero-touch. Off by default; only runs if the model is already downloaded, and adds ~1.3 GB resident memory for the session.

## Workflow (CLI)

Two flows. The **Product Context lifecycle** (step 1) runs once per product to teach the system what you sell; the **Event pipeline** (steps 2–4) runs per exhibitor list. The same operations are available as MCP tools in Claude Desktop (see [Use it in Claude Desktop](#use-it-in-claude-desktop)).

1. **Draft Product Context** *(one-time per product)*
   ```powershell
   event-intel draft-cards --source docs/product.md --workspace default
   # Hand-edit outputs/default/capability_cards.draft.yaml → save as capability_cards.yaml
   event-intel validate --cards outputs/default/capability_cards.yaml
   event-intel ingest --cards outputs/default/capability_cards.yaml --workspace default
   ```

   *Optional — richer drafts from a source library:* drop product docs
   (PDF / MD / TXT / CSV) under `<workspace>/sources/`, index them, then draft
   from that grounded context. The raw source improves drafts + adds S/A
   rationale provenance — it never affects a score.
   ```powershell
   event-intel sources sync --workspace default            # index sources/ → product_sources_{ws}
   event-intel draft-cards --workspace default --from-workspace
   event-intel ingest --cards ... --workspace default --sync-sources   # opt-in re-index on ingest
   ```

2. **Acquire Source (URL → artifact)**
   ```powershell
   event-intel acquire-source `
     --workspace default --event-slug sample_expo `
     --url https://example-expo.com/exhibitors
   # Writes ~/.event-intel/artifacts/default/sample_expo/source.html + manifest.json
   # Returns source_kind + source_ref for the next step
   ```

3. **Build Event Tier List**
   ```powershell
   event-intel build-event `
     --workspace default --event-name "Sample Expo" `
     --event-slug sample_expo --html-file path/to/exhibitors.html
   ```

4. **Inspect results**
   - `<workspace>/default/sample_expo/tier_list.md` — human-readable
   - `<workspace>/default/sample_expo/tier_list.yaml` — machine-readable

## Where things live (folders & migration)

Two roots:

- **Working folder** (cards, `sources/`, event reports). A fresh install uses **`~/EventIntel`**; an existing checkout keeps using **`<repo>/outputs`** (a transparent back-compat fallback) until you migrate.
- **Local data folder** `~/.event-intel` (Chroma vector store, artifacts, cache, source index). Default unchanged.

Resolution precedence per location: fine-grained env (`EVENT_INTEL_OUTPUT_DIR` / `_CHROMA_DIR` / `_ARTIFACTS_DIR`) → coarse env (`EVENT_INTEL_WORKSPACE_DIR` / `EVENT_INTEL_DATA_DIR`) → `config.paths.*` → built-in default. In the `.mcpb` install form, the **Working folder** / **Local data & RAG folder** fields set the coarse env vars (leave blank for the defaults). `event-intel check-runtime` prints the resolved `paths` so you can confirm where everything lands.

To move an existing `<repo>/outputs` tree to `~/EventIntel` (non-destructive — copies are checksum-verified, the source is never deleted, conflicts are reported not overwritten):

```powershell
event-intel storage migrate            # dry-run: shows what would copy
event-intel storage migrate --apply    # execute
```

Chroma & artifacts under `~/.event-intel` are not moved (data root unchanged), so no server shutdown is needed.

## Source Acquisition (Phase 18T)

Three upstream tools let you hand the agent **a URL** instead of a pre-captured file. The agent classifies the page, probes the right extraction path, and writes a raw artifact to disk — then `build_event_tier_list` consumes it exactly as before.

### Three tools

| Tool | Purpose | LLM calls |
|---|---|---|
| `analyze_event_page(url)` | Fetch the landing page, classify it into one of 5 verdicts | 1 (Sonnet) |
| `probe_exhibitor_endpoint(url, hints)` | Given analyzer hints, fire deterministic HTTP candidates and return the best match | 0 |
| `acquire_exhibitor_source(url, workspace_id, event_slug)` | End-to-end orchestrator: analyze → probe → fetch → artifact + manifest | ≤1 |

### Five verdicts

| Verdict | Meaning | What `acquire` does |
|---|---|---|
| `static_html` | Exhibitor list is in the initial HTML | GET + write `source.html` |
| `xhr_endpoint` | Page loads data via XHR/AJAX (e.g. jQuery `.post`) | Probe candidate endpoints, paginate up to 3 pages, write `source.html` |
| `embedded_json` | Data is in an inline `<script>` (`__NEXT_DATA__`, `window.__INITIAL_STATE__`) | stdlib regex extract + JSON parse + write `source.json` (`text_file` kind) |
| `operator_capture_required` | Heavy JS / lazy-load / infinite scroll | Returns `OPERATOR_CAPTURE_REQUIRED` → see Operator-Assisted Capture below |
| `login_required` | Paywall / OAuth gate / member-only | Returns `LOGIN_REQUIRED` — permanent dead end |

### URL safety and robots policy

Every tool that issues an HTTP request runs two gates before the first byte goes out:

1. **URL safety** — rejects `http://localhost`, `http://10.x.x.x`, `http://192.168.x.x`, `ftp://…`, `user:pass@…`, bare hostnames. Redirect targets are re-validated. Violation → `INVALID_INPUT`.
2. **robots.txt** — stdlib `urllib.robotparser` with 1-hour in-memory cache. Disallowed path → `ROBOTS_DISALLOWED`. Hint includes the robots URL and a suggested fix.

Neither gate can be skipped by one tool trusting another's validation — each tool is independently safe.

### Caching

`acquire_exhibitor_source` writes `manifest.json` alongside the artifact (sha256 + verdict + source_kind + fetched_at). On re-run, if the manifest exists and sha256 matches → 0 fetches, 0 LLM calls. Pass `refetch=True` to force re-acquisition.

### Inspect a page without acquiring

```powershell
event-intel analyze-page --url https://example-expo.com/exhibitors
# Returns verdict + hints + confidence without writing anything to disk
```

## Operator-Assisted Capture

JS-heavy exhibitor pages (infinite scroll, login-walled) are not auto-crawled in v0. Use one of:

- **Saved HTML**: open the page in a browser, scroll to load all exhibitors, then `Ctrl+S` → "Webpage, Complete"
- **CSV export**: many event organizers offer downloadable participant lists
- **Pasted text**: copy the exhibitor block, save to a `.txt` file

All three feed into `build-event` via `--html-file` / `--csv-file` / `--text-file`.

## Use it in Claude Desktop

### Option A — `.mcpb` bundle (recommended)

The repo ships a Claude Desktop extension bundle, so you install through a UI form instead of hand-editing JSON. It's ~4 KB and points at your live repo + interpreter (it does not bundle the Python source or the ~3 GB ML deps).

1. Build the bundle (needs `npm i -g @anthropic-ai/mcpb`):
   ```powershell
   cd mcpb
   mcpb pack . event-intel-mcp-0.5.0.mcpb
   ```
2. Claude Desktop → **Settings → Extensions** → drag the `.mcpb` onto the pane (or "Install from file").
3. Fill the form — most fields are pre-filled or optional:
   - **Python interpreter path** — pre-filled with `${HOME}/miniconda3/envs/event-intel/python.exe`; just confirm or adjust. (Must be an interpreter that ran `pip install -e .`; the editable install means no repo path / `PYTHONPATH` is needed.)
   - **Brave Search API key** — optional; only used when `search.provider: brave`. The default search backend is `ddgs` (DuckDuckGo, keyless) so enrichment works with no key. Leave blank to use `BRAVE_API_KEY` from the repo's `.env`.
   - **Use ChatGPT Plus/Pro subscription** — check to use ChatGPT OAuth instead of an Anthropic key (then run `event-intel login-chatgpt` once in a terminal); leave unchecked for the Anthropic path.
   - **Anthropic API key** — optional; leave blank to use `ANTHROPIC_API_KEY` from `.env` (or if you checked the ChatGPT box).
   - **Preload model on start** — optional; see "Avoiding first-build latency" above.
4. Restart Claude Desktop → the 8 tools appear in the tool picker.

The server loads API keys from the repo's `.env` automatically (it derives the repo root from the editable install), so blank form keys fall back to `.env`; a key typed into the form wins over `.env`.

Details in [`mcpb/README.md`](mcpb/README.md).

### Option B — manual config

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "event-intel": {
      "command": "C:/Users/.../miniconda3/envs/event-intel/python.exe",
      "args": ["-m", "event_intel.mcp_server"],
      "env": {
        "BRAVE_API_KEY": "...",
        "ANTHROPIC_API_KEY": "...",
        "EVENT_INTEL_USE_CHATGPT_OAUTH": "true"
      }
    }
  }
}
```

Include `ANTHROPIC_API_KEY` only for the Anthropic path. Set `EVENT_INTEL_USE_CHATGPT_OAUTH=true` to opt into ChatGPT OAuth (then run `event-intel login-chatgpt` once); omit it otherwise.

### The 13 tools

**In-app setup** — one-time first run (no terminal needed):

| Tool | Purpose |
|---|---|
| `prepare_models` | Download the bge-m3 model (~1.3 GB) in-app. *Non-blocking* — returns `downloading` at once; poll `check_runtime` until `ready`. |
| `login_chatgpt` | Authenticate the ChatGPT subscription (OAuth) LLM path in-app. *Non-blocking* — opens a browser, returns `pending`; approve, then poll `check_runtime` until `logged_in`. |

**Product Context lifecycle** — one-time per product:

| Tool | Purpose |
|---|---|
| `check_runtime` | Verify bge-m3 cache / Chroma / API keys / product context. `warm_up: true` starts a *non-blocking* background model load (returns at once); `checks.warm_up.status` reports `not_started`/`warming`/`ready` — poll by calling again. The response always carries a `paths` block (where cards / sources / Chroma live + writability) and a `setup` block (bge-m3 download + ChatGPT login state). |
| `sync_product_sources` | Index a workspace's raw source library (PDF/MD/TXT/CSV) into `product_sources_{workspace_id}` for richer drafts + rationale provenance (never scored) |
| `draft_capability_cards` | Draft a `capability_cards.yaml` from a source doc (md/txt/pdf), inline text, or the synced source library (`source_kind="workspace"`) |
| `validate_capability_cards` | Validate a hand-edited `capability_cards.yaml` against the pydantic schema (v1) |
| `ingest_product_context` | Embed validated cards via bge-m3 → Chroma `product_{workspace_id}` collection (opt-in `sync_sources` re-indexes the source library first) |

**Event pipeline** — per exhibitor list:

| Tool | Purpose |
|---|---|
| `analyze_event_page` | Classify an exhibitor-page URL into one of 5 verdicts + return acquisition hints |
| `probe_exhibitor_endpoint` | Deterministic HTTP probe of the analyzer's candidate endpoints (0 LLM) |
| `acquire_exhibitor_source` | Orchestrate analyze → probe → fetch → artifact + manifest (URL → `source_ref`) |
| `build_event_tier_list` | Run capture → extraction → enrichment → scoring → `tier_list.md` + `tier_list.yaml` |

**Benchmark labeling** — `draft_labels` (multi-vendor gold-label production for accuracy measurement).

**Remote I/O foundation (Y2.1, in progress)** — `get_job` polls a background job (status + result artifact ids). The card/source/event inputs also accept `*_content` / `*_artifact_id` (not just a server-local path), and reports/drafts return `*_artifact_id` — so the workflow can run path-free. This is the groundwork for remote serving (a later phase); today it still runs over local stdio.

In Claude Desktop you don't invoke these by hand — ask in natural language (e.g. *"analyze this exhibitor page: &lt;url&gt;"*, *"build a tier list from this CSV against my default workspace"*) and Claude calls the tools for you. The CLI commands below are the same code paths for terminal use.

## Troubleshooting — `error_code` → fix

Every tool that fails returns the same envelope shape:

```json
{ "ok": false, "error_code": "...", "stage": "...", "message": "...", "hint": {...}, "retryable": false }
```

14 stable `error_code` values × 7 `stage` values cover the full taxonomy:

| `error_code` | What happened | Fix |
|---|---|---|
| `INVALID_INPUT` | A slug violated `^[a-zA-Z0-9_-]{1,64}$`, a required arg was empty, or the URL failed a safety check (private IP, non-http scheme, userinfo) | For slugs: use `hint.suggested_slug`. For URLs: check `hint.reason`. |
| `MODEL_NOT_READY` | bge-m3 weights not cached locally | Run `event-intel models prepare` once (~1.3 GB download). |
| `SCHEMA_ERROR` | `capability_cards.yaml` fails pydantic validation | Read `hint.errors[].path` (e.g. `capabilities[0].keywords` needs ≥3 entries). Re-edit yaml, re-run `event-intel validate`. |
| `RATE_LIMITED` | The search backend or Anthropic returned 429 | `retryable=true` — wait per `hint.retry_after` and re-run. (The `ddgs` backend absorbs its own rate limits via throttle/backoff and degrades to empty + a `degraded` warning rather than this code.) |
| `UPSTREAM_ERROR` | Anthropic / search backend / HTTP call failed for non-rate reasons (timeout, 5xx, DNS, malformed response) | `retryable=true` — re-run. If from a network fetch, the underlying error is in `hint`. |
| `IO_ERROR` | Filesystem unwritable (Chroma persist dir, output dir) | Check `hint.path` and adjust permissions or `EVENT_INTEL_CHROMA_DIR` / `EVENT_INTEL_OUTPUT_DIR`. |
| `INTERNAL` | Unexpected exception escaped a tool handler — bug | Capture the full envelope (`message` carries `TypeName: detail`) and file an issue. |
| `PRODUCT_CONTEXT_MISSING` | `build_event_tier_list` found no chunks in `product_{workspace_id}` | Run `event-intel ingest --cards <path> --workspace <ws>` first. `hint.collection` names the missing collection. |
| `SOURCE_CAPTURE_FAILED` | File not found, empty CSV, unsupported `source_kind`, or trafilatura got zero text | Check `hint.expected_path` / `hint.supported`. For JS-heavy pages, use operator-assisted capture (below). |
| `CONFIG_ERROR` | `config/defaults.yaml` is missing a required key, or an API key in `.env` is missing/invalid | `hint.missing_key` is dotted (e.g. `scoring.weights.capability_fit`). Copy `.env.example` → `.env` for API keys. |
| `ACQUISITION_AMBIGUOUS` | `probe_exhibitor_endpoint` fired all candidates but none scored above the exhibitor-list threshold | `hint.attempts` lists every candidate URL + score. Try `analyze_event_page` again or use operator-assisted capture. |
| `LOGIN_REQUIRED` | 401/403, or analyzer classified the page as login-walled | `hint.fix` describes next steps. Check for an official exhibitor API or contact the organizer. |
| `OPERATOR_CAPTURE_REQUIRED` | Page is heavy JS / CAPTCHA / bot-wall / too dynamic to auto-fetch | See Operator-Assisted Capture section below. `hint.fix` has exact steps. |
| `ROBOTS_DISALLOWED` | `robots.txt` disallows crawling the URL with user-agent `event-intel-mcp` | `hint.robots_url` + `hint.fix`. Contact the site owner or use operator-assisted capture. |

`stage` values pinpoint where in the pipeline the error fired:

| `stage` | Covers |
|---|---|
| `acquisition` | URL safety, robots check, analyze/probe/acquire tools |
| `preflight` | Slug validation, config, model-ready, product-context, API-key checks |
| `extraction` | Source capture or LLM extraction |
| `enrichment` | web/news search (ddgs / searxng / brave) |
| `scoring` | Weighted sum, tier decision, rationale |
| `report` | Markdown/yaml render |
| `ingest` | Capability cards lifecycle |

Example — Korean event slug rejection:

```json
{
  "ok": false,
  "error_code": "INVALID_INPUT",
  "stage": "preflight",
  "message": "event_slug '서울 ITS 2026' violates [a-zA-Z0-9_-]{1,64}",
  "hint": {
    "rule": "^[a-zA-Z0-9_-]{1,64}$",
    "suggested_slug": "its-2026",
    "field": "event_slug"
  },
  "retryable": false
}
```

The `suggested_slug` is always itself a valid slug — paste it back and re-run.

## License

(TBD)
