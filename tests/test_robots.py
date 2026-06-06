"""Phase 18T T0.5 + plan v3 follow-up — robots.py gate tests.

Two layers of tests:
1. `is_allowed()` end-to-end: monkeypatches `_fetch_and_parse` to test allow/deny
   logic + caching + circular-bypass for robots.txt itself.
2. `_fetch_and_parse()` direct unit tests: monkeypatches `httpx.get` to verify
   the status-code → cache-entry mapping (200 / 404 / 401 / 403 / 5xx / transport
   error) AND the User-Agent header identity (shared with raw_fetch).
"""
from __future__ import annotations

import time
import urllib.robotparser
from unittest.mock import MagicMock, patch

import httpx
import pytest

from event_intel.acquisition import robots as _robots_mod
from event_intel.acquisition.raw_fetch import get_user_agent
from event_intel.acquisition.robots import _CacheEntry, clear_cache, is_allowed


@pytest.fixture(autouse=True)
def clear_robots_cache():
    """Wipe the per-host in-memory cache between tests."""
    clear_cache()
    yield
    clear_cache()


def _make_rp(*, disallow: str | None = None) -> urllib.robotparser.RobotFileParser:
    """Build a RobotFileParser with a canned allow/disallow rule."""
    rp = urllib.robotparser.RobotFileParser()
    lines = ["User-agent: *"]
    if disallow:
        lines.append(f"Disallow: {disallow}")
    else:
        lines.append("Disallow:")  # allow all
    rp.parse(lines)
    return rp


def _patch_fetch(rp: urllib.robotparser.RobotFileParser | None, allowed: bool = True):
    """Monkeypatch _fetch_and_parse to return a canned cache entry."""
    entry = _CacheEntry(rp=rp, allowed=allowed, expires=time.monotonic() + 3600)
    return patch.object(_robots_mod, "_fetch_and_parse", return_value=entry)


# ---------- is_allowed() — end-to-end logic ----------


def test_is_allowed_when_robots_allows_all():
    with _patch_fetch(_make_rp(disallow=None)):
        assert is_allowed("https://example.com/exhibitors") is True


def test_is_allowed_returns_false_when_disallowed():
    with _patch_fetch(_make_rp(disallow="/private/")):
        assert is_allowed("https://example.com/private/data") is False


def test_is_allowed_returns_true_for_non_disallowed_path():
    with _patch_fetch(_make_rp(disallow="/private/")):
        assert is_allowed("https://example.com/exhibitors") is True


def test_is_allowed_returns_true_when_robots_txt_404_allow_all_entry():
    # 404 → rp=None, allowed=True → is_allowed returns True
    with _patch_fetch(rp=None, allowed=True):
        assert is_allowed("https://example.com/exhibitors") is True


def test_is_allowed_returns_false_when_robots_txt_5xx():
    # 5xx / network error → conservative deny (allowed=False in cache entry).
    with _patch_fetch(rp=None, allowed=False):
        assert is_allowed("https://example.com/exhibitors") is False


def test_cached_entry_prevents_second_network_call():
    """After the first call, the in-memory cache must satisfy subsequent calls
    with zero additional invocations of _fetch_and_parse."""
    with _patch_fetch(_make_rp()) as mock_fetch:
        is_allowed("https://example.com/exhibitors")
        is_allowed("https://example.com/exhibitors2")
        # Both calls hit the same host → same cache entry → only 1 fetch.
        assert mock_fetch.call_count == 1


def test_robots_txt_url_itself_is_always_allowed():
    """Fetching robots.txt must never trigger a robots check — no chicken-and-egg."""
    with _patch_fetch(rp=None, allowed=False) as mock_fetch:
        result = is_allowed("https://example.com/robots.txt")
    assert result is True
    # _fetch_and_parse was NOT called (bypassed for robots.txt URL itself).
    assert mock_fetch.call_count == 0


# ---------- _fetch_and_parse() — direct status mapping unit tests ----------
#
# These tests patch `httpx.get` so we exercise the actual mapping inside
# `_fetch_and_parse` rather than mocking the whole helper away. Previous
# tests bypassed this layer entirely, leaving the status→policy table
# unchecked. (plan v3 R4 fix.)


def _mock_httpx_response(status_code: int, body: str = "") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = body
    return resp


def test_fetch_and_parse_200_parses_body_and_marks_allowed():
    body = "User-agent: *\nDisallow: /private\n"
    with patch("httpx.get", return_value=_mock_httpx_response(200, body)):
        entry = _robots_mod._fetch_and_parse("https://example.com/robots.txt")
    assert entry.allowed is True
    assert entry.rp is not None
    assert entry.rp.can_fetch("event-intel-mcp", "https://example.com/exhibitors") is True
    assert entry.rp.can_fetch("event-intel-mcp", "https://example.com/private") is False


def test_fetch_and_parse_404_returns_allow_all_without_rp():
    with patch("httpx.get", return_value=_mock_httpx_response(404)):
        entry = _robots_mod._fetch_and_parse("https://example.com/robots.txt")
    assert entry.rp is None
    assert entry.allowed is True  # RFC 9309: absent = allow


def test_fetch_and_parse_410_returns_allow_all_without_rp():
    with patch("httpx.get", return_value=_mock_httpx_response(410)):
        entry = _robots_mod._fetch_and_parse("https://example.com/robots.txt")
    assert entry.rp is None
    assert entry.allowed is True


def test_fetch_and_parse_403_returns_allow_all_without_rp():
    """smarttechkorea.com case — site 403s anonymous robots.txt fetchers.
    plan v3 R5: treat as 'site hides policy' = allow rather than deny."""
    with patch("httpx.get", return_value=_mock_httpx_response(403)):
        entry = _robots_mod._fetch_and_parse("https://example.com/robots.txt")
    assert entry.rp is None
    assert entry.allowed is True


def test_fetch_and_parse_401_returns_allow_all_without_rp():
    with patch("httpx.get", return_value=_mock_httpx_response(401)):
        entry = _robots_mod._fetch_and_parse("https://example.com/robots.txt")
    assert entry.rp is None
    assert entry.allowed is True


def test_fetch_and_parse_500_returns_deny():
    with patch("httpx.get", return_value=_mock_httpx_response(500)):
        entry = _robots_mod._fetch_and_parse("https://example.com/robots.txt")
    assert entry.rp is None
    assert entry.allowed is False


def test_fetch_and_parse_503_returns_deny():
    with patch("httpx.get", return_value=_mock_httpx_response(503)):
        entry = _robots_mod._fetch_and_parse("https://example.com/robots.txt")
    assert entry.rp is None
    assert entry.allowed is False


def test_fetch_and_parse_timeout_returns_deny():
    with patch("httpx.get", side_effect=httpx.TimeoutException("timed out")):
        entry = _robots_mod._fetch_and_parse("https://example.com/robots.txt")
    assert entry.rp is None
    assert entry.allowed is False


def test_fetch_and_parse_connect_error_returns_deny():
    with patch("httpx.get", side_effect=httpx.ConnectError("connection refused")):
        entry = _robots_mod._fetch_and_parse("https://example.com/robots.txt")
    assert entry.rp is None
    assert entry.allowed is False


def test_fetch_and_parse_uses_shared_user_agent():
    """plan v3 R5: robots.txt fetch must use the same UA as raw_fetch so the
    policy identity matches the actual page-fetch identity."""
    expected_ua = get_user_agent()
    captured_headers: dict[str, str] = {}

    def _capture(*args, **kwargs):
        captured_headers.update(kwargs.get("headers") or {})
        return _mock_httpx_response(200, "User-agent: *\nAllow: /\n")

    with patch("httpx.get", side_effect=_capture):
        _robots_mod._fetch_and_parse("https://example.com/robots.txt")

    assert captured_headers.get("User-Agent") == expected_ua
