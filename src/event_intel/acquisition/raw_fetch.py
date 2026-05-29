"""Raw HTTP fetch helper for the Phase 18T acquisition layer.

fetch_raw(url, *, method, headers, params, max_redirects) -> RawResponse

Contract (from plan v1.2):
  RAISES MCPError(INVALID_INPUT, stage=acquisition) ONLY for safety violations:
    - Initial URL rejected by validate_url
    - A redirect target rejected by validate_url

  RETURNS RawResponse(status=0, network_error=str(exc)) for transport failures:
    - DNS resolution failure
    - Connection refused
    - Timeout

  HTTP-level outcomes (any 1xx-5xx status) are returned as-is. HTTP semantic
  mapping (401→LOGIN_REQUIRED, etc.) is http_status_map.py's responsibility.

No trafilatura. No extraction. Pure raw bytes / decoded body.
httpx is already a top-level dep (pyproject.toml:24).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from event_intel.acquisition.url_safety import host_relation, validate_url
from event_intel.errors import ErrorCode, MCPError, Stage

# Default user-agent string — identifies the tool to site operators.
# Shared with robots.py so the robots.txt fetch and the actual page fetch
# present the same identity to the server (policy identity = fetch identity).
_USER_AGENT = "event-intel-mcp/0.1 (exhibitor list acquisition; contact: see GitHub)"
_DEFAULT_TIMEOUT = 20.0


def get_user_agent() -> str:
    """Public accessor for the shared User-Agent string.

    Used by `acquisition.robots` to fetch robots.txt with the same UA that
    `fetch_raw` uses for the actual page request. Keeps `_USER_AGENT` private
    while documenting the cross-module dependency.
    """
    return _USER_AGENT


@dataclass
class RawResponse:
    status: int                           # 0 = transport failure
    headers: dict[str, str]
    body: str                             # decoded with errors="replace"
    content_type: str
    final_url: str                        # URL after redirects
    history: list[str] = field(default_factory=list)  # intermediate URLs
    network_error: str | None = None      # set when status == 0


def fetch_raw(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    allow_cross_origin: bool = False,
    timeout: float = _DEFAULT_TIMEOUT,
    max_redirects: int = 5,
) -> RawResponse:
    """Fetch `url` and return a RawResponse.

    Safety gates:
    - validate_url(url) before any network call (raises on violation).
    - After each redirect, re-validate the target. Default policy allows only
      same-host or subdomain redirects; set allow_cross_origin=True to permit
      cross-origin redirects (still validated for private IPs etc.).

    Transport errors (DNS, timeout, conn refused) return RawResponse(status=0,
    network_error=...) rather than raising, so callers can route through
    http_status_map.map_http_response().
    """
    import httpx  # lazy import — keeps this module import-cold

    validate_url(url)

    merged_headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/json,*/*",
    }
    if headers:
        merged_headers.update(headers)

    landing_host: str = _host_from(url)
    history_urls: list[str] = []

    try:
        with httpx.Client(
            follow_redirects=True,
            max_redirects=max_redirects,
            timeout=timeout,
            event_hooks={"request": [], "response": []},
        ) as client:
            resp = client.request(
                method=method.upper(),
                url=url,
                headers=merged_headers,
                params=params or {},
                data=data or {},
            )

            # Validate each redirect target.
            for r in resp.history:
                redirect_url = str(r.url)
                history_urls.append(redirect_url)
                _check_redirect(landing_host, redirect_url, allow_cross_origin)

            final_url = str(resp.url)
            if final_url != url:
                _check_redirect(landing_host, final_url, allow_cross_origin)

            ct = resp.headers.get("content-type", "")
            body = resp.text  # httpx decodes; falls back to errors="replace"
            return RawResponse(
                status=resp.status_code,
                headers=dict(resp.headers),
                body=body,
                content_type=ct,
                final_url=final_url,
                history=history_urls,
            )

    except MCPError:
        # Safety violations from _check_redirect — re-raise.
        raise
    except Exception as exc:
        # Transport failure: DNS error, timeout, conn refused, etc.
        # Return a response with status=0 so http_status_map can classify.
        return RawResponse(
            status=0,
            headers={},
            body="",
            content_type="",
            final_url=url,
            history=history_urls,
            network_error=f"{type(exc).__name__}: {exc}",
        )


def _host_from(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).hostname or ""


def _check_redirect(
    landing_host: str,
    redirect_url: str,
    allow_cross_origin: bool,
) -> None:
    """Validate a redirect target. Raises MCPError(INVALID_INPUT) on violation."""
    # Always check for private IPs and forbidden schemes.
    validate_url(redirect_url)

    if not allow_cross_origin:
        redirect_host = _host_from(redirect_url)
        rel = host_relation(landing_host, redirect_host)
        if rel == "cross":
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT,
                stage=Stage.ACQUISITION,
                message=(
                    f"Redirect to cross-origin host {redirect_host!r} is not allowed "
                    f"(landing host: {landing_host!r}). "
                    "Pass allow_cross_origin=True to permit cross-origin redirects."
                ),
                hint={
                    "landing_host": landing_host,
                    "redirect_host": redirect_host,
                    "redirect_url": redirect_url,
                    "rule": "cross-origin redirects blocked by default",
                },
                retryable=False,
            )
