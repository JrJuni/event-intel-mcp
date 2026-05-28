"""HTTP status → MCPError mapping for the Phase 18T acquisition layer.

map_http_response(resp, *, landing_url) -> tuple[bool, MCPError | None]

Returns (should_proceed, error_or_none).
  - should_proceed=True: caller may continue with resp.body.
  - should_proceed=False: caller should raise/return the MCPError.

This module centralises Contract #9 from plan v1.2 so that analyze /
probe / acquire never drift on the meaning of 403 vs 429 vs short-body.

raw_fetch.py owns only safety + transport-level errors. HTTP semantics
(auth walls, bot blocks, server errors) are this module's job.

Short-body rule (R2-2 SPA-shell fix):
  200 + body < 1KB + no <script> + no endpoint hint → OPERATOR_CAPTURE_REQUIRED
  200 + body < 1KB + has <script> OR endpoint hint  → proceed + warning
  This prevents blocking JS-rendered exhibitor shells that only contain script
  references (the exact sites Phase 18T is designed to crack).
"""
from __future__ import annotations

import re

from event_intel.acquisition.raw_fetch import RawResponse
from event_intel.errors import ErrorCode, MCPError, Stage

# Body length below which we run the short-body heuristic.
_SHORT_BODY_THRESHOLD = 1024  # 1 KB

_CAPTCHA_KEYWORDS = (
    "captcha",
    "are you a robot",
    "please verify",
    "cloudflare",
    "access denied",
    "unusual activity",
)

# Patterns that suggest the short body has endpoint/script hints — SPA shells.
_ENDPOINT_HINTS = (
    "fetch(",
    "axios",
    "$.ajax",
    "xmlhttprequest",
    "api/",
    ".asp",
    ".php",
    ".json",
)

_SCRIPT_TAG_RE = re.compile(r"<script", re.IGNORECASE)


def _has_script_or_hint(body: str) -> bool:
    lower = body.lower()
    if _SCRIPT_TAG_RE.search(body):
        return True
    return any(h in lower for h in _ENDPOINT_HINTS)


def _has_captcha(body: str) -> bool:
    lower = body.lower()
    return any(kw in lower for kw in _CAPTCHA_KEYWORDS)


def _make_error(
    code: ErrorCode,
    message: str,
    *,
    retryable: bool = False,
    hint: dict | None = None,
) -> MCPError:
    return MCPError(
        error_code=code,
        stage=Stage.ACQUISITION,
        message=message,
        hint=hint,
        retryable=retryable,
    )


def map_http_response(
    resp: RawResponse,
    *,
    landing_url: str = "",
) -> tuple[bool, MCPError | None]:
    """Map a RawResponse to an acquisition outcome.

    Returns:
        (True, None)          — proceed with resp.body
        (False, MCPError)     — caller should surface this error
        (True, MCPError)      — proceed, but attach the error as a warning
                                (used for short_body_with_scripts case)
    """
    # Transport failure (status == 0 from raw_fetch).
    if resp.network_error is not None:
        return False, _make_error(
            ErrorCode.UPSTREAM_ERROR,
            f"Network error fetching {landing_url or resp.final_url}: {resp.network_error}",
            retryable=True,
            hint={"network_error": resp.network_error, "url": landing_url or resp.final_url},
        )

    status = resp.status

    # 2xx — check body quality.
    if 200 <= status < 300:
        body = resp.body or ""

        # CAPTCHA / bot-wall (regardless of body length).
        if _has_captcha(body):
            return False, _make_error(
                ErrorCode.OPERATOR_CAPTURE_REQUIRED,
                "Page appears to be bot-protected (CAPTCHA / challenge detected). "
                "Save the page manually and use source_kind=html_file.",
                retryable=False,
                hint={
                    "url": landing_url or resp.final_url,
                    "fix": (
                        "Open the URL in a browser, scroll to load exhibitors, "
                        "Ctrl+S → 'Webpage, Complete', then use build-event --html-file."
                    ),
                },
            )

        # Short-body heuristic (R2-2).
        if len(body) < _SHORT_BODY_THRESHOLD:
            if _has_script_or_hint(body):
                # SPA shell with script references — analyzer should proceed.
                # Return proceed=True + a warning MCPError (caller treats as advisory).
                warning = _make_error(
                    ErrorCode.INTERNAL,  # advisory only — not surfaced as failure
                    "short_body_with_scripts",
                    retryable=False,
                    hint={"body_bytes": len(body), "url": landing_url or resp.final_url},
                )
                return True, warning
            else:
                return False, _make_error(
                    ErrorCode.OPERATOR_CAPTURE_REQUIRED,
                    f"Page body is very short ({len(body)} bytes) with no script or API hints. "
                    "The page likely requires JavaScript execution or is an inert shell.",
                    retryable=False,
                    hint={
                        "url": landing_url or resp.final_url,
                        "body_bytes": len(body),
                        "fix": (
                            "Open the URL in a browser, let it load, "
                            "Ctrl+S → 'Webpage, Complete', then use build-event --html-file."
                        ),
                    },
                )

        return True, None

    # 401 / 403 — auth wall.
    if status in (401, 403):
        return False, _make_error(
            ErrorCode.LOGIN_REQUIRED,
            f"HTTP {status}: page requires authentication.",
            retryable=False,
            hint={
                "url": landing_url or resp.final_url,
                "fix": (
                    "Check for an official exhibitor API or contact the organizer for "
                    "a downloadable participant list."
                ),
            },
        )

    # 404 — URL doesn't exist.
    if status == 404:
        return False, _make_error(
            ErrorCode.INVALID_INPUT,
            f"HTTP 404: URL not found — {landing_url or resp.final_url}",
            retryable=False,
            hint={
                "url": landing_url or resp.final_url,
                "fix": "Verify the landing URL is correct.",
            },
        )

    # 429 — rate limited.
    if status == 429:
        retry_after = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
        return False, _make_error(
            ErrorCode.RATE_LIMITED,
            f"HTTP 429: rate limited by {landing_url or resp.final_url}",
            retryable=True,
            hint={
                "url": landing_url or resp.final_url,
                "retry_after": retry_after,
            },
        )

    # 5xx — server error.
    if status >= 500:
        return False, _make_error(
            ErrorCode.UPSTREAM_ERROR,
            f"HTTP {status}: server error from {landing_url or resp.final_url}",
            retryable=True,
            hint={"url": landing_url or resp.final_url, "status": status},
        )

    # Other 1xx / 3xx that raw_fetch didn't resolve (shouldn't happen with
    # follow_redirects=True, but be defensive).
    return False, _make_error(
        ErrorCode.UPSTREAM_ERROR,
        f"HTTP {status}: unexpected status from {landing_url or resp.final_url}",
        retryable=False,
        hint={"status": status, "url": landing_url or resp.final_url},
    )
