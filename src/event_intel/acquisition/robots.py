"""robots.txt gate for the Phase 18T acquisition layer.

is_allowed(url, *, user_agent) -> bool
    Returns True if scraping the URL is permitted by the site's robots.txt.
    Returns False if disallowed, or if robots.txt returns 5xx (conservative deny).
    Returns True if robots.txt returns 404 (per RFC 9309 §2.3: absent = allow all).

Per-host cache with 1-hour TTL (in-memory, process-local). A second call for
the same host within 1 hour makes zero network requests.

IMPORTANT: Fetching robots.txt itself bypasses the robots check — otherwise
we'd need to check robots.txt before fetching robots.txt (circular).
Robots.txt is always fetched at the scheme+host level, never at a disallowed path.

Callers raise MCPError(ROBOTS_DISALLOWED, stage=acquisition) when is_allowed
returns False. This module returns a plain bool to keep the check composable.
"""
from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class _CacheEntry:
    rp: urllib.robotparser.RobotFileParser
    allowed: bool        # False means "deny all" (e.g. 5xx on robots.txt)
    expires: float       # time.monotonic() at which entry is stale


_HOST_CACHE: dict[str, _CacheEntry] = {}
_TTL_SECONDS = 3600.0  # 1 hour


def _robots_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/robots.txt"


def _fetch_and_parse(robots_url: str, *, timeout: float = 10.0) -> _CacheEntry:
    """Fetch robots.txt and return a cache entry. Always allows robots.txt itself."""
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        # urllib.robotparser.read() fetches + parses in one call.
        # We wrap in a try/except to handle 5xx as "deny conservatively"
        # and 404 as "allow" (urllib.robotparser treats missing robots.txt
        # as allow-all internally).
        rp.read()
        allowed = True  # parser succeeded → use rp.can_fetch()
    except Exception:
        # 5xx, network error, timeout → conservative deny.
        allowed = False
        rp = None  # type: ignore[assignment]

    return _CacheEntry(rp=rp, allowed=allowed, expires=time.monotonic() + _TTL_SECONDS)


def _get_entry(robots_url: str, host_key: str) -> _CacheEntry:
    now = time.monotonic()
    entry = _HOST_CACHE.get(host_key)
    if entry is None or entry.expires <= now:
        entry = _fetch_and_parse(robots_url)
        _HOST_CACHE[host_key] = entry
    return entry


def is_allowed(url: str, *, user_agent: str = "event-intel-mcp") -> bool:
    """Return True if robots.txt permits fetching `url` with `user_agent`.

    Always returns True for the robots.txt path itself (no circular check).
    Returns True when robots.txt is absent (404).
    Returns False when robots.txt is unreachable due to 5xx.
    """
    parsed = urlparse(url)
    host_key = f"{parsed.scheme}://{parsed.netloc}"
    robots_url = f"{host_key}/robots.txt"

    # Never block the robots.txt fetch itself.
    if url.rstrip("/") == robots_url.rstrip("/"):
        return True

    entry = _get_entry(robots_url, host_key)
    if not entry.allowed or entry.rp is None:
        return False  # 5xx → conservative deny
    return entry.rp.can_fetch(user_agent, url)


def clear_cache() -> None:
    """Remove all cached entries. Useful for tests."""
    _HOST_CACHE.clear()
