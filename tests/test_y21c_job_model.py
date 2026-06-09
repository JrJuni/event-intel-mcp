"""Y2.1c — job_store + run_as_job + get_job tool.

Corner-case set (self-generated, adversarial):
  A create→running (opaque id, non-blocking)   B run_as_job done→result+pinned
  C run_as_job fn raises→failed (no crash)       D restart(boot mismatch)→interrupted
  E missing/invalid id→None / workspace isolation
  F result artifacts pinned survive registry.gc  G get_job tool envelope
"""
from __future__ import annotations

import importlib
import threading
import time

import pytest

from event_intel.runtime import job_store as J
from event_intel.storage import artifact_registry as R


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def _wait_status(workspace_id, job_id, status, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        m = J.get_job(workspace_id=workspace_id, job_id=job_id)
        if m and m["status"] == status:
            return m
        time.sleep(0.01)
    return J.get_job(workspace_id=workspace_id, job_id=job_id)


# A
def test_create_job_running_opaque_id():
    m = J.create_job(workspace_id="default", tool="build")
    assert m["status"] == J.RUNNING
    assert J._valid_id(m["job_id"]) and len(m["job_id"]) >= 16
    # readable back
    assert J.get_job(workspace_id="default", job_id=m["job_id"])["tool"] == "build"


# B
def test_run_as_job_completes_with_result_artifacts():
    art = R.put_artifact(workspace_id="default", content="result blob")
    gate = threading.Event()

    def _fn():
        gate.wait(2)
        return [art["artifact_id"]]

    started = J.run_as_job(workspace_id="default", tool="build", fn=_fn)
    assert started["status"] == J.RUNNING
    assert J.get_job(workspace_id="default", job_id=started["job_id"])["status"] == J.RUNNING
    gate.set()
    done = _wait_status("default", started["job_id"], J.DONE)
    assert done["status"] == J.DONE
    assert done["result_artifact_ids"] == [art["artifact_id"]]


# C
def test_run_as_job_failure_captured_not_raised():
    def _boom():
        raise RuntimeError("kaboom")

    started = J.run_as_job(workspace_id="default", tool="ingest", fn=_boom)
    failed = _wait_status("default", started["job_id"], J.FAILED)
    assert failed["status"] == J.FAILED
    assert "RuntimeError: kaboom" in failed["error"]


# D
def test_restart_transitions_running_to_interrupted(monkeypatch):
    m = J.create_job(workspace_id="default", tool="build")  # running, current boot
    # simulate a NEW process: change the module boot id → manifest's is now "prior"
    monkeypatch.setattr(J, "_BOOT_ID", "different-boot-id")
    got = J.get_job(workspace_id="default", job_id=m["job_id"])
    assert got["status"] == J.INTERRUPTED
    assert "restart" in got["error"]
    # persisted (a second read stays interrupted)
    assert J.get_job(workspace_id="default", job_id=m["job_id"])["status"] == J.INTERRUPTED


# E
def test_missing_and_invalid_id_and_workspace_isolation():
    assert J.get_job(workspace_id="default", job_id="Zm9vMTIzNDU2Nzg5MDEy") is None
    assert J.get_job(workspace_id="default", job_id="../../etc") is None
    m = J.create_job(workspace_id="team_a", tool="build")
    assert J.get_job(workspace_id="team_b", job_id=m["job_id"]) is None  # cross-ws blocked
    assert J.get_job(workspace_id="team_a", job_id=m["job_id"]) is not None


# F
def test_result_artifacts_pinned_survive_gc():
    art = R.put_artifact(workspace_id="default", content="keepme", ttl_seconds=10, now=1000.0)
    started = J.run_as_job(workspace_id="default", tool="build", fn=lambda: [art["artifact_id"]])
    _wait_status("default", started["job_id"], J.DONE)
    # well past artifact TTL, but pinned by the completed job → gc skips it
    assert R.gc(workspace_id="default", now=1e12) == 0
    assert R.get_artifact(workspace_id="default", artifact_id=art["artifact_id"], now=1e12) == b"keepme"


# G
def test_get_job_tool_envelope():
    server = importlib.import_module("event_intel.mcp_server")
    # missing job_id
    res = server.get_job(job_id="")
    assert res["ok"] is False and res["error_code"] == "INVALID_INPUT"
    # unknown job_id
    res2 = server.get_job(job_id="Zm9vMTIzNDU2Nzg5MDEy")
    assert res2["ok"] is False and res2["error_code"] == "INVALID_INPUT"
    # real job — create through the SAME live module the server tool uses, so a
    # prior cold-start purge can't leave the test's stale module's _BOOT_ID
    # mismatching the server's fresh one (playbook #2). create+get share one module.
    live_js = importlib.import_module("event_intel.runtime.job_store")
    m = live_js.create_job(workspace_id="default", tool="build")
    res3 = server.get_job(job_id=m["job_id"])
    assert res3["ok"] is True and res3["status"] == live_js.RUNNING and res3["tool"] == "build"
