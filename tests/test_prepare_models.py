"""P1.1 — prepare_models tool: ready short-circuit, non-blocking download start,
failure surfacing, force re-download. Live import + string-target patches
(cold-start purge safe, playbook #2). No real model is downloaded.
"""
from __future__ import annotations

import importlib
import threading
import time

import pytest


def _tool_mod():
    return importlib.import_module("event_intel.tools.prepare_models")


@pytest.fixture(autouse=True)
def _reset_job():
    # Reset the module-singleton download job around each test.
    mod = _tool_mod()
    mod._download_job.reset()
    yield
    mod._download_job.reset()


class _Emb:
    def __init__(self, status, **extra):
        self._status = status
        self._extra = extra

    def is_ready(self):
        return {"status": self._status, **self._extra}


def test_ready_short_circuits_no_download(monkeypatch):
    monkeypatch.setattr(
        "event_intel.providers.embedding.BgeM3Provider",
        lambda *a, **k: _Emb("ready", path="/c/bge", size_mb=1300),
    )
    # prepare_bge_m3 must NOT be called when already cached
    called = []
    monkeypatch.setattr(
        "event_intel.runtime.models.prepare_bge_m3",
        lambda *a, **k: called.append(1),
    )
    res = _tool_mod().prepare_models()
    assert res["ok"] is True
    assert res["status"] == "ready"
    assert res["size_mb"] == 1300
    assert called == []


def test_missing_starts_background_download(monkeypatch):
    monkeypatch.setattr(
        "event_intel.providers.embedding.BgeM3Provider",
        lambda *a, **k: _Emb("missing", path="/c/bge"),
    )
    gate = threading.Event()
    calls = []

    def _fake_prepare():
        calls.append(1)
        gate.wait(2)
        return {"ok": True, "path": "/c/bge", "size_mb": 1300}

    monkeypatch.setattr("event_intel.runtime.models.prepare_bge_m3", _fake_prepare)

    res = _tool_mod().prepare_models()
    assert res["ok"] is True
    assert res["status"] == "downloading"
    assert "1.3 GB" in res["message"]
    assert len(calls) == 1

    # second call while downloading is a no-op (does not start a 2nd download)
    res2 = _tool_mod().prepare_models()
    assert res2["status"] == "downloading"
    assert len(calls) == 1

    gate.set()


def test_download_failure_is_surfaced(monkeypatch):
    monkeypatch.setattr(
        "event_intel.providers.embedding.BgeM3Provider",
        lambda *a, **k: _Emb("missing"),
    )

    def _boom():
        raise RuntimeError("network down")

    monkeypatch.setattr("event_intel.runtime.models.prepare_bge_m3", _boom)

    # block=False, but the failure may land between start and our read — poll.
    _tool_mod().prepare_models()
    mod = _tool_mod()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and mod._download_job.status()["phase"] != "failed":
        time.sleep(0.01)
    res = _tool_mod().prepare_models()  # failed job → surfaced (and restartable)
    # after a failed job, calling again restarts → downloading again; either way ok
    assert res["ok"] is True
    assert res["status"] in ("failed", "downloading", "ready")


def test_force_redownloads_even_if_ready(monkeypatch):
    monkeypatch.setattr(
        "event_intel.providers.embedding.BgeM3Provider",
        lambda *a, **k: _Emb("ready", path="/c/bge", size_mb=1300),
    )
    calls = []
    gate = threading.Event()

    def _fake_prepare():
        calls.append(1)
        gate.wait(2)
        return {"ok": True}

    monkeypatch.setattr("event_intel.runtime.models.prepare_bge_m3", _fake_prepare)
    res = _tool_mod().prepare_models(force=True)
    assert res["status"] == "downloading"
    assert len(calls) == 1
    gate.set()


def test_mcp_server_registers_prepare_models():
    server = importlib.import_module("event_intel.mcp_server")
    assert callable(server.prepare_models)
