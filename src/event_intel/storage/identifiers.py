"""Slug sanitization + validation + suggestion.

Per plan v0.5 §S6 + Contract #15 + #19 (INVALID_INPUT hint):

    sanitize_slug(s)  -> str    # raises MCPError(INVALID_INPUT) with suggested_slug on failure
    validate_slug(s)  -> bool   # pure boolean predicate, no raise
    suggest_slug(s)   -> str    # best-effort ASCII transliteration; hash-suffix fallback

The slug grammar is `^[a-zA-Z0-9_-]{1,64}$`. This is the gate for every
workspace_id / event_slug entering the system — MCP tools, CLI, preflight.
Without it, a Korean event name like "서울 ITS 2026" would either path-
traverse, crash Chroma's collection-name validator, or silently truncate
to an ambiguous collection key.

`suggest_slug` produces a *usable* slug — not a perfect transliteration.
Romanization of Korean / Chinese / Arabic requires a domain dictionary
we deliberately don't ship in v0. So we:
  1. NFKD-decompose so Latin diacritics fold to ASCII (café → cafe)
  2. Keep any surviving [a-z0-9 _-] characters from the original
  3. Lowercase + collapse whitespace runs to single hyphens
  4. Strip leading/trailing hyphens/underscores
  5. Truncate to 64 chars
  6. If the result is empty (all non-ASCII input), fall back to
     `event-{sha1(original)[:8]}` — still a valid slug, deterministic
     across re-runs of the same input.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

from event_intel.errors import ErrorCode, MCPError, Stage

_SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_SLUG_MAX_LEN = 64
_FALLBACK_PREFIX = "event-"


def validate_slug(s: str) -> bool:
    """True iff `s` matches `^[a-zA-Z0-9_-]{1,64}$`. Never raises."""
    return bool(s) and isinstance(s, str) and bool(_SLUG_RE.match(s))


def suggest_slug(s: str) -> str:
    """Best-effort ASCII-safe slug derivation. Always returns a valid slug.

    Examples (anchor for the test suite):
        "서울 ITS 2026"   -> "its-2026"
        "Sample Expo!"   -> "sample-expo"
        "café résumé"    -> "cafe-resume"
        "a/b"            -> "a-b"
        "한글만"          -> "event-{8-hex-sha1}"
        ""               -> "event-{8-hex-sha1-of-empty}"
        "A" * 100        -> "a" * 64
    """
    raw = s if isinstance(s, str) else str(s or "")

    # NFKD splits café -> "cafe" + combining acute. Filtering combining marks
    # leaves ASCII letters intact and folds most European diacritics.
    nfkd = unicodedata.normalize("NFKD", raw)
    stripped = "".join(
        ch for ch in nfkd if not unicodedata.combining(ch)
    )

    # Replace runs of non-slug-safe characters with a single hyphen so word
    # boundaries are preserved in the output.
    out_chars: list[str] = []
    for ch in stripped:
        if ch.isascii() and (ch.isalnum() or ch in "_-"):
            out_chars.append(ch.lower())
        else:
            out_chars.append("-")
    collapsed = re.sub(r"-+", "-", "".join(out_chars)).strip("-_")

    if collapsed:
        return collapsed[:_SLUG_MAX_LEN]

    # All non-ASCII input — hash-suffix fallback, deterministic per input.
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{_FALLBACK_PREFIX}{digest}"


def sanitize_slug(s: str, *, field_name: str = "slug") -> str:
    """Return `s` if it's already a valid slug. Otherwise raise MCPError with
    a `suggested_slug` in the hint.

    The MCP tool boundary catches and renders the envelope, so callers see:

        {"ok": false, "error_code": "INVALID_INPUT", "stage": "preflight",
         "message": "event_slug '서울 ITS 2026' violates [a-zA-Z0-9_-]{1,64}",
         "hint": {"suggested_slug": "its-2026", "rule": "..."}}
    """
    if validate_slug(s):
        return s
    suggested = suggest_slug(s if isinstance(s, str) else "")
    raise MCPError(
        error_code=ErrorCode.INVALID_INPUT,
        stage=Stage.PREFLIGHT,
        message=(
            f"{field_name} {s!r} violates [a-zA-Z0-9_-]{{1,64}}"
            if isinstance(s, str) and s
            else f"{field_name} is empty or not a string"
        ),
        hint={
            "rule": "^[a-zA-Z0-9_-]{1,64}$",
            "suggested_slug": suggested,
            "field": field_name,
        },
        retryable=False,
    )
