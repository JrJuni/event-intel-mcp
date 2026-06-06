"""Regression guard for the check_runtime stdio timeout (2026-06-04).

Root cause (confirmed by probe): the first `import chromadb` inside a FastMCP
worker thread hangs indefinitely in the stdio server — a cold `check_runtime` got
NO response in 240s. The fix pre-imports chromadb + sentence_transformers on the
MAIN thread in `mcp_server.main()`. This test drives the REAL server over stdio
and asserts a cold `check_runtime` actually responds (and that stdout carries only
JSON-RPC — also guards against stdout pollution).

Why a subprocess (not `collection_info()` standalone): the hang only reproduces in
the FastMCP worker-thread context. A standalone `ChromaProvider().collection_info()`
runs on the main thread and returns in <1s — it would FALSE-NEGATIVE the bug.

Marked `slow` + skipped unless preconditions to reach the chromadb step are met
(bge-m3 cached + BRAVE key resolvable), since preflight stops earlier otherwise.
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.slow


def _preconditions():
    from event_intel._env import load_project_env

    load_project_env()
    import os

    from event_intel.providers.embedding import BgeM3Provider

    if not os.environ.get("BRAVE_API_KEY"):
        return False, "BRAVE_API_KEY not resolvable — preflight stops before the chromadb step"
    if BgeM3Provider().is_ready().get("status") != "ready":
        return False, "bge-m3 not cached — preflight stops at the embedding check"
    return True, ""


def test_cold_check_runtime_responds_over_stdio():
    ok, why = _preconditions()
    if not ok:
        pytest.skip(why)

    proc = subprocess.Popen(
        [sys.executable, "-m", "event_intel.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    out_q: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [out_q.put(ln.rstrip("\n")) for ln in proc.stdout], daemon=True).start()
    bad_lines = []

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def wait_for_id(tid, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = out_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                bad_lines.append(line)  # non-JSON on stdout = protocol corruption
                continue
            if isinstance(msg, dict) and msg.get("id") == tid:
                return msg
        return None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "regression", "version": "0"}}})
        assert wait_for_id(1, 60) is not None, "server did not complete initialize handshake"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # The cold call: warm_up=false reaches the chromadb step (check 5) where the
        # pre-fix hang occurred. 60s is far beyond the ~7s fixed path but well under
        # the >240s hang.
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "check_runtime",
                         "arguments": {"workspace_id": "default", "warm_up": False}}})
        resp = wait_for_id(2, 60)
    finally:
        proc.terminate()

    assert resp is not None, (
        "cold check_runtime did NOT respond within 60s over stdio — the worker-thread "
        "chromadb import hang has regressed (main-thread pre-import missing?)."
    )
    assert not bad_lines, f"non-JSON-RPC lines on server stdout (protocol pollution): {bad_lines[:3]}"
