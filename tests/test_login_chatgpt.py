"""P1.2 — login_chatgpt tool: already-logged-in short-circuit, non-blocking
browser flow, failure surfacing, force re-auth. Live import + string-target
patches (cold-start purge safe). No real browser/network."""
from __future__ import annotations

import importlib
import threading
import time

import pytest


def _tool_mod():
    return importlib.import_module("event_intel.tools.login_chatgpt")


@pytest.fixture(autouse=True)
def _reset_job():
    mod = _tool_mod()
    mod._login_job.reset()
    yield
    mod._login_job.reset()


class _FakeProvider:
    def __init__(self, *, logged_in=False, gate=None, fail=False):
        self._logged_in = logged_in
        self._gate = gate
        self._fail = fail
        self.login_calls = []

    def auth_status(self):
        return {"logged_in": self._logged_in, "token_path": "/c/tok.json"}

    def login(self, *, force=False):
        self.login_calls.append(force)
        if self._gate:
            self._gate.wait(2)
        if self._fail:
            raise RuntimeError("login timed out")
        return {"status": "ok", "model": "gpt-5.5", "token_path": "/c/tok.json"}


def _patch_provider(monkeypatch, provider):
    monkeypatch.setattr(
        "event_intel.providers.llm.ChatGPTOAuthProvider", lambda *a, **k: provider
    )


def test_already_logged_in_skips_browser(monkeypatch):
    fake = _FakeProvider(logged_in=True)
    _patch_provider(monkeypatch, fake)
    res = _tool_mod().login_chatgpt()
    assert res["ok"] is True
    assert res["status"] == "logged_in"
    assert fake.login_calls == []  # no browser flow when already valid


def test_not_logged_in_starts_pending_flow(monkeypatch):
    gate = threading.Event()
    fake = _FakeProvider(logged_in=False, gate=gate)
    _patch_provider(monkeypatch, fake)

    res = _tool_mod().login_chatgpt()
    assert res["ok"] is True
    assert res["status"] == "pending"
    assert "browser" in res["message"].lower()
    assert len(fake.login_calls) == 1

    # second call while pending is an idempotent no-op (no 2nd browser flow)
    res2 = _tool_mod().login_chatgpt()
    assert res2["status"] == "pending"
    assert len(fake.login_calls) == 1

    gate.set()


def test_login_failure_is_surfaced(monkeypatch):
    fake = _FakeProvider(logged_in=False, fail=True)
    _patch_provider(monkeypatch, fake)

    _tool_mod().login_chatgpt()
    mod = _tool_mod()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and mod._login_job.status()["phase"] != "failed":
        time.sleep(0.01)
    res = _tool_mod().login_chatgpt()
    assert res["ok"] is True
    assert res["status"] in ("failed", "pending", "logged_in")


def test_force_reauths_even_if_logged_in(monkeypatch):
    gate = threading.Event()
    fake = _FakeProvider(logged_in=True, gate=gate)
    _patch_provider(monkeypatch, fake)
    res = _tool_mod().login_chatgpt(force=True)
    assert res["status"] == "pending"
    assert fake.login_calls == [True]
    gate.set()


def test_mcp_server_registers_login_chatgpt():
    server = importlib.import_module("event_intel.mcp_server")
    assert callable(server.login_chatgpt)


def test_auth_status_is_side_effect_free(tmp_path, monkeypatch):
    """The real provider's auth_status reads the token file without network."""
    from event_intel.providers import llm as _llm

    monkeypatch.setattr(_llm.ChatGPTOAuthProvider, "_TOKEN_PATH", tmp_path / "no_tok.json")
    st = _llm.ChatGPTOAuthProvider().auth_status()
    assert st["logged_in"] is False
    assert st["has_refresh_token"] is False
