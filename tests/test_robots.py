"""Phase 18T T0.5 — robots.py gate tests.

Uses monkeypatching to avoid real HTTP calls — the goal is to test the
allow/deny logic and caching, not robots.txt parsing itself (that's stdlib).
"""
from __future__ import annotations

import urllib.robotparser
from unittest.mock import MagicMock, patch

import pytest

from event_intel.acquisition import robots as _robots_mod
from event_intel.acquisition.robots import clear_cache, is_allowed


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
    from event_intel.acquisition.robots import _CacheEntry
    import time

    entry = _CacheEntry(rp=rp, allowed=allowed, expires=time.monotonic() + 3600)

    return patch.object(_robots_mod, "_fetch_and_parse", return_value=entry)


def test_is_allowed_when_robots_allows_all():
    with _patch_fetch(_make_rp(disallow=None)):
        assert is_allowed("https://example.com/exhibitors") is True


def test_is_allowed_returns_false_when_disallowed():
    with _patch_fetch(_make_rp(disallow="/private/")):
        # /private/ is disallowed
        assert is_allowed("https://example.com/private/data") is False


def test_is_allowed_returns_true_for_non_disallowed_path():
    with _patch_fetch(_make_rp(disallow="/private/")):
        assert is_allowed("https://example.com/exhibitors") is True


def test_is_allowed_returns_true_when_robots_txt_404():
    # 404 → rp parsed with empty body → allow all.
    with _patch_fetch(_make_rp(disallow=None), allowed=True):
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
        # Even if 5xx → deny, the robots.txt URL itself should be allowed.
        result = is_allowed("https://example.com/robots.txt")
    assert result is True
    # _fetch_and_parse was NOT called (bypassed for robots.txt URL itself).
    assert mock_fetch.call_count == 0
