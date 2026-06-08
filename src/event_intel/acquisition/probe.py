"""probe_exhibitor_endpoint core logic — Phase 18T T2.

Pipeline (0 LLM calls):
  1. Validate hints via AnalyzeHints.model_validate (INVALID_INPUT on failure).
  2. url_safety.validate_url(landing_url) + robots.is_allowed check.
  3. For each candidate (cap at 5):
     a. Restrict method to {GET, POST}; non-allowed → skip + attempt log.
     b. host_relation check; cross-origin skipped unless allow_cross_origin=True.
     c. robots check on candidate URL.
     d. raw_fetch.fetch_raw → http_status_map.map_http_response.
        - should_proceed=False → skip + log.
        - (True, warning) → proceed + carry warning in attempt log (SPA shell).
     e. _response_looks_like_exhibitor_list → score 0..1.
  4. Return best-scoring candidate above min_score; else ACQUISITION_AMBIGUOUS.

  For embedded_json verdict: probe_embedded_json uses stdlib regex to locate
  the JSON blob, then dotted-key walk. No JSONPath dependency.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from event_intel.errors import ErrorCode, MCPError, Stage

# --- keyword sets for exhibitor-list scorer ---

_EN_KEYWORDS = frozenset({
    "exhibitor", "exhibitors", "participant", "participants",
    "booth", "company", "companies", "stand",
})
_KO_KEYWORDS = frozenset({
    "참가업체", "참가사", "회사명", "부스", "출품사", "출품업체",
    "전시업체", "참가기업",
})

ALLOWED_METHODS = frozenset({"GET", "POST"})
MIN_SCORE_DEFAULT = 0.5
MAX_CANDIDATES = 5

# Redaction (v2.1 §F): never persist token-like values in provenance / logs.
_SENSITIVE_KEY_RE = re.compile(
    r"token|key|secret|password|sig|signature|auth", re.IGNORECASE
)
_REDACTED = "***REDACTED***"


def _redact_params(d: dict[str, str] | None) -> dict[str, str]:
    if not d:
        return {}
    return {k: (_REDACTED if _SENSITIVE_KEY_RE.search(k) else v) for k, v in d.items()}


def _redacted_request_spec(
    *, url: str, method: str, params: dict | None, data: dict | None, referer: str
) -> dict:
    """Reproducible request shape with sensitive query/body values redacted.

    Authorization / Cookie headers are never put here (only Referer is sent, and
    it is the public landing URL, not a credential).
    """
    return {
        "url": url,
        "method": method,
        "params": _redact_params(params),
        "data": _redact_params(data),
        "referer": referer,
    }

# Regex for embedded JSON extraction.
_SCRIPT_ID_RE = re.compile(
    r'<script[^>]*\bid=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</script>',
    re.IGNORECASE,
)
_SCRIPT_VAR_RE = re.compile(
    r'(?:var|window\[[\'"]\w+[\'"]\]|window\.)\s*([A-Za-z_$][\w$]*)\s*=\s*(\{[\s\S]*?\});',
)


# --- data classes ---

@dataclass
class ProbeAttempt:
    url: str
    method: str
    status: int
    score: float
    error_code: ErrorCode | None = None
    error_message: str | None = None
    warning: str | None = None
    body: str | None = None               # scored response body (winner returned w/o re-fetch)
    content_type: str | None = None
    request_spec: dict | None = None      # redacted url/method/params/data/referer (provenance)


@dataclass
class ProbeResult:
    winner: ProbeAttempt | None
    attempts: list[ProbeAttempt] = field(default_factory=list)
    body: str | None = None
    content_type: str | None = None


# --- internal helpers ---

def _response_looks_like_exhibitor_list(body: str, lang: str) -> float:
    """Return 0..1 score based on keyword density in the response body."""
    if not body:
        return 0.0
    lower = body.lower()
    words = set(re.findall(r"[\w가-힣]+", lower))

    if lang.startswith("ko"):
        ko_hits = sum(1 for kw in _KO_KEYWORDS if kw in body)
        en_hits = sum(1 for kw in _EN_KEYWORDS if kw in words)
        # Use the better-scoring language independently so Korean-only pages
        # are not diluted by an EN-keyword denominator.
        density = max(
            ko_hits / len(_KO_KEYWORDS),
            en_hits / len(_EN_KEYWORDS),
        )
    else:
        hits = sum(1 for kw in _EN_KEYWORDS if kw in words)
        density = hits / len(_EN_KEYWORDS)

    # Bonus for substantial body size (likely real list, not a stub).
    size_bonus = min(len(body) / 10_000, 0.2)
    return min(density + size_bonus, 1.0)


# --- language-neutral JSON roster validator (review #2 / v2.1 §C) ---

# A roster must carry a company-specific signal — not just a generic "name" field
# (which a product catalog or staff list also has). The array KEY or a FIELD name
# must match one of these tokens.
_COMPANY_SIGNAL_RE = re.compile(
    r"compan|exhibitor|organi[sz]ation|\borg\b|booth|vendor|participant|参展|出展"
    r"|会社|社名|会員|회사|업체|기업|참가",
    re.IGNORECASE,
)
# Fields whose VALUE is treated as the company name (for the unique-name count).
_NAME_FIELD_RE = re.compile(
    r"name|title|社名|会社|회사|업체|기업|compan|exhibitor", re.IGNORECASE
)


def _looks_like_json(body: str, content_type: str | None) -> bool:
    if content_type and "json" in content_type.lower():
        return True
    head = body.lstrip()[:1]
    return head in ("{", "[")


def _find_largest_dict_list(
    data: Any, *, max_depth: int, max_nodes: int
) -> tuple[list, str | None]:
    """Bounded DFS for the largest list-of-dicts and the key it hangs off.

    HCR's roster is `{"company_data": [ ... ]}` — not the root — so we must look
    past the top level, but depth/node caps keep a hostile JSON from blowing up.
    """
    best: tuple[list, str | None] = ([], None)
    nodes = 0
    stack: list[tuple[Any, str | None, int]] = [(data, None, 0)]
    while stack:
        obj, key, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            continue
        if isinstance(obj, list):
            dicts = [x for x in obj if isinstance(x, dict)]
            if len(dicts) > len(best[0]):
                best = (dicts, key)
            for x in obj:
                if isinstance(x, (list, dict)):
                    stack.append((x, key, depth + 1))
        elif isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (list, dict)):
                    stack.append((v, str(k), depth + 1))
    return best


def _roster_structural_score(
    data: Any, *, min_repeated: int, min_unique: int, max_depth: int, max_nodes: int
) -> float:
    """Score a parsed JSON value 0..1 as an exhibitor roster — language-neutral."""
    dicts, parent_key = _find_largest_dict_list(
        data, max_depth=max_depth, max_nodes=max_nodes
    )
    if len(dicts) < min_repeated:
        return 0.0

    field_names: set[str] = set()
    for d in dicts[:50]:
        field_names.update(str(k) for k in d.keys())

    # Company-specific signal: the array key OR a field name. A product catalog
    # ("products"/"product_name") or staff list ("staff"/"name") fails here.
    signal_sources = list(field_names) + ([parent_key] if parent_key else [])
    if not any(_COMPANY_SIGNAL_RE.search(s) for s in signal_sources):
        return 0.0

    name_fields = [f for f in field_names if _NAME_FIELD_RE.search(f)]
    uniq: set[str] = set()
    for d in dicts:
        for f in name_fields:
            v = d.get(f)
            if isinstance(v, str) and v.strip():
                uniq.add(v.strip())
    if len(uniq) < min_unique:
        return 0.0

    return min(1.0, 0.6 + len(dicts) / 1000.0)


def _response_looks_like_roster(
    body: str,
    content_type: str | None,
    lang: str = "en",
    *,
    min_repeated: int = 5,
    min_unique: int = 5,
    max_depth: int = 4,
    max_nodes: int = 2000,
) -> float:
    """Score a probe response: structural for JSON, keyword for HTML.

    A JSON body is validated by structure (repeated company objects), so a
    language-neutral roster with field names like `company_name` / `회사명` /
    `会社名` scores even though it contains none of the EN/KO keyword tokens.
    Non-JSON (or unparseable JSON) falls back to the keyword scorer.
    """
    if _looks_like_json(body, content_type):
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return _response_looks_like_exhibitor_list(body, lang)
        return _roster_structural_score(
            data,
            min_repeated=min_repeated,
            min_unique=min_unique,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
    return _response_looks_like_exhibitor_list(body, lang)


def _dotted_key_walk(obj: Any, key_path: str) -> Any:
    """Walk a nested dict/list using a dotted key path like 'props.pageProps.exhibitors'."""
    parts = key_path.split(".")
    for part in parts:
        if not part:
            continue
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (IndexError, ValueError):
                return None
        else:
            return None
        if obj is None:
            return None
    return obj


# --- probe_endpoints ---

def probe_endpoints(
    *,
    url: str,
    hints: dict | None,
    lang: str = "en",
    allow_cross_origin: bool = False,
    min_score: float = MIN_SCORE_DEFAULT,
) -> ProbeResult:
    """Try each candidate endpoint from analyzer hints; return the best-scoring one.

    Safety gates (url_safety + robots) run independently for both the landing
    URL and each candidate URL — never assumes the caller validated.

    Returns ProbeResult with winner=None + ACQUISITION_AMBIGUOUS if no candidate
    clears min_score.
    """
    from pydantic import ValidationError

    from event_intel.acquisition import http_status_map as _status_map
    from event_intel.acquisition import raw_fetch as _raw_fetch
    from event_intel.acquisition import robots as _robots
    from event_intel.acquisition.analyzer import AnalyzeHints
    from event_intel.acquisition.url_safety import host_relation, validate_url

    # 1. Validate hints.
    try:
        validated_hints = AnalyzeHints.model_validate(hints or {})
    except ValidationError as exc:
        raise MCPError(
            error_code=ErrorCode.INVALID_INPUT,
            stage=Stage.ACQUISITION,
            message="analyzer hints failed schema validation",
            hint={"validation_error": str(exc)},
            retryable=False,
        ) from exc

    # 2. Landing URL safety + robots.
    validate_url(url)
    if not _robots.is_allowed(url):
        raise MCPError(
            error_code=ErrorCode.ROBOTS_DISALLOWED,
            stage=Stage.ACQUISITION,
            message=f"robots.txt disallows fetching {url}",
            hint={
                "robots_url": url.rsplit("/", 1)[0] + "/robots.txt",
                "user_agent": "event-intel-mcp",
                "fix": "Contact the site owner or use operator-assisted capture.",
            },
            retryable=False,
        )

    landing_host = urlparse(url).hostname or ""
    attempts: list[ProbeAttempt] = []

    # 3. Try each candidate.
    for cand in validated_hints.candidate_endpoints[:MAX_CANDIDATES]:
        method = cand.method.upper()

        # a. Method allowlist.
        if method not in ALLOWED_METHODS:
            attempts.append(ProbeAttempt(
                url=cand.url,
                method=cand.method,
                status=0,
                score=0.0,
                error_code=ErrorCode.INVALID_INPUT,
                error_message=f"method {cand.method!r} not in {{GET, POST}}",
            ))
            continue

        # b. Host relation check.
        cand_host = urlparse(cand.url).hostname or ""
        relation = host_relation(landing_host, cand_host)
        if relation == "cross" and not allow_cross_origin:
            attempts.append(ProbeAttempt(
                url=cand.url,
                method=method,
                status=0,
                score=0.0,
                error_code=ErrorCode.INVALID_INPUT,
                error_message=(
                    f"candidate host {cand_host!r} is cross-origin relative to "
                    f"landing host {landing_host!r}; skipped (pass allow_cross_origin=True to override)"
                ),
                warning="cross_origin_skipped",
            ))
            continue

        # b2. URL safety on candidate.
        try:
            validate_url(cand.url)
        except MCPError as e:
            attempts.append(ProbeAttempt(
                url=cand.url, method=method, status=0, score=0.0,
                error_code=e.error_code, error_message=e.message,
            ))
            continue

        # c. Robots check on candidate.
        if not _robots.is_allowed(cand.url):
            attempts.append(ProbeAttempt(
                url=cand.url, method=method, status=0, score=0.0,
                error_code=ErrorCode.ROBOTS_DISALLOWED,
                error_message=f"robots.txt disallows {cand.url}",
            ))
            continue

        # d. Fetch.
        params = cand.sample_params if method == "GET" else None
        post_data = cand.sample_params if method == "POST" else None
        resp = _raw_fetch.fetch_raw(
            cand.url,
            method=method,
            params=params,
            data=post_data,
            headers={"Referer": url},
            allow_cross_origin=allow_cross_origin,
        )

        should_proceed, err_or_warn = _status_map.map_http_response(resp, landing_url=cand.url)
        if not should_proceed:
            attempts.append(ProbeAttempt(
                url=cand.url, method=method, status=resp.status, score=0.0,
                error_code=err_or_warn.error_code if err_or_warn else None,
                error_message=err_or_warn.message if err_or_warn else None,
            ))
            continue

        # should_proceed=True + err_or_warn is not None → advisory warning (SPA shell).
        warning_msg = err_or_warn.message if err_or_warn else None

        # e. Score (structural for JSON, keyword for HTML — review #2 / v2.1 §C).
        score = _response_looks_like_roster(resp.body, resp.content_type, lang)
        attempts.append(ProbeAttempt(
            url=cand.url, method=method, status=resp.status, score=score,
            warning=warning_msg,
            body=resp.body,
            content_type=resp.content_type,
            request_spec=_redacted_request_spec(
                url=cand.url, method=method,
                params=params, data=post_data, referer=url,
            ),
        ))

    # 4. Pick winner. Return the response captured while scoring — no re-fetch,
    # so params/body/Referer of the winning request are not silently dropped
    # (review #3). RequestSpec on the attempt preserves the request for
    # pagination/provenance.
    scored = [a for a in attempts if a.score >= min_score]
    if scored:
        winner = max(scored, key=lambda a: a.score)
        return ProbeResult(
            winner=winner,
            attempts=attempts,
            body=winner.body,
            content_type=winner.content_type,
        )

    raise MCPError(
        error_code=ErrorCode.ACQUISITION_AMBIGUOUS,
        stage=Stage.ACQUISITION,
        message="No candidate endpoint scored above threshold",
        hint={
            "min_score": min_score,
            "attempts": [
                {
                    "url": a.url,
                    "method": a.method,
                    "status": a.status,
                    "score": round(a.score, 3),
                    "error": a.error_message,
                    "warning": a.warning,
                }
                for a in attempts
            ],
        },
        retryable=False,
    )


# --- probe_embedded_json ---

def probe_embedded_json(
    *,
    url: str,
    hints: dict | None,
    prefetched_body: str | None = None,
) -> ProbeResult:
    """Extract embedded JSON from a page using stdlib regex selectors.

    Supports:
    - script_id: matches <script id="X">...</script>
    - script_var_name: matches var X = {...};
    - key_path: dotted-key walk (e.g. 'props.pageProps.exhibitors')

    ``prefetched_body``: when the caller already has the landing HTML (the acquire
    ladder fetches the landing page exactly once and shares it), pass it here to
    skip the re-fetch. Safety gates still run on ``url``; only the network read is
    elided.
    """
    from pydantic import ValidationError

    from event_intel.acquisition import http_status_map as _status_map
    from event_intel.acquisition import raw_fetch as _raw_fetch
    from event_intel.acquisition import robots as _robots
    from event_intel.acquisition.analyzer import AnalyzeHints
    from event_intel.acquisition.url_safety import validate_url

    # 1. Validate hints.
    try:
        validated_hints = AnalyzeHints.model_validate(hints or {})
    except ValidationError as exc:
        raise MCPError(
            error_code=ErrorCode.INVALID_INPUT,
            stage=Stage.ACQUISITION,
            message="analyzer hints failed schema validation",
            hint={"validation_error": str(exc)},
            retryable=False,
        ) from exc

    # 2. Safety gates on landing URL.
    validate_url(url)
    if not _robots.is_allowed(url):
        raise MCPError(
            error_code=ErrorCode.ROBOTS_DISALLOWED,
            stage=Stage.ACQUISITION,
            message=f"robots.txt disallows fetching {url}",
            hint={"url": url, "fix": "Contact the site owner or use operator-assisted capture."},
            retryable=False,
        )

    # 3. Use the shared landing body if provided, else fetch the page once.
    if prefetched_body is not None:
        html = prefetched_body
        resp_status = 200
    else:
        resp = _raw_fetch.fetch_raw(url, method="GET")
        should_proceed, err_or_warn = _status_map.map_http_response(resp, landing_url=url)
        if not should_proceed:
            raise err_or_warn  # type: ignore[misc]
        html = resp.body
        resp_status = resp.status
    selectors = validated_hints.embedded_json_selectors
    if not selectors:
        raise MCPError(
            error_code=ErrorCode.ACQUISITION_AMBIGUOUS,
            stage=Stage.ACQUISITION,
            message="No embedded_json_selectors provided in hints",
            hint={"url": url, "fix": "Re-run analyze_event_page to obtain hints."},
            retryable=False,
        )

    # 4. Try each selector.
    for sel in selectors:
        blob: str | None = None

        if sel.script_id:
            # <script id="X">...</script>
            pattern = re.compile(
                rf'<script[^>]*\bid=["\']{re.escape(sel.script_id)}["\'][^>]*>([\s\S]*?)</script>',
                re.IGNORECASE,
            )
            m = pattern.search(html)
            if m:
                blob = m.group(1).strip()

        elif sel.script_var_name:
            # var X = {...}; or window.X = {...};
            pattern = re.compile(
                rf'(?:var\s+{re.escape(sel.script_var_name)}'
                rf'|window\[[\'"]{re.escape(sel.script_var_name)}[\'"]\]'
                rf'|window\.{re.escape(sel.script_var_name)})\s*=\s*(\{{[\s\S]*?\}});',
            )
            m = pattern.search(html)
            if m:
                blob = m.group(1).strip()

        if blob is None:
            continue

        # 5. JSON-parse the blob.
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue

        # 6. Walk key_path if provided.
        if sel.key_path:
            data = _dotted_key_walk(data, sel.key_path)
            if data is None:
                continue

        # 7. Serialize the extracted data back to JSON text for the caller.
        extracted_text = json.dumps(data, ensure_ascii=False)
        attempt = ProbeAttempt(
            url=url, method="GET", status=resp_status,
            score=_response_looks_like_roster(extracted_text, "application/json", "en"),
        )
        return ProbeResult(
            winner=attempt,
            attempts=[attempt],
            body=extracted_text,
            content_type="application/json",
        )

    raise MCPError(
        error_code=ErrorCode.ACQUISITION_AMBIGUOUS,
        stage=Stage.ACQUISITION,
        message="No embedded JSON selector matched the page content",
        hint={
            "url": url,
            "selectors_tried": len(selectors),
            "fix": (
                "Try a different script_id or script_var_name, or use "
                "probe_exhibitor_endpoint with the xhr_endpoint verdict instead."
            ),
        },
        retryable=False,
    )
