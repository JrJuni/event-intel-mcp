# mcpb — Claude Desktop bundle

This folder builds `event-intel-mcp.mcpb`, a [Claude Desktop MCP Bundle](https://github.com/modelcontextprotocol/mcpb) for one-click install.

## Why .mcpb (and not manual `claude_desktop_config.json` editing)

The bundle ships the manifest + user-config schema. When the user double-clicks `event-intel-mcp-{version}.mcpb`, Claude Desktop prompts for the required paths/keys via a UI form instead of asking the user to hand-edit JSON. This is the recommended path for any MCP server distributed by an author the recipient doesn't have explicit trust in — install friction stays low, secrets stay in the OS keychain rather than a plaintext config file.

This bundle is **lightweight** (~4 KB) — it does NOT bundle the Python source or the ~3 GB ML deps (torch / transformers / chromadb). Instead, the manifest takes a single required path:

- **`python_path`** — the user's Python interpreter that already ran `pip install -e .` against this repo (pre-filled with `${HOME}/miniconda3/envs/event-intel/python.exe`). Because the install is editable, `event_intel` imports without `PYTHONPATH`, so **no repo path is needed**.

API keys are optional in the form — the server loads them from the repo's `.env` (it derives the repo root from the editable install), so blank form keys fall back to `.env`. This trades self-containment for a much smaller bundle that always tracks the user's live working copy.

## Build

```bash
cd mcpb
mcpb validate manifest.json          # schema check
mcpb pack . event-intel-mcp-0.5.0.mcpb
mcpb info event-intel-mcp-0.5.0.mcpb # confirm size + contents
```

The `.mcpb` output is gitignored (it's a build artifact, version-stamped in the filename).

`mcpb` CLI: `npm install -g @anthropic-ai/mcpb` (Node.js 18+).

## Install

1. Open Claude Desktop → Settings → Extensions
2. Drag `event-intel-mcp-{version}.mcpb` onto the Extensions pane (or click "Install from file")
3. Fill the user_config form (most fields pre-filled or optional):
   - **Python interpreter path** — pre-filled with `${HOME}/miniconda3/envs/event-intel/python.exe`; confirm or adjust. Must be an interpreter that ran `pip install -e .`. (No repo path field — the editable install removes the need for `PYTHONPATH`.)
   - **Brave Search API key** — optional; leave blank to use `BRAVE_API_KEY` from the repo's `.env`.
   - **Use ChatGPT Plus/Pro subscription** — check this to drive the LLM with your ChatGPT subscription (OAuth) instead of an Anthropic API key. If checked, run `event-intel login-chatgpt` once in a terminal after install to authenticate (browser login, one-time).
   - **Anthropic API key** — optional; leave blank to use `ANTHROPIC_API_KEY` from `.env`, or if you checked the ChatGPT box.
   - **Preload the embedding model when Claude Desktop starts** — optional. Check to load bge-m3 (~1.3 GB) in the background at server start so the first tier-list build is fast (zero-touch). Off by default; only runs if the model is already downloaded; adds ~1.3 GB resident memory for the session.
4. Restart Claude Desktop
5. Verify the 8 tools appear in the tool picker: `check_runtime`, `draft_capability_cards`, `validate_capability_cards`, `ingest_product_context`, `build_event_tier_list`, `analyze_event_page`, `probe_exhibitor_endpoint`, `acquire_exhibitor_source`

## Version bump

When releasing a new version:

1. Update `version` in `manifest.json`
2. Update the `tools[]` list if the MCP tool surface changed
3. Rebuild with `mcpb pack . event-intel-mcp-{new_version}.mcpb`
4. Optionally `mcpb sign` if you have a code-signing cert

**Note on versioning:** the bundle `version` here is an **independent track** from the Python
package version in `pyproject.toml` (the package stays at an early `0.1.x` alpha). The bundle version
only signals install-surface changes (manifest fields / form); they are intentionally not synced.
