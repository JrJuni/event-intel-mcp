# mcpb — Claude Desktop bundle

This folder builds `event-intel-mcp.mcpb`, a [Claude Desktop MCP Bundle](https://github.com/modelcontextprotocol/mcpb) for one-click install.

## Why .mcpb (and not manual `claude_desktop_config.json` editing)

The bundle ships the manifest + user-config schema. When the user double-clicks `event-intel-mcp-{version}.mcpb`, Claude Desktop prompts for the required paths/keys via a UI form instead of asking the user to hand-edit JSON. This is the recommended path for any MCP server distributed by an author the recipient doesn't have explicit trust in — install friction stays low, secrets stay in the OS keychain rather than a plaintext config file.

This bundle is **lightweight** (~3 KB) — it does NOT bundle the Python source or the ~3 GB ML deps (torch / transformers / chromadb). Instead, the manifest takes two user-config paths:

- **`python_path`** — the user's Python interpreter that already has `pip install -e .` run against this repo (typically `~/miniconda3/envs/event-intel/python.exe`)
- **`repo_path`** — the local clone of `event-intel-mcp` (PYTHONPATH gets set to `{repo_path}/src` so `-m event_intel.mcp_server` resolves)

This trades self-containment for a much smaller bundle that always tracks the user's live working copy.

## Build

```bash
cd mcpb
mcpb validate manifest.json          # schema check
mcpb pack . event-intel-mcp-0.2.0.mcpb
mcpb info event-intel-mcp-0.2.0.mcpb # confirm size + contents
```

The `.mcpb` output is gitignored (it's a build artifact, version-stamped in the filename).

`mcpb` CLI: `npm install -g @anthropic-ai/mcpb` (Node.js 18+).

## Install

1. Open Claude Desktop → Settings → Extensions
2. Drag `event-intel-mcp-{version}.mcpb` onto the Extensions pane (or click "Install from file")
3. Fill the user_config form:
   - **Python interpreter path** — e.g. `C:\Users\<you>\miniconda3\envs\event-intel\python.exe`
   - **Repo path** — e.g. `C:\Users\<you>\Downloads\event-intel-mcp`
   - **Brave Search API key** — required for `build_event_tier_list`
   - **Anthropic API key** — required only if `llm.provider=anthropic` (default). Leave empty if you've switched to `chatgpt_oauth` in `~/.event-intel/config.yaml`.
4. Restart Claude Desktop
5. Verify the 8 tools appear in the tool picker: `check_runtime`, `draft_capability_cards`, `validate_capability_cards`, `ingest_product_context`, `build_event_tier_list`, `analyze_event_page`, `probe_exhibitor_endpoint`, `acquire_exhibitor_source`

## Version bump

When releasing a new version:

1. Update `version` in `manifest.json`
2. Update the `tools[]` list if the MCP tool surface changed
3. Rebuild with `mcpb pack . event-intel-mcp-{new_version}.mcpb`
4. Optionally `mcpb sign` if you have a code-signing cert
