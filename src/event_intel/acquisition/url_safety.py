"""URL safety gate for the Phase 18T acquisition layer.

Every acquisition tool (analyze_event_page, probe_exhibitor_endpoint,
acquire_exhibitor_source) calls validate_url() before the first HTTP byte
goes out. This is an independent per-tool check — not delegated to the
orchestrator.

validate_url(url) -> str
    Returns the normalized URL string on success.
    Raises MCPError(INVALID_INPUT, stage=acquisition) on any violation.
    Rejects: non-http(s) schemes, userinfo, private IPs, loopback, link-local,
    multicast, 0.0.0.0, localhost/*.local/*.internal, bare hostnames (no dot).

host_relation(landing_host, candidate_host) -> "same" | "subdomain" | "cross"
    Stdlib-only. NO public-suffix-list (PSL) dependency.
    Normalizes by stripping a single leading "www." from each host.
    "subdomain": candidate ends with ".{normalized_landing}".
    "same":      normalized hosts are equal.
    "cross":     anything else.
    Intentionally conservative: event.co.kr does NOT allow something.co.kr
    (that would require PSL knowledge we deliberately avoid).
"""
from __future__ import annotations

import ipaddress
from typing import Literal
from urllib.parse import urlparse

from event_intel.errors import ErrorCode, MCPError, Stage


def _mcp_invalid(msg: str, *, hint: dict | None = None) -> MCPError:
    return MCPError(
        error_code=ErrorCode.INVALID_INPUT,
        stage=Stage.ACQUISITION,
        message=msg,
        hint=hint,
        retryable=False,
    )


def validate_url(url: str) -> str:
    """Validate and return the URL; raise MCPError(INVALID_INPUT) on violation.

    Safety checks (all evaluated before any network call):
    1. Scheme must be http or https.
    2. No userinfo (user:pass@host).
    3. Host must be present.
    4. No bare hostname (must contain at least one dot).
    5. Host not localhost / *.localhost / *.local / *.internal.
    6. Host must not resolve to a private / reserved IP range when parsed
       numerically (numeric IPs only — no DNS lookup performed).
    """
    if not url or not isinstance(url, str):
        raise _mcp_invalid("URL is empty or not a string")

    parsed = urlparse(url)

    # 1. Scheme
    if parsed.scheme not in ("http", "https"):
        raise _mcp_invalid(
            f"URL scheme {parsed.scheme!r} is not allowed; use http or https",
            hint={"url": url, "rule": "scheme must be http or https"},
        )

    # 2. Userinfo
    if parsed.username or parsed.password:
        raise _mcp_invalid(
            "URL must not contain credentials (user:pass@host)",
            hint={"url": url, "rule": "no userinfo"},
        )

    # 3. Host present
    host = parsed.hostname or ""
    if not host:
        raise _mcp_invalid("URL has no host", hint={"url": url})

    # 4. Bare hostname (no dot)
    # We allow numeric IPs (checked below) but reject single-label names.
    is_numeric = _is_numeric_host(host)
    if not is_numeric and "." not in host:
        raise _mcp_invalid(
            f"URL host {host!r} has no dot (bare hostname not allowed)",
            hint={"url": url, "rule": "host must be a domain or IP"},
        )

    # 5. Reserved label names
    lower = host.lower()
    if (
        lower == "localhost"
        or lower.endswith(".localhost")
        or lower.endswith(".local")
        or lower.endswith(".internal")
    ):
        raise _mcp_invalid(
            f"URL host {host!r} is a local/internal name",
            hint={"url": url, "rule": "no localhost / .local / .internal"},
        )

    # 6. Private / reserved IP (numeric hosts only — no DNS resolution)
    if is_numeric:
        _reject_if_private_ip(host, url)

    return url


# Private / reserved IPv4 ranges checked numerically.
_PRIVATE_V4_NETWORKS = [
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),   # link-local
    ipaddress.IPv4Network("127.0.0.0/8"),       # loopback
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv4Network("100.64.0.0/10"),     # shared address space
    ipaddress.IPv4Network("192.0.0.0/24"),      # IETF protocol assignments
    ipaddress.IPv4Network("224.0.0.0/4"),       # multicast
    ipaddress.IPv4Network("240.0.0.0/4"),       # reserved
]


def _is_numeric_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _reject_if_private_ip(host: str, url: str) -> None:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_multicast:
            raise _mcp_invalid(
                f"URL host {host!r} is a private/reserved IPv6 address",
                hint={"url": url, "rule": "no private/reserved IPs"},
            )
        return
    for net in _PRIVATE_V4_NETWORKS:
        if addr in net:
            raise _mcp_invalid(
                f"URL host {host!r} is a private/reserved IP ({net})",
                hint={"url": url, "rule": "no private/reserved IPs"},
            )


def host_relation(
    landing_host: str,
    candidate_host: str,
) -> Literal["same", "subdomain", "cross"]:
    """Classify the relationship between a landing page host and a candidate host.

    Normalizes by stripping exactly one leading 'www.' label from each.
    Uses plain string comparison — NO public-suffix-list lookup.

    'same'      — normalized hosts are equal
    'subdomain' — candidate is a strict subdomain of landing
    'cross'     — all other cases

    Examples:
        host_relation("event.com", "api.event.com")        -> "subdomain"
        host_relation("www.event.com", "api.event.com")    -> "subdomain"
        host_relation("event.com", "www.event.com")        -> "same"
        host_relation("event.co.kr", "something.co.kr")   -> "cross"
        host_relation("event.co.kr", "api.event.co.kr")   -> "subdomain"
    """
    def _norm(h: str) -> str:
        h = h.lower()
        if h.startswith("www."):
            h = h[4:]
        return h

    lh = _norm(landing_host)
    ch = _norm(candidate_host)

    if lh == ch:
        return "same"
    if ch.endswith(f".{lh}"):
        return "subdomain"
    return "cross"
