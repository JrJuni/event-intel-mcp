"""P1.0 — runtime.async_job.BackgroundJob: non-blocking start, idempotency,
inline block, failure capture, reset."""
from __future__ import annotations

import threading
import time

from event_intel.runtime.async_job import BackgroundJob


def _wait_phase(job, phase, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.status()["phase"] == phase:
            return True
        time.sleep(0.01)
    return False


def test_initial_phase_not_started():
    job = BackgroundJob("t")
    assert job.status() == {"phase": "not_started"}


def test_block_runs_inline_and_records_detail():
    job = BackgroundJob("t")
    out = job.start(lambda: {"ok": True, "n": 1}, block=True)
    assert out["phase"] == "done"
    assert job.status()["detail"] == {"ok": True, "n": 1}


def test_nonblocking_runs_in_background_then_done():
    job = BackgroundJob("t")
    gate = threading.Event()
    calls = []

    def fn():
        calls.append(1)
        gate.wait(2)
        return {"ok": True}

    job.start(fn, block=False)
    assert job.status()["phase"] == "running"
    assert "elapsed_seconds" in job.status()

    # a second start while running is an idempotent no-op (fn not invoked twice)
    job.start(fn, block=False)
    assert len(calls) == 1

    gate.set()
    assert _wait_phase(job, "done")
    assert job.status()["detail"] == {"ok": True}
    assert len(calls) == 1


def test_failure_is_captured_not_raised():
    job = BackgroundJob("t")

    def boom():
        raise RuntimeError("nope")

    out = job.start(boom, block=True)
    assert out["phase"] == "failed"
    assert "RuntimeError: nope" in job.status()["error"]


def test_failed_job_can_restart():
    job = BackgroundJob("t")
    job.start(lambda: (_ for _ in ()).throw(ValueError("x")), block=True)
    assert job.status()["phase"] == "failed"
    out = job.start(lambda: {"ok": True}, block=True)  # failed → restart allowed
    assert out["phase"] == "done"


def test_done_start_is_noop():
    job = BackgroundJob("t")
    calls = []
    job.start(lambda: calls.append(1) or {"ok": True}, block=True)
    assert job.status()["phase"] == "done"
    job.start(lambda: calls.append(1) or {"ok": True}, block=True)  # no-op
    assert len(calls) == 1


def test_reset_clears_state():
    job = BackgroundJob("t")
    job.start(lambda: {"ok": True}, block=True)
    assert job.status()["phase"] == "done"
    job.reset()
    assert job.status() == {"phase": "not_started"}
