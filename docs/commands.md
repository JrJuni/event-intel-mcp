# Commands

Operational command catalog. CLAUDE.md `## Common commands` keeps only the five most-used invocations — anything debugging-specific or smoke-test-flavored lives here.

Use the project's conda Python directly (`~/miniconda3/envs/event-intel/python.exe`) — never bare `python` / `py` (Windows MS Store stub).

---

## Setup

```bash
# One-time env creation
~/miniconda3/Scripts/conda.exe create -n event-intel python=3.11 -y

# Install (editable mode + dev extras)
cd /c/Users/JuniBecky/Downloads/event-intel-mcp
~/miniconda3/envs/event-intel/python.exe -m pip install -e ".[dev]"

# Download bge-m3 (~1.3 GB) and verify runtime
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli models prepare
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli check-runtime --workspace default
```

### Optional: CJK morphological tokenizer (Phase 18W P2-4)

`category_fit` defaults to a char-bigram tokenizer (cold-start safe, no extra deps)
which over-matches CJK words sharing a 2-char window. For Japanese/Chinese exhibitor
lists, install the optional `[cjk]` extra (janome + jieba) and switch the mode in
`~/.event-intel/config.yaml`:

```bash
~/miniconda3/envs/event-intel/python.exe -m pip install -e ".[cjk]"
```

```yaml
# ~/.event-intel/config.yaml — deep-merged over config/defaults.yaml
scoring:
  cjk_tokenizer:
    mode: morphological   # bigram (default) | morphological
    language: auto        # auto | ja | zh | ko — SOURCE text language, NOT output lang
    han_default: zh       # auto's choice for ambiguous pure-Han runs (zh | ja)
```

Korean stays on bigram (no pure-Python analyzer). If the libraries are missing,
morphological silently falls back to bigram with a one-time warning.

---

## Tests

```bash
# All tests
cd /c/Users/JuniBecky/Downloads/event-intel-mcp
~/miniconda3/envs/event-intel/python.exe -m pytest -q

# Cold-start regression only (fast, no heavy ML)
~/miniconda3/envs/event-intel/python.exe -m pytest tests/test_mcp_cold_start.py -v

# Single test
~/miniconda3/envs/event-intel/python.exe -m pytest tests/test_cards_schema.py::test_round_trip -v

# With coverage
~/miniconda3/envs/event-intel/python.exe -m pytest --cov=event_intel --cov-report=term-missing
```

---

## Product context lifecycle

```bash
# 1. Draft capability_cards.yaml from source docs (Sonnet, chunked)
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli draft-cards \
    --source docs/product_brief.md \
    --workspace default \
    --lang en
# Writes outputs/default/capability_cards.draft.yaml

# 2. (Human edits the draft → saves as capability_cards.yaml)

# 3. Validate against pydantic schema v1
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli validate \
    --cards outputs/default/capability_cards.yaml

# 4. Ingest into mini-RAG (bge-m3 + Chroma)
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli ingest \
    --cards outputs/default/capability_cards.yaml \
    --workspace default

# Optional: export JSON schema (for external tooling / IDE hints)
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli export-schema --format json
```

---

## Event tier list pipeline

```bash
# From a saved HTML file (operator-assisted capture path — preferred)
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli build-event \
    --workspace default \
    --event-name "Sample Expo 2026" \
    --event-slug sample_expo_2026 \
    --html-file path/to/exhibitors.html \
    --lang en

# From a CSV export
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli build-event \
    --workspace default \
    --event-slug some_expo \
    --csv-file path/to/exhibitor_list.csv

# From pasted text (e.g. copied exhibitor block)
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli build-event \
    --workspace default \
    --event-slug some_expo \
    --text-file path/to/pasted.txt

# From a URL (best-effort, JS-heavy sites should use html-file instead)
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli build-event \
    --workspace default \
    --event-slug some_expo \
    --url https://event.example.com/exhibitors

# Resume after a failure
~/miniconda3/envs/event-intel/python.exe -m event_intel.cli build-event \
    --workspace default \
    --event-slug sample_expo_2026 \
    --html-file path/to/exhibitors.html \
    --resume-from enrichment    # 'extraction' | 'enrichment' | 'scoring'

# Each run writes outputs/{workspace}/{event_slug}/{raw_source.*, exhibitor_candidates.yaml,
# enriched_exhibitors.yaml, tier_list.md, tier_list.yaml, run_summary.json}
```

---

## MCP server (stdio)

```bash
# Run the MCP server directly (Claude Desktop spawns this)
~/miniconda3/envs/event-intel/python.exe -m event_intel.mcp_server

# Or via the installed entry point
~/miniconda3/envs/event-intel/Scripts/event-intel mcp-server
```

`claude_desktop_config.json` snippet:

```json
{
  "mcpServers": {
    "event-intel": {
      "command": "C:/Users/JuniBecky/miniconda3/envs/event-intel/python.exe",
      "args": ["-m", "event_intel.mcp_server"]
    }
  }
}
```

---

## Debugging

```bash
# Verify heavy deps are importable (post pip install)
~/miniconda3/envs/event-intel/python.exe -c "import torch, chromadb, sentence_transformers, anthropic; print('OK')"

# Confirm mcp_server import is cold (no heavy ML in sys.modules)
~/miniconda3/envs/event-intel/python.exe -c "from event_intel.mcp_server import app; import sys; assert 'torch' not in sys.modules and 'chromadb' not in sys.modules; print('cold-start OK')"

# List Chroma collections for a workspace
~/miniconda3/envs/event-intel/python.exe -c "
from event_intel.providers.vectorstore import ChromaProvider
p = ChromaProvider()
print(p._get_client().list_collections())
"

# Inspect a run summary
cat outputs/default/sample_expo_2026/run_summary.json | python -m json.tool
```

---

## Notes

- All Chroma data lives at `~/.event-intel/chroma/{workspace_id}/`. Safe to delete and re-ingest.
- Brave per-call cache at `outputs/{ws}/{event}/cache/brave/{hash}.json`. Delete cache to force re-search.
- For environment setup gotchas (conda env, Windows path quoting, `[dev]` glob expansion), see `docs/lesson-learned.md`.
- For phase-by-phase progress, see `docs/status.md`.
