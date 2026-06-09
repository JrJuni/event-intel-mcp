"""Remote tool surface policy (Y2.2d-1).

Default-deny: only an explicit allowlist of tools is exposed on the remote
(streamable-http) surface. Setup / host-bound tools are withheld — they act on
the server host, not the calling remote user, and the ChatGPT OAuth lane is
already disabled in remote deploy (Y2.2b).

Default-deny is strict: any registered tool NOT in REMOTE_ALLOWED is withheld,
including a newly added tool that hasn't been classified yet. test_tool_policy
asserts REMOTE_ALLOWED + REMOTE_EXCLUDED together cover exactly the registered
surface, so adding a tool forces a deliberate classification rather than a
silent expose-or-drop.

stdlib-only at import (cold-start safe).
"""
from __future__ import annotations

from typing import Any

# Per-user BD work — safe to expose remotely (path-free capable since Y2.1).
REMOTE_ALLOWED: frozenset[str] = frozenset(
    {
        "check_runtime",
        "draft_capability_cards",
        "validate_capability_cards",
        "ingest_product_context",
        "build_event_tier_list",
        "analyze_event_page",
        "probe_exhibitor_endpoint",
        "acquire_exhibitor_source",
        "draft_labels",
        "sync_product_sources",
        "get_job",
    }
)

# Setup / host-bound — act on the server host, not the remote caller.
REMOTE_EXCLUDED: frozenset[str] = frozenset(
    {
        "prepare_models",  # downloads ~1.3GB to the server host (admin/provisioning)
        "login_chatgpt",  # opens a browser ON the server host; OAuth off in remote (Y2.2b)
    }
)


def apply_remote_tool_policy(app: Any) -> list[str]:
    """Remove non-allowlisted tools from a FastMCP app. Returns removed names (sorted).

    Strict allowlist: any registered tool not in REMOTE_ALLOWED is withheld.
    """
    registered = [t.name for t in app._tool_manager.list_tools()]
    removed = sorted(n for n in registered if n not in REMOTE_ALLOWED)
    for name in removed:
        app._tool_manager.remove_tool(name)
    return removed
