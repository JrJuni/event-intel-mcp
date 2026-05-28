# event-intel-mcp

Turn exhibitor lists into evidence-backed BD target tier lists via MCP.

## Status

Pre-alpha. v0 surface complete (S0–S6) — 5 MCP tools live, 171/171 tests green. Awaiting real-exhibition smoke + Claude Desktop registration. See `docs/status.md` for stream-by-stream history and `~/.claude/plans/tender-mixing-badger.md` for the design plan.

## Install

```powershell
conda create -n event-intel python=3.11 -y
conda activate event-intel
pip install -e .
```

## First-time setup

```powershell
event-intel models prepare        # download bge-m3 (~1.3 GB, one-time)
event-intel check-runtime         # verify model + vectorstore + APIs
```

## Workflow

1. **Draft Product Context**
   ```powershell
   event-intel draft-cards --source docs/product.md --workspace default
   # Hand-edit outputs/default/capability_cards.draft.yaml → save as capability_cards.yaml
   event-intel validate --cards outputs/default/capability_cards.yaml
   event-intel ingest --cards outputs/default/capability_cards.yaml --workspace default
   ```

2. **Build Event Tier List**
   ```powershell
   event-intel build-event `
     --workspace default --event-name "Sample Expo" `
     --event-slug sample_expo --html-file path/to/exhibitors.html
   ```

3. **Inspect results**
   - `outputs/default/sample_expo/tier_list.md` — human-readable
   - `outputs/default/sample_expo/tier_list.yaml` — machine-readable

## Operator-Assisted Capture

JS-heavy exhibitor pages (infinite scroll, login-walled) are not auto-crawled in v0. Use one of:

- **Saved HTML**: open the page in a browser, scroll to load all exhibitors, then `Ctrl+S` → "Webpage, Complete"
- **CSV export**: many event organizers offer downloadable participant lists
- **Pasted text**: copy the exhibitor block, save to a `.txt` file

All three feed into `build-event` via `--html-file` / `--csv-file` / `--text-file`.

## MCP integration

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "event-intel": {
      "command": "C:/Users/.../miniconda3/envs/event-intel/python.exe",
      "args": ["-m", "event_intel.mcp_server"]
    }
  }
}
```

5 tools become available: `check_runtime`, `draft_capability_cards`, `validate_capability_cards`, `ingest_product_context`, `build_event_tier_list`.

## Troubleshooting — `error_code` → fix

Every tool that fails returns the same envelope shape:

```json
{ "ok": false, "error_code": "...", "stage": "...", "message": "...", "hint": {...}, "retryable": false }
```

10 stable `error_code` values × 6 `stage` values cover the full taxonomy:

| `error_code` | What happened | Fix |
|---|---|---|
| `INVALID_INPUT` | A user-facing slug (`workspace_id`, `event_slug`) violated `^[a-zA-Z0-9_-]{1,64}$`, or a required arg was empty | Use `hint.suggested_slug` (auto-generated, ASCII-safe). For Korean input like `"서울 ITS 2026"` it returns something like `"its-2026"`; for pure non-ASCII it falls back to `event-{8-hex-sha1}` — always deterministic per input. |
| `MODEL_NOT_READY` | bge-m3 weights not cached locally | Run `event-intel models prepare` once (~1.3 GB download). |
| `SCHEMA_ERROR` | `capability_cards.yaml` fails pydantic validation | Read `hint.errors[].path` for the field path (e.g. `capabilities[0].keywords` needs ≥3 entries). Re-edit yaml, re-run `event-intel validate`. |
| `RATE_LIMITED` | Brave or Anthropic returned 429 | `retryable=true` — wait per `hint.retry_after` and re-run. v0 has no auto-backoff; v0.4+ planned. |
| `UPSTREAM_ERROR` | Anthropic / Brave API call failed for non-rate reasons (timeout, 5xx, malformed response) | `retryable=true` — re-run. If persistent, check `~/.bd-coldcall/logs/` for the underlying error. |
| `IO_ERROR` | Filesystem unwritable (Chroma persist dir, output dir) | Check `hint.path` and adjust permissions or `EVENT_INTEL_CHROMA_DIR` / `EVENT_INTEL_OUTPUT_DIR`. |
| `INTERNAL` | An unexpected exception escaped a tool handler — bug | Capture the full envelope (`message` carries `TypeName: detail`) and file an issue. |
| `PRODUCT_CONTEXT_MISSING` | `build_event_tier_list` (or `check_runtime`) found no chunks in the `product_{workspace_id}` Chroma collection | Run `event-intel ingest --cards <path> --workspace <ws>` first. The `hint.collection` field names the missing collection. |
| `SOURCE_CAPTURE_FAILED` | Bad source — file not found, empty CSV, unsupported `source_kind`, or trafilatura got zero text | Check `hint.expected_path` / `hint.supported`. For JS-heavy pages, capture with Save-As "Webpage, Complete" then point `--html-file` at the saved file. |
| `CONFIG_ERROR` | `config/defaults.yaml` is missing a required nested key, or an API key in `.env` is missing/invalid | `hint.missing_key` is dotted (e.g. `scoring.weights.capability_fit`). Add it back. For API keys, copy `.env.example` → `.env` and fill in. |

`stage` values pinpoint *where* in the pipeline the error fired: `preflight` (slug / config / model-ready / product-context / API-key checks), `extraction` (source capture or LLM extraction), `enrichment` (Brave search), `scoring` (weighted sum / tier decision / rationale), `report` (md/yaml render), `ingest` (capability cards lifecycle).

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
