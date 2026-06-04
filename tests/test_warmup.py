"""runtime/warmup.py — async background warm-up + status polling.

No real model: a trivial warm_fn stands in. Locks the state machine
(not_started → warming → ready/failed), idempotent start, block vs async, and
that a failed attempt can be retried. See docs/lesson-learned.md 2026-06-04.
"""
from __future__ import annotations

import threading
import time

from event_intel.runtime import warmup


def setup_function():
    warmup.reset()


def teardown_function():
    warmup.reset()


def _poll_until(status_value: str, timeout: float = 3.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = warmup.status()["status"]
        if s == status_value:
            return s
        time.sleep(0.02)
    return warmup.status()["status"]


def test_initial_status_not_started():
    assert warmup.status()["status"] == "not_started"


def test_block_start_runs_inline_and_reaches_ready():
    calls = {"n": 0}

    def warm_fn():
        calls["n"] += 1
        return {"load_seconds": 0.01, "already_cached": False}

    res = warmup.start(warm_fn, block=True)
    assert res["status"] == "ready"
    assert res["load_seconds"] == 0.01
    assert calls["n"] == 1

    # Already ready → second start is a no-op.
    warmup.start(warm_fn, block=True)
    assert calls["n"] == 1


def test_async_start_returns_warming_then_ready():
    gate = threading.Event()

    def warm_fn():
        gate.wait(2)
        return {"load_seconds": 0.5}

    res = warmup.start(warm_fn, block=False)
    assert res["status"] == "warming"
    assert "elapsed_seconds" in res

    gate.set()
    assert _poll_until("ready") == "ready"
    assert warmup.status()["load_seconds"] == 0.5


def test_start_while_warming_is_noop():
    gate = threading.Event()
    calls = {"n": 0}

    def warm_fn():
        calls["n"] += 1
        gate.wait(2)
        return {"load_seconds": 0.1}

    warmup.start(warm_fn, block=False)
    warmup.start(warm_fn, block=False)  # second call while warming
    gate.set()
    _poll_until("ready")
    assert calls["n"] == 1


def test_failed_status_then_retry_allowed():
    def boom():
        raise RuntimeError("load exploded")

    warmup.start(boom, block=True)
    s = warmup.status()
    assert s["status"] == "failed"
    assert "load exploded" in s["error"]

    # A failed attempt is not sticky — the next warm_up request retries.
    def ok():
        return {"load_seconds": 0.0}

    res = warmup.start(ok, block=True)
    assert res["status"] == "ready"
