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

### Optional: CJK morphological tokenizer (Phase 18W P2-4 / 18X)

`category_fit` defaults to a char-bigram tokenizer (cold-start safe, no extra deps)
which over-matches CJK words sharing a 2-char window. Install the optional extras
and switch the mode in `~/.event-intel/config.yaml`:

```bash
# Japanese + Chinese (pure-Python: janome + jieba)
~/miniconda3/envs/event-intel/python.exe -m pip install -e ".[cjk]"
# Korean (kiwipiepy — NATIVE wheel + ~109 MB model; separate extra)
~/miniconda3/envs/event-intel/python.exe -m pip install -e ".[kr]"
# or both:
~/miniconda3/envs/event-intel/python.exe -m pip install -e ".[cjk,kr]"
```

```yaml
# ~/.event-intel/config.yaml — deep-merged over config/defaults.yaml
scoring:
  cjk_tokenizer:
    mode: morphological   # bigram (default) | morphological
    language: auto        # auto | ja | zh | ko — SOURCE text language, NOT output lang
    han_default: zh       # auto's choice for ambiguous pure-Han runs (zh | ja)
```

Backends: ja→janome, zh→jieba (`[cjk]`), ko→kiwipiepy (`[kr]`). If a backend's
library is missing, morphological falls back to bigram with a one-time warning.

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

## Y1 benchmark — accuracy measurement + multi-vendor gold labeling

The `benchmark` sub-app runs the blind measurement state machine (design v4 §2)
and the multi-vendor labeling system (plan `snoopy-weaving-robin.md` v3). The
labeler is a DIFFERENT vendor than the engine (engine = chatgpt_oauth ↔ labeler =
Claude) so gold stays independent. **silver** = single-vendor auto-accepted draft
(DEV only); **gold** = cross-vendor agreement / search-refine / human (the
holdout gate accepts gold only).

```bash
PY=~/miniconda3/envs/event-intel/python.exe

# (1) Freeze the gate contract BEFORE any label is seen. --gates-file carries the
#     complete per-pair gate set (required/optional/not_applicable + coverage).
$PY -m event_intel.cli benchmark threshold-freeze --out benchmarks/gold/thresholds.json \
  [--universe-file universe.json] [--gates-file gates.json]

# (2) Hidden run (gold-blind): build the tier list, persist an immutable run-result.
$PY -m event_intel.cli benchmark run --pair p1_mongodb_gtc --runs-root benchmarks/runs \
  --workspace mongodb --event-name "NVIDIA GTC 2026" --event-slug gtc2026 \
  --csv-file outputs/mongodb/gtc2026_exhibitors.csv --no-enrich --no-rationale

# (3) Blind company packet (names only; full or top10_decoy cohort)
$PY -m event_intel.cli benchmark company-packet --pair p1_mongodb_gtc \
  --roster benchmarks/gold/p1_mongodb_gtc/roster.json --cohort full \
  --out benchmarks/_local/p1_mongodb_gtc/company_packet.json

# (4) Labeling sheet = packet + NEUTRAL source overviews + product rubric (no engine verdict)
$PY -m event_intel.cli benchmark labeling-sheet --pair p1_mongodb_gtc \
  --packet benchmarks/_local/p1_mongodb_gtc/company_packet.json \
  --source outputs/mongodb/gtc2026_exhibitors.csv --source-format csv \
  --name-key name --overview-keys description --url-key url \
  --card outputs/mongodb/capability_cards.yaml \
  --out-json benchmarks/_local/p1_mongodb_gtc/labeling_sheet.json \
  --out-md benchmarks/_local/p1_mongodb_gtc/worksheet.md

# (5a) Stage A+B: GPT-OAuth single-vendor draft (silver) + flag gate-class/low-conf
$PY -m event_intel.cli benchmark draft-labels \
  --sheet benchmarks/_local/p1_mongodb_gtc/labeling_sheet.json \
  --card outputs/mongodb/capability_cards.yaml \
  --out benchmarks/_local/p1_mongodb_gtc/drafted.json --lang en

# (5b) Gold via cross-vendor agreement (Claude, GPT-blind). Prove independence with the view SHA.
$PY -m event_intel.cli benchmark independent-view --sheet …/drafted.json --out …/view.json   # prints input_sha
#   → an independent Claude pass labels view.json → claude_labels.json
$PY -m event_intel.cli benchmark cross-vendor --sheet …/drafted.json \
  --claude-labels …/claude_labels.json --input-sha <sha> --out …/crossed.json

# (5c) Gold via search refine (host/agent web-searches the flagged rows)
$PY -m event_intel.cli benchmark apply-refinements --sheet …/crossed.json \
  --refinements …/refinements.json --out …/refined.json

# (6) Seal labels (grade preserved) → (7) measure
$PY -m event_intel.cli benchmark seal-labels --sheet …/refined.json \
  --packet …/company_packet.json --out benchmarks/gold/p1_mongodb_gtc/sealed_labels.json
$PY -m event_intel.cli benchmark measure --run-dir benchmarks/runs/p1_mongodb_gtc/<run_id> \
  --roster …/roster.json --sealed-labels …/sealed_labels.json \
  --thresholds benchmarks/gold/thresholds.json --target-mode customer   # add --waiver <gate> if ineligible
#   holdout gate: pass --holdout (rejects any non-gold label)

# Labeling-process trust metrics (gold/flag/flip rate)
$PY -m event_intel.cli benchmark label-stats --sheet …/refined.json
```

- **Data hygiene**: working sheets/worksheets/drafts live under gitignored
  `benchmarks/_local/`; only `sealed_labels` / `roster` / `measure_report` /
  threshold manifests are committable under `benchmarks/gold/`.
- **Holdout discipline**: a pair whose labels were seen can't be a holdout
  (re-freeze won't restore it). The competitor holdout (MongoDB × AI Expo Tokyo)
  stays blind until its one-shot holdout run.

---

## Notes

- All Chroma data lives at `~/.event-intel/chroma/{workspace_id}/`. Safe to delete and re-ingest.
- Brave per-call cache at `outputs/{ws}/{event}/cache/brave/{hash}.json`. Delete cache to force re-search.
- For environment setup gotchas (conda env, Windows path quoting, `[dev]` glob expansion), see `docs/lesson-learned.md`.
- For phase-by-phase progress, see `docs/status.md`.
