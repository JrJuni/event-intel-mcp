"""Phase 18T T0.5 — url_safety: validate_url + host_relation."""
from __future__ import annotations

import pytest

from event_intel.acquisition.url_safety import host_relation, validate_url
from event_intel.errors import ErrorCode, MCPError


# ---------- validate_url — rejection cases ----------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://localhost/exhibitors",
        "http://localhost.local",
        "http://127.0.0.1",
        "http://127.0.0.1:8080/path",
        "http://10.0.0.1",
        "http://10.255.255.255",
        "http://172.16.0.1",
        "http://172.31.255.255",
        "http://192.168.0.1",
        "http://192.168.255.255",
        "http://169.254.1.1",      # link-local
        "http://0.0.0.0",
        "http://224.0.0.1",        # multicast
        "http://::1",              # IPv6 loopback
        "ftp://example.com",       # non-http scheme
        "file:///etc/passwd",
        "http://user:pass@example.com",   # userinfo
        "http://bareword",         # no dot in hostname
        "http://event.local",      # .local
        "http://event.internal",   # .internal
        "http://event.localhost",  # .localhost
        "",
    ],
)
def test_validate_url_rejects_unsafe(url):
    with pytest.raises(MCPError) as ei:
        validate_url(url)
    assert ei.value.error_code == ErrorCode.INVALID_INPUT
    assert ei.value.stage.value == "acquisition"


# ---------- validate_url — acceptance cases ----------


@pytest.mark.parametrize(
    "url",
    [
        "https://smarttechkorea.com/aibigdatashow",
        "https://biz.smarttechkorea.com/biz/get_panel_com.asp",
        "http://www.example.com/exhibitors",
        "https://api.event.co.kr/list",
        "https://event.com:8443/path?q=1",
    ],
)
def test_validate_url_accepts_safe(url):
    result = validate_url(url)
    assert result == url


def test_validate_url_returns_url_unchanged():
    url = "https://example.com/exhibitors"
    assert validate_url(url) == url


# ---------- host_relation ----------


def test_host_relation_same_host():
    assert host_relation("example.com", "example.com") == "same"


def test_host_relation_www_stripped_same():
    assert host_relation("www.example.com", "example.com") == "same"
    assert host_relation("example.com", "www.example.com") == "same"
    assert host_relation("www.example.com", "www.example.com") == "same"


def test_host_relation_subdomain():
    assert host_relation("example.com", "api.example.com") == "subdomain"
    assert host_relation("www.example.com", "api.example.com") == "subdomain"
    assert host_relation("event.co.kr", "api.event.co.kr") == "subdomain"


def test_host_relation_cross():
    assert host_relation("example.com", "evil.com") == "cross"
    # co.kr is NOT treated as a PSL root — event.co.kr does not allow something.co.kr.
    assert host_relation("event.co.kr", "something.co.kr") == "cross"


def test_host_relation_deep_subdomain():
    # Multiple levels of subdomain are still "subdomain" as long as they end with .{landing}.
    assert host_relation("example.com", "a.b.example.com") == "subdomain"
