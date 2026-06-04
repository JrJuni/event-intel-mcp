"""Process-global, non-blocking warm-up manager for the embedding model.

Why this exists (see docs/lesson-learned.md 2026-06-04): the bge-m3 load is
~10-20s. Doing it synchronously inside an MCP tool call risks hitting Claude
Desktop's own request timeout — the client gives up while the server is still
loading, surfacing as an opaque failure. The coldcall sibling project hit the
same wall and settled on an async pattern: a tool invocation *starts* the warm-up
in the background and returns immediately with a "warming up, ready in ~N min"
message; the user (or the agent) calls again later to poll readiness.

This module is that manager. It holds one process-wide state machine guarded by a
lock. `start()` kicks off a background load (or runs it inline when `block=True`,
used by the terminal CLI where waiting is fine). `status()` is the poll surface.

Stdlib-only (threading + time) → safe to import at module top without breaking the
MCP cold-start contract.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

# Conservative readiness estimate shown to users. Measured loads are ~10-20s; we
# advertise a wider window so a slow disk / cold cache never reads as "stuck".
_ETA_HINT = "usually under a minute, allow up to ~2 minutes"

_lock = threading.Lock()
_thread: threading.Thread | None = None
_state: dict = {
    "phase": "not_started",  # not_started | warming | ready | failed
    "started_at": None,  # time.monotonic() when warming began
    "finished_at": None,
    "detail": None,  # warm_fn() return on success
    "error": None,  # "Type: msg" on failure
}


def reset() -> None:
    """Clear all state. Tests only — the live process never resets."""
    global _thread
    with _lock:
        _thread = None
        _state.update(
            phase="not_started",
            started_at=None,
            finished_at=None,
            detail=None,
            error=None,
        )


def _public(snapshot: dict) -> dict:
    """Render the internal state into a user-facing status dict with guidance."""
    phase = snapshot["phase"]
    if phase == "ready":
        out = {
            "status": "ready",
            "message": "bge-m3 is loaded and cached for this server process.",
        }
        detail = snapshot.get("detail") or {}
        if isinstance(detail, dict) and "load_seconds" in detail:
            out["load_seconds"] = detail["load_seconds"]
        return out
    if phase == "warming":
        started = snapshot.get("started_at")
        elapsed = round(time.monotonic() - started, 1) if started else None
        return {
            "status": "warming",
            "elapsed_seconds": elapsed,
            "message": (
                "bge-m3 is loading in the background "
                f"({_ETA_HINT}). Call check_runtime again to poll — once "
                "warm_up.status is 'ready', build_event_tier_list will be fast."
            ),
        }
    if phase == "failed":
        return {
            "status": "failed",
            "error": snapshot.get("error"),
            "message": "Warm-up failed; it will retry on the next warm_up request.",
        }
    return {
        "status": "not_started",
        "message": "bge-m3 is not preloaded. Pass warm_up=true to load it in the background.",
    }


def status() -> dict:
    with _lock:
        snapshot = dict(_state)
    return _public(snapshot)


def start(warm_fn: Callable[[], dict], *, block: bool = False) -> dict:
    """Begin warming the model (idempotent).

    Parameters
    ----------
    warm_fn:
        Zero-arg callable that performs the load (e.g. ``BgeM3Provider().warm_up``)
        and returns a small status dict. It is never called twice concurrently.
    block:
        True  → run inline and return only when loaded (terminal CLI; waiting is fine).
        False → spawn a daemon thread and return immediately (MCP server; the tool
                 call must not block on the load or it may hit the client timeout).

    Returns the current ``status()`` dict. A second call while ``warming`` or after
    ``ready`` is a no-op that just reports state.
    """
    global _thread
    with _lock:
        if _state["phase"] in ("warming", "ready"):
            return _public(dict(_state))
        # not_started or failed → (re)start
        _state.update(
            phase="warming",
            started_at=time.monotonic(),
            finished_at=None,
            detail=None,
            error=None,
        )

    if block:
        _run(warm_fn)
        return status()

    _thread = threading.Thread(target=_run, args=(warm_fn,), daemon=True)
    _thread.start()
    return status()


def _run(warm_fn: Callable[[], dict]) -> None:
    """Body of the warm-up. Records ready/failed under the lock."""
    try:
        detail = warm_fn()
        with _lock:
            _state.update(phase="ready", finished_at=time.monotonic(), detail=detail)
    except Exception as exc:  # noqa: BLE001 — record any load failure, never crash the server
        with _lock:
            _state.update(
                phase="failed",
                finished_at=time.monotonic(),
                error=f"{type(exc).__name__}: {exc}",
            )
