"""Remote tool surface policy tests — Y2.2d-1 (default-deny allowlist).

Verifies the allowlist/excluded sets cover exactly the registered 13-tool
surface (so a new tool forces classification), the apply function strictly
withholds non-allowlisted tools, and main() applies the policy only on the
streamable-http surface — never on stdio.
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

from event_intel.runtime import tool_policy as P

# ---------- allowlist completeness (guards future tool additions) ----------


def test_allowed_and_excluded_are_disjoint():
    assert not (P.REMOTE_ALLOWED & P.REMOTE_EXCLUDED)


def test_allowlist_plus_excluded_covers_registered_surface():
    """REMOTE_ALLOWED ∪ REMOTE_EXCLUDED must equal the live registered tool set.

    A newly added tool fails this until it is deliberately classified — no
    silent expose (via allowlist) or silent drop (unclassified → withheld).
    """
    server = importlib.import_module("event_intel.mcp_server")
    registered = {t.name for t in server.app._tool_manager.list_tools()}
    assert P.REMOTE_ALLOWED | P.REMOTE_EXCLUDED == registered


def test_setup_tools_are_excluded():
    assert {"prepare_models", "login_chatgpt"} <= P.REMOTE_EXCLUDED


# ---------- apply_remote_tool_policy ----------


class _FakeTM:
    def __init__(self, names):
        self._names = list(names)

    def list_tools(self):
        return [SimpleNamespace(name=n) for n in self._names]

    def remove_tool(self, name):
        self._names.remove(name)


class _FakeApp:
    def __init__(self, names):
        self._tool_manager = _FakeTM(names)


def test_apply_removes_excluded_keeps_allowed():
    names = sorted(P.REMOTE_ALLOWED | P.REMOTE_EXCLUDED)
    app = _FakeApp(names)
    removed = P.apply_remote_tool_policy(app)
    assert removed == sorted(P.REMOTE_EXCLUDED)
    remaining = {t.name for t in app._tool_manager.list_tools()}
    assert remaining == set(P.REMOTE_ALLOWED)


def test_unclassified_tool_is_withheld_by_default():
    app = _FakeApp([*P.REMOTE_ALLOWED, "brand_new_tool"])
    removed = P.apply_remote_tool_policy(app)
    assert "brand_new_tool" in removed
    remaining = {t.name for t in app._tool_manager.list_tools()}
    assert "brand_new_tool" not in remaining


def test_apply_on_allowed_only_removes_nothing():
    app = _FakeApp(sorted(P.REMOTE_ALLOWED))
    assert P.apply_remote_tool_policy(app) == []


# ---------- main() applies policy only on http surface ----------


def _patch_server(monkeypatch):
    server = importlib.import_module("event_intel.mcp_server")
    monkeypatch.setattr(server, "_preimport_heavy_deps", lambda: None)
    monkeypatch.setattr("event_intel.runtime.warmup.maybe_warm_on_start", lambda: None)
    monkeypatch.setattr(server.app, "run", lambda *a, **k: None)
    calls: dict = {}

    def _record(app):
        calls["applied"] = app
        return []  # withheld names; [] so main()'s join/log path stays exercised

    monkeypatch.setattr(
        "event_intel.runtime.tool_policy.apply_remote_tool_policy", _record
    )
    return server, calls


def test_stdio_does_not_apply_remote_policy(monkeypatch):
    monkeypatch.delenv("EVENT_INTEL_TRANSPORT", raising=False)
    server, calls = _patch_server(monkeypatch)
    server.main()
    assert "applied" not in calls


def test_http_applies_remote_policy(monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_TRANSPORT", "streamable-http")
    server, calls = _patch_server(monkeypatch)
    server.main()
    assert calls.get("applied") is server.app
