"""Generic non-blocking single-task job manager (#14 in-app setup parity).

Generalizes the proven warm-up pattern (``runtime/warmup.py``): a heavy one-shot
task (bge-m3 download, ChatGPT OAuth) must NOT run synchronously inside an MCP
tool call — the ~1.3 GB download / browser round-trip would blow Claude Desktop's
request timeout. Instead a tool *starts* the task in the background and returns at
once; ``check_runtime`` (or the tool itself) is the poll surface.

Each ``BackgroundJob`` holds one process-wide state machine guarded by a lock:
``start(fn, block=False)`` kicks off a daemon thread (or runs inline for the
terminal CLI where waiting is fine); ``status()`` is the poll surface. ``start``
while ``running`` / ``done`` is an idempotent no-op that just reports state.

This is pure mechanism — it returns ``{phase, elapsed_seconds, detail, error}``
and leaves the user-facing message to the caller (download vs login differ).
``warmup.py`` is deliberately NOT refactored onto this (it is on the stdio-
critical path); DRY-merging the two is a follow-up.

Stdlib-only (threading + time) → cold-import safe.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

# Phases: not_started → running → (done | failed). A failed job can be restarted.
_PHASES = ("not_started", "running", "done", "failed")


class BackgroundJob:
    """One process-wide async task with a poll-able status."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict = {
            "phase": "not_started",
            "started_at": None,  # time.monotonic() when running began
            "finished_at": None,
            "detail": None,  # fn() return on success
            "error": None,  # "Type: msg" on failure
        }

    def reset(self) -> None:
        """Clear state back to not_started. Used by tests and by an explicit
        force-restart (e.g. re-login), which resets then starts.
        """
        with self._lock:
            self._thread = None
            self._state.update(
                phase="not_started",
                started_at=None,
                finished_at=None,
                detail=None,
                error=None,
            )

    def status(self) -> dict:
        with self._lock:
            snapshot = dict(self._state)
        phase = snapshot["phase"]
        out: dict = {"phase": phase}
        if phase == "running" and snapshot["started_at"] is not None:
            out["elapsed_seconds"] = round(time.monotonic() - snapshot["started_at"], 1)
        if snapshot["detail"] is not None:
            out["detail"] = snapshot["detail"]
        if snapshot["error"] is not None:
            out["error"] = snapshot["error"]
        return out

    def start(self, fn: Callable[[], dict | None], *, block: bool = False) -> dict:
        """Begin the task (idempotent).

        ``fn`` is a zero-arg callable performing the work and optionally returning
        a small detail dict. ``block=True`` runs inline and returns only when done
        (terminal CLI); ``block=False`` spawns a daemon thread and returns at once
        (MCP server — the tool must not block on the load).

        A call while ``running`` or ``done`` is a no-op that reports current state;
        only ``not_started`` / ``failed`` (re)start.
        """
        with self._lock:
            if self._state["phase"] in ("running", "done"):
                return self._public_locked()
            self._state.update(
                phase="running",
                started_at=time.monotonic(),
                finished_at=None,
                detail=None,
                error=None,
            )

        if block:
            self._run(fn)
            return self.status()

        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self._thread.start()
        return self.status()

    # --- internal -------------------------------------------------------------
    def _public_locked(self) -> dict:
        # caller already holds the lock
        snapshot = dict(self._state)
        phase = snapshot["phase"]
        out: dict = {"phase": phase}
        if phase == "running" and snapshot["started_at"] is not None:
            out["elapsed_seconds"] = round(time.monotonic() - snapshot["started_at"], 1)
        if snapshot["detail"] is not None:
            out["detail"] = snapshot["detail"]
        if snapshot["error"] is not None:
            out["error"] = snapshot["error"]
        return out

    def _run(self, fn: Callable[[], dict | None]) -> None:
        try:
            detail = fn()
            with self._lock:
                self._state.update(
                    phase="done", finished_at=time.monotonic(), detail=detail
                )
        except Exception as exc:  # noqa: BLE001 — record any failure, never crash the server
            with self._lock:
                self._state.update(
                    phase="failed",
                    finished_at=time.monotonic(),
                    error=f"{type(exc).__name__}: {exc}",
                )
