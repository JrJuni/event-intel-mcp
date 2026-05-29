"""robots.txt gate for the Phase 18T acquisition layer.

is_allowed(url, *, user_agent) -> bool
    Returns True if scraping the URL is permitted by the site's robots.txt.

Status semantics (HTTP status → policy):
    200            → parse the body, apply `can_fetch()`
    404 / 410      → allow (RFC 9309 §2.3: absent robots.txt = allow all)
    401 / 403      → allow (site hides robots.txt; treated as absent policy)
    5xx            → deny (conservative; matches plan v1.2 Robots Contract)
    transport err  → deny (DNS / timeout / conn-refused; conservative)
    other          → deny (any other non-2xx is unexpected)

Per-host cache with 1-hour TTL (in-memory, process-local). A second call for
the same host within 1 hour makes zero network requests.

IMPORTANT: Fetching robots.txt itself bypasses the robots check — otherwise
we'd need to check robots.txt before fetching robots.txt (circular).
Robots.txt is always fetched at the scheme+host level, never at a disallowed path.

UA shared with `raw_fetch.fetch_raw` (see `raw_fetch.get_user_agent()`) so the
policy identity matches the actual fetch identity. Sites that vary robots rules
by UA see the same UA at both fetch points.

Callers raise MCPError(ROBOTS_DISALLOWED, stage=acquisition) when is_allowed
returns False. This module returns a plain bool to keep the check composable.
"""
from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse

from event_intel.acquisition.raw_fetch import get_user_agent


@dataclass
class _CacheEntry:
    rp: urllib.robotparser.RobotFileParser | None
    allowed: bool        # When rp is None: True = allow-all (404/401/403), False = deny-all (5xx/transport)
    expires: float       # time.monotonic() at which entry is stale


_HOST_CACHE: dict[str, _CacheEntry] = {}
_TTL_SECONDS = 3600.0  # 1 hour


def _robots_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/robots.txt"


def _fetch_and_parse(robots_url: str, *, timeout: float = 10.0) -> _CacheEntry:
    """Fetch robots.txt via httpx (uses shared UA) and map status to a cache entry.

    The previous implementation used `urllib.robotparser.RobotFileParser.read()`
    which internally fetches with Python's default User-Agent (`Python-urllib/3.x`).
    Many sites (e.g. Cloudflare-fronted) 403 that UA and trigger robotparser's
    `disallow_all=True` fallback — producing false ROBOTS_DISALLOWED on sites
    that actually permit crawling.
    """
    import httpx

    expires = time.monotonic() + _TTL_SECONDS
    headers = {"User-Agent": get_user_agent()}

    try:
        resp = httpx.get(
            robots_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )
    except (httpx.RequestError, httpx.HTTPError):
        # DNS / timeout / connection refused / TLS error → conservative deny
        return _CacheEntry(rp=None, allowed=False, expires=expires)

    status = resp.status_code

    if status == 200:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        rp.parse(resp.text.splitlines())
        return _CacheEntry(rp=rp, allowed=True, expires=expires)

    if status in (404, 410):
        # RFC 9309: absent robots.txt means allow-all
        return _CacheEntry(rp=None, allowed=True, expires=expires)

    if status in (401, 403):
        # Site hides robots.txt from anonymous fetchers → treat as absent policy
        return _CacheEntry(rp=None, allowed=True, expires=expires)

    if 500 <= status < 600:
        # Server error → conservative deny (plan v1.2 Robots Contract)
        return _CacheEntry(rp=None, allowed=False, expires=expires)

    # Any other non-2xx (unexpected) → conservative deny
    return _CacheEntry(rp=None, allowed=False, expires=expires)


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
    See module docstring for the status → policy mapping.
    """
    parsed = urlparse(url)
    host_key = f"{parsed.scheme}://{parsed.netloc}"
    robots_url = f"{host_key}/robots.txt"

    # Never block the robots.txt fetch itself.
    if url.rstrip("/") == robots_url.rstrip("/"):
        return True

    entry = _get_entry(robots_url, host_key)
    if entry.rp is None:
        # 404/401/403 → allowed=True; 5xx/transport → allowed=False
        return entry.allowed
    return entry.rp.can_fetch(user_agent, url)


def clear_cache() -> None:
    """Remove all cached entries. Useful for tests."""
    _HOST_CACHE.clear()
