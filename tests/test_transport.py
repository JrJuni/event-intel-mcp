"""Transport selection tests — Y2.2c (opt-in streamable-http, loopback default).

Covers the env resolver (defaults, alias, validation, allowlist parsing),
apply_to_app settings mutation + transport-security construction, and main()
dispatch (stdio default vs streamable-http) without ever binding a socket.
"""
from __future__ import annotations

import importlib

import pytest

from event_intel.errors import ErrorCode, MCPError
from event_intel.runtime import transport as T

_TRANSPORT_ENVS = (
    "EVENT_INTEL_TRANSPORT",
    "EVENT_INTEL_HTTP_HOST",
    "EVENT_INTEL_HTTP_PORT",
    "EVENT_INTEL_HTTP_ALLOWED_HOSTS",
    "EVENT_INTEL_HTTP_ALLOWED_ORIGINS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _TRANSPORT_ENVS:
        monkeypatch.delenv(name, raising=False)


# ---------- resolve_transport_config: defaults ----------


def test_default_is_stdio_loopback():
    cfg = T.resolve_transport_config()
    assert cfg.transport == "stdio"
    assert cfg.is_http is False
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8000
    assert cfg.allowed_hosts == () and cfg.allowed_origins == ()
    assert cfg.binds_non_loopback is False


def test_empty_or_whitespace_transport_falls_back_to_stdio(monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_TRANSPORT", "   ")
    assert T.resolve_transport_config().transport == "stdio"


# ---------- transport selection + alias ----------


def test_streamable_http_selected(monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_TRANSPORT", "streamable-http")
    cfg = T.resolve_transport_config()
    assert cfg.transport == "streamable-http"
    assert cfg.is_http is True


def test_http_alias_maps_to_streamable_http(monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_TRANSPORT", "http")
    assert T.resolve_transport_config().transport == "streamable-http"


def test_invalid_transport_raises_config_error(monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_TRANSPORT", "websocket")
    with pytest.raises(MCPError) as exc:
        T.resolve_transport_config()
    assert exc.value.error_code == ErrorCode.CONFIG_ERROR
    assert "streamable-http" in exc.value.hint["allowed"]


# ---------- host / loopback detection ----------


def test_non_loopback_host_flagged(monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_TRANSPORT", "streamable-http")
    monkeypatch.setenv("EVENT_INTEL_HTTP_HOST", "0.0.0.0")
    cfg = T.resolve_transport_config()
    assert cfg.host == "0.0.0.0"
    assert cfg.binds_non_loopback is True


def test_localhost_variants_not_flagged(monkeypatch):
    for host in ("127.0.0.1", "::1", "localhost"):
        monkeypatch.setenv("EVENT_INTEL_HTTP_HOST", host)
        assert T.resolve_transport_config().binds_non_loopback is False


# ---------- port validation ----------


def test_valid_port_override(monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_HTTP_PORT", "9123")
    assert T.resolve_transport_config().port == 9123


def test_non_integer_port_raises(monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_HTTP_PORT", "abc")
    with pytest.raises(MCPError) as exc:
        T.resolve_transport_config()
    assert exc.value.error_code == ErrorCode.CONFIG_ERROR


@pytest.mark.parametrize("bad", ["0", "70000", "-1"])
def test_out_of_range_port_raises(monkeypatch, bad):
    monkeypatch.setenv("EVENT_INTEL_HTTP_PORT", bad)
    with pytest.raises(MCPError):
        T.resolve_transport_config()


# ---------- allowlist parsing ----------


def test_allowlists_parsed_stripped_and_blanks_dropped(monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_HTTP_ALLOWED_HOSTS", " a.com , , b.com:8080 ")
    monkeypatch.setenv("EVENT_INTEL_HTTP_ALLOWED_ORIGINS", "https://a.com,")
    cfg = T.resolve_transport_config()
    assert cfg.allowed_hosts == ("a.com", "b.com:8080")
    assert cfg.allowed_origins == ("https://a.com",)


# ---------- apply_to_app ----------


class _FakeSettings:
    def __init__(self):
        self.host = "127.0.0.1"
        self.port = 8000
        self.transport_security = "DEFAULT"  # sentinel: untouched


class _FakeApp:
    def __init__(self):
        self.settings = _FakeSettings()


def test_apply_is_noop_for_stdio():
    app = _FakeApp()
    T.apply_to_app(app, T.TransportConfig(transport="stdio"))
    assert app.settings.host == "127.0.0.1"
    assert app.settings.transport_security == "DEFAULT"


def test_apply_sets_host_port_for_http():
    app = _FakeApp()
    cfg = T.TransportConfig(transport="streamable-http", host="127.0.0.1", port=9001)
    T.apply_to_app(app, cfg)
    assert app.settings.host == "127.0.0.1"
    assert app.settings.port == 9001
    # No explicit allowlists → leave FastMCP's default security untouched.
    assert app.settings.transport_security == "DEFAULT"


def test_apply_builds_transport_security_when_allowlists_given():
    from mcp.server.transport_security import TransportSecuritySettings

    app = _FakeApp()
    cfg = T.TransportConfig(
        transport="streamable-http",
        allowed_hosts=("a.com",),
        allowed_origins=("https://a.com",),
    )
    T.apply_to_app(app, cfg)
    ts = app.settings.transport_security
    assert isinstance(ts, TransportSecuritySettings)
    assert ts.enable_dns_rebinding_protection is True
    assert ts.allowed_hosts == ["a.com"]
    assert ts.allowed_origins == ["https://a.com"]


# ---------- main() dispatch (no socket bound) ----------


def _patch_server(monkeypatch):
    server = importlib.import_module("event_intel.mcp_server")
    monkeypatch.setattr(server, "_preimport_heavy_deps", lambda: None)
    monkeypatch.setattr(
        "event_intel.runtime.warmup.maybe_warm_on_start", lambda: None
    )
    calls: dict = {}
    monkeypatch.setattr(
        server.app, "run", lambda *a, **k: calls.update(args=a, kwargs=k)
    )
    return server, calls


def test_main_defaults_to_stdio_run(monkeypatch):
    server, calls = _patch_server(monkeypatch)
    server.main()
    assert calls == {"args": (), "kwargs": {}}  # no transport kwarg = stdio


def test_main_dispatches_streamable_http(monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_TRANSPORT", "streamable-http")
    server, calls = _patch_server(monkeypatch)
    server.main()
    assert calls["kwargs"] == {"transport": "streamable-http"}
    # settings applied (loopback default — does not pollute global host)
    assert server.app.settings.host == "127.0.0.1"
    assert server.app.settings.port == 8000
