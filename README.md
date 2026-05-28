# event-intel-mcp

Turn exhibitor lists into evidence-backed BD target tier lists via MCP.

## Status

Pre-alpha. v0 in active development. See `~/.claude/plans/tender-mixing-badger.md` for the design plan.

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

## License

(TBD)
