"""acquire_exhibitor_source orchestrator — Phase 18T T3 + agentic ladder (C7).

Pipeline (≤ 1 LLM call):
  1. Sanitize workspace_id + event_slug.
  2. URL safety + robots gates (landing-level).
  3. Cache lookup. manifest + sha256 ok + refetch=False → return cached.
  4. Fetch the landing page ONCE, classify it with analyze_response (1 Sonnet
     call). A hard landing status (401/403/404/5xx/transport) raises here, before
     the LLM — that is the only "fatal" early-exit (design v2.1 §B).
  5. Run the strategy ladder. The analyzer verdict is a *prior* that orders the
     rungs, not a gate — every rung in the fixed set is reachable, evidence-gated
     and budget-bounded:
        static   — the already-fetched landing body, if it scores as a roster
        embedded — embedded-JSON selectors against the shared landing body
        xhr      — candidate endpoints from hints (probe_endpoints)
        bundle   — endpoints discovered inside same-origin external <script src>
                   bundles (the H.C.R. Vue-SPA case)
        operator — terminal: raise OPERATOR_CAPTURE_REQUIRED (or LOGIN_REQUIRED
                   when the prior was login_required)
  6. Write artifact (content-type-aware naming) + manifest with provenance
     (selected_rung, redacted winning_request, analysis_fp, config_fp).

A single AcquireBudget enforces the per-response byte cap, a cumulative byte cap,
a total HTTP-call cap, and a wall-clock deadline, with calls reserved for the
bundle rung so a wrong prior can't starve it (design v2.1 §D). LLM calls after
step 4: zero. Determinism scope: for a fixed stored analysis/hints + fixed HTTP
responses, the internal run is deterministic (the analyzer LLM and the host
retry loop are not — design v2.1 §B / review #4).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from event_intel.errors import ErrorCode, MCPError, Stage

_log = logging.getLogger(__name__)

# Score at/above which a probe/landing body is accepted as an exhibitor roster.
_MIN_ROSTER_SCORE = 0.5

# Shipped acquisition defaults — deep-merged under config["acquisition"] so an
# absent block (or a partial user override) still yields a complete budget.
_ACQ_DEFAULTS: dict[str, Any] = {
    "max_bytes_per_fetch": 5_000_000,
    "max_external_bundles": 8,
    "max_candidate_endpoints": 5,
    "max_http_calls_per_acquire": 20,
    "rung_timeout_seconds": 20,
    "overall_deadline_seconds": 60,
    "bundle_discovery_enabled": True,
    "roster_min_repeated_objects": 5,
    "roster_min_unique_names": 5,
    "cumulative_max_bytes": 20_000_000,
    "per_rung_http_quota": 6,
    "reserved_calls_bundle_rung": 4,
    "roster_max_depth": 4,
    "roster_max_nodes": 2000,
}

# Per-verdict rung order. The verdict orders the *same* fixed rung set — it never
# removes a rung — so a wrong prior still reaches the others (review #6).
_RUNG_ORDER: dict[str, list[str]] = {
    "static_html": ["static", "embedded", "xhr", "bundle", "operator"],
    "xhr_endpoint": ["xhr", "bundle", "embedded", "static", "operator"],
    "embedded_json": ["embedded", "xhr", "bundle", "static", "operator"],
    "operator_capture_required": ["static", "embedded", "xhr", "bundle", "operator"],
    "login_required": ["static", "embedded", "xhr", "bundle", "operator"],
}
_DEFAULT_ORDER = ["static", "embedded", "xhr", "bundle", "operator"]
_NETWORK_RUNGS = frozenset({"embedded", "xhr", "bundle"})


@dataclass
class AcquireResult:
    source_kind: str
    source_ref: str
    analysis: dict[str, Any] = field(default_factory=dict)
    probe: dict[str, Any] | None = None
    artifact_path: Path | None = None
    manifest_path: Path | None = None
    selected_rung: str | None = None


@dataclass
class _LadderWin:
    body: str
    content_type: str
    source_kind: str
    basename: str
    selected_rung: str
    status: int
    http_pages: int
    winning_request: dict | None = None
    probe_dict: dict[str, Any] | None = None


@dataclass
class AcquireBudget:
    """Real resource budget for one acquire run (design v2.1 §D).

    Charges every acquire-direct fetch (landing, bundle discovery, pagination).
    Probe rungs are bounded by their own MAX_CANDIDATES cap and gated by a
    pre-check here; their internal per-candidate fetches are accounted in bulk
    rather than per-byte (they fetch endpoints, not the landing, so the wall-clock
    deadline still bounds them).
    """

    max_http_calls: int
    cumulative_max_bytes: int
    deadline: float                  # time.monotonic() timestamp
    reserved_bundle_calls: int
    http_calls: int = 0
    cumulative_bytes: int = 0

    @classmethod
    def from_config(cls, acq: dict[str, Any]) -> AcquireBudget:
        import time
        return cls(
            max_http_calls=int(acq["max_http_calls_per_acquire"]),
            cumulative_max_bytes=int(acq["cumulative_max_bytes"]),
            deadline=time.monotonic() + float(acq["overall_deadline_seconds"]),
            reserved_bundle_calls=int(acq["reserved_calls_bundle_rung"]),
        )

    def _time_left(self) -> float:
        import time
        return self.deadline - time.monotonic()

    def may_fetch(self, *, reserved: int = 0) -> bool:
        """True if another fetch is allowed, keeping `reserved` calls in reserve."""
        if self.cumulative_bytes >= self.cumulative_max_bytes:
            return False
        if self._time_left() <= 0:
            return False
        return self.http_calls < (self.max_http_calls - reserved)

    def charge(self, resp: Any) -> None:
        self.http_calls += 1
        n = getattr(resp, "byte_count", 0) or len(getattr(resp, "body", "") or "")
        self.cumulative_bytes += n

    def charge_bulk(self, *, calls: int, body: str | None) -> None:
        self.http_calls += max(1, calls)
        self.cumulative_bytes += len(body or "")


def _artifact_naming(content_type: str, body: str) -> tuple[str, str]:
    """Return (basename, source_kind) from content-type, confirmed by a body sniff.

    A JSON winner is preserved as source.json / text_file (review #7), but only
    when the body is actually JSON-shaped ('{' or '['), so a paginated wrapper or
    an HTML SPA shell served with a json content-type still lands as html_file.
    """
    ct = (content_type or "").lower()
    head = body.lstrip()[:1] if body else ""
    if "json" in ct and head in ("{", "["):
        return "source.json", "text_file"
    return "source.html", "html_file"


def _fingerprint(obj: Any) -> str:
    """Short stable sha256 of a JSON-able object (provenance, not security)."""
    import hashlib
    import json
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def acquire_source(
    *,
    url: str,
    workspace_id: str,
    event_slug: str,
    lang: str = "en",
    refetch: bool = False,
    max_pages: int = 3,
) -> AcquireResult:
    """Orchestrate landing fetch → classify → strategy ladder → artifact.

    Raises MCPError on any unrecoverable failure.
    The caller (tool handler) wraps in envelope_from_exception.
    """
    from event_intel.acquisition import analyzer as _analyzer
    from event_intel.acquisition import raw_fetch as _raw_fetch
    from event_intel.acquisition import robots as _robots
    from event_intel.acquisition.url_safety import validate_url
    from event_intel.providers import llm as _llm
    from event_intel.runtime import preflight as _preflight
    from event_intel.storage import artifacts as _artifacts
    from event_intel.storage.identifiers import sanitize_slug

    # 1. Sanitize slugs.
    sanitize_slug(workspace_id)
    sanitize_slug(event_slug)

    # 2. URL safety + robots (landing-level — fatal, never bypassed).
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

    # 3. Cache lookup.
    art_dir = _artifacts.artifact_dir(workspace_id=workspace_id, event_slug=event_slug)
    if not refetch:
        manifest = _artifacts.read_manifest(art_dir)
        if manifest is not None:
            artifact_path = Path(manifest.source_ref)
            if artifact_path.is_file():
                if _artifacts.verify_artifact_sha256(artifact_path, manifest.sha256):
                    _log.info("Cache hit for %s/%s — skipping re-acquisition.", workspace_id, event_slug)
                    return AcquireResult(
                        source_kind=manifest.source_kind,
                        source_ref=manifest.source_ref,
                        analysis={"verdict": manifest.verdict, "cached": True},
                        artifact_path=artifact_path,
                        manifest_path=art_dir / "manifest.json",
                        selected_rung=manifest.selected_rung,
                    )
                else:
                    _log.warning(
                        "Artifact sha256 mismatch for %s/%s — refetching.",
                        workspace_id, event_slug,
                    )

    # 4. Config + budget, then fetch the landing page once and classify it.
    config = _preflight.load_config()
    acq = {**_ACQ_DEFAULTS, **(config.get("acquisition") or {})}
    model = config.get("llm", {}).get("extract_exhibitors_model", "claude-sonnet-4-6")
    max_tokens = int(config.get("llm", {}).get("extract_max_tokens", 2048))
    llm_provider = _llm.make_llm_provider(config, model=model)

    budget = AcquireBudget.from_config(acq)
    landing_resp = _raw_fetch.fetch_raw(url, method="GET", max_bytes=acq["max_bytes_per_fetch"])
    budget.charge(landing_resp)

    # analyze_response maps the HTTP status first — a hard landing status raises
    # (LOGIN_REQUIRED / INVALID_INPUT / UPSTREAM_ERROR) before the LLM call.
    analysis = _analyzer.analyze_response(
        resp=landing_resp,
        url=url,
        lang=lang,
        llm_provider=llm_provider,
        max_tokens=max_tokens,
    )
    verdict = analysis["verdict"]
    hints = analysis.get("hints", {})

    # 5. Strategy ladder.
    win = _run_ladder(
        verdict=verdict,
        hints=hints,
        landing_resp=landing_resp,
        url=url,
        lang=lang,
        budget=budget,
        acq=acq,
        max_pages=max_pages,
    )

    # 6. Write artifact + manifest with provenance.
    artifact_path = _artifacts.write_artifact(art_dir, win.basename, win.body)
    manifest_dict = _artifacts.make_manifest(
        verdict=verdict,
        source_kind=win.source_kind,
        source_ref=str(artifact_path),
        url=url,
        content_type=win.content_type,
        status=win.status,
        http_pages=win.http_pages,
        artifact_path=artifact_path,
        selected_rung=win.selected_rung,
        winning_request=win.winning_request,
        analysis_fp=_fingerprint({"verdict": verdict, "hints": hints}),
        config_fp=_fingerprint(acq),
    )
    manifest_path = _artifacts.write_manifest(art_dir, manifest_dict)

    return AcquireResult(
        source_kind=win.source_kind,
        source_ref=str(artifact_path),
        analysis=analysis,
        probe=win.probe_dict,
        artifact_path=artifact_path,
        manifest_path=manifest_path,
        selected_rung=win.selected_rung,
    )


# ---- ladder ----


def _run_ladder(
    *,
    verdict: str,
    hints: dict[str, Any],
    landing_resp: Any,
    url: str,
    lang: str,
    budget: AcquireBudget,
    acq: dict[str, Any],
    max_pages: int,
) -> _LadderWin:
    """Run rungs in verdict-priority order; return the first win, else terminal."""
    order = _RUNG_ORDER.get(verdict, _DEFAULT_ORDER)
    for rung in order:
        if rung == "operator":
            break
        # Network rungs need budget headroom; static only reads the cached body.
        if rung in _NETWORK_RUNGS and not budget.may_fetch(
            reserved=(0 if rung == "bundle" else budget.reserved_bundle_calls)
        ):
            continue

        if rung == "static":
            win = _rung_static(landing_resp, url, lang, acq)
        elif rung == "embedded":
            win = _rung_embedded(hints, landing_resp, url, lang, budget)
        elif rung == "xhr":
            win = _rung_xhr(hints, url, lang, budget, max_pages)
        elif rung == "bundle":
            win = _rung_bundle(landing_resp, url, lang, budget, acq, max_pages)
        else:  # pragma: no cover - defensive
            win = None

        if win is not None:
            return win

    raise _terminal_error(verdict, url)


def _rung_static(landing_resp: Any, url: str, lang: str, acq: dict[str, Any]) -> _LadderWin | None:
    """Accept the already-fetched landing body only if it scores as a roster.

    An SPA shell that merely contains a few keywords scores below the floor and
    is rejected here, so it is never mistaken for a static success (review #6).
    """
    from event_intel.acquisition import probe as _probe

    body = landing_resp.body or ""
    score = _probe._response_looks_like_roster(
        body,
        landing_resp.content_type,
        lang,
        min_repeated=acq["roster_min_repeated_objects"],
        min_unique=acq["roster_min_unique_names"],
        max_depth=acq["roster_max_depth"],
        max_nodes=acq["roster_max_nodes"],
    )
    if score < _MIN_ROSTER_SCORE:
        return None
    basename, kind = _artifact_naming(landing_resp.content_type, body)
    return _LadderWin(
        body=body,
        content_type=landing_resp.content_type or "text/html",
        source_kind=kind,
        basename=basename,
        selected_rung="static",
        status=landing_resp.status,
        http_pages=1,
        winning_request=_probe._redacted_request_spec(
            url=url, method="GET", params=None, data=None, referer=""
        ),
        probe_dict=None,
    )


def _rung_embedded(
    hints: dict[str, Any], landing_resp: Any, url: str, lang: str, budget: AcquireBudget
) -> _LadderWin | None:
    from event_intel.acquisition import probe as _probe

    if not hints.get("embedded_json_selectors"):
        return None
    try:
        # Reuse the shared landing body — no second landing fetch.
        pr = _probe.probe_embedded_json(
            url=url, hints=hints, prefetched_body=landing_resp.body or ""
        )
    except MCPError:
        return None
    budget.charge_bulk(calls=0, body=pr.body)
    body = pr.body or "{}"
    ct = pr.content_type or "application/json"
    basename, kind = _artifact_naming(ct, body)
    return _LadderWin(
        body=body,
        content_type=ct,
        source_kind=kind,
        basename=basename,
        selected_rung="embedded",
        status=pr.winner.status if pr.winner else 200,
        http_pages=1,
        winning_request=pr.winner.request_spec if pr.winner else None,
        probe_dict={"winner_url": pr.winner.url if pr.winner else None},
    )


def _rung_xhr(
    hints: dict[str, Any], url: str, lang: str, budget: AcquireBudget, max_pages: int
) -> _LadderWin | None:
    from event_intel.acquisition import probe as _probe

    if not hints.get("candidate_endpoints"):
        return None
    try:
        pr = _probe.probe_endpoints(url=url, hints=hints, lang=lang)
    except MCPError:
        return None
    budget.charge_bulk(calls=len(pr.attempts), body=pr.body)
    return _win_from_probe(pr, selected_rung="xhr", budget=budget, max_pages=max_pages)


def _rung_bundle(
    landing_resp: Any,
    url: str,
    lang: str,
    budget: AcquireBudget,
    acq: dict[str, Any],
    max_pages: int,
) -> _LadderWin | None:
    """Discover endpoints inside same-origin external bundles, then probe them."""
    if not acq.get("bundle_discovery_enabled", True):
        return None

    from event_intel.acquisition import analyzer as _analyzer
    from event_intel.acquisition import probe as _probe
    from event_intel.acquisition import raw_fetch as _raw_fetch

    if not _analyzer.extract_script_srcs(landing_resp.body or ""):
        return None

    def _bfetch(u: str, *, max_bytes: int | None = None, **_: Any) -> Any:
        resp = _raw_fetch.fetch_raw(u, method="GET", max_bytes=max_bytes)
        budget.charge(resp)
        return resp

    allowed = min(
        int(acq["max_external_bundles"]),
        max(0, budget.max_http_calls - budget.http_calls),
    )
    if allowed <= 0:
        return None

    endpoints = _analyzer.discover_endpoints_from_bundles(
        html=landing_resp.body or "",
        page_url=url,
        fetch=_bfetch,
        max_bundles=allowed,
        max_bytes=int(acq["max_bytes_per_fetch"]),
    )
    if not endpoints:
        return None

    hint2 = {
        "candidate_endpoints": [
            {"url": e.url, "method": e.method, "sample_params": {}, "rationale": e.rationale}
            for e in endpoints
        ],
        "embedded_json_selectors": [],
        "operator_action": None,
    }
    try:
        pr = _probe.probe_endpoints(url=url, hints=hint2, lang=lang)
    except MCPError:
        return None
    budget.charge_bulk(calls=len(pr.attempts), body=pr.body)
    win = _win_from_probe(pr, selected_rung="bundle", budget=budget, max_pages=max_pages)
    if win is not None and win.probe_dict is not None:
        win.probe_dict["bundles"] = len(endpoints)
    return win


def _win_from_probe(
    pr: Any, *, selected_rung: str, budget: AcquireBudget, max_pages: int
) -> _LadderWin | None:
    if pr.winner is None:
        return None
    body = pr.body or ""
    ct = pr.content_type or "text/html"
    status = pr.winner.status
    body, http_pages = _maybe_paginate(pr.winner, body, budget, max_pages)
    basename, kind = _artifact_naming(ct, body)
    return _LadderWin(
        body=body,
        content_type=ct,
        source_kind=kind,
        basename=basename,
        selected_rung=selected_rung,
        status=status,
        http_pages=http_pages,
        winning_request=pr.winner.request_spec,
        probe_dict={"winner_url": pr.winner.url, "attempts": len(pr.attempts)},
    )


def _terminal_error(verdict: str, url: str) -> MCPError:
    """Exhausted every recovery rung — surface the analyzer's terminal prior."""
    if verdict == "login_required":
        return MCPError(
            error_code=ErrorCode.LOGIN_REQUIRED,
            stage=Stage.ACQUISITION,
            message="Page requires authentication and no public roster was recoverable.",
            hint={
                "url": url,
                "fix": (
                    "This page requires authentication. Check for an official "
                    "exhibitor API or contact the organizer."
                ),
            },
            retryable=False,
        )
    return MCPError(
        error_code=ErrorCode.OPERATOR_CAPTURE_REQUIRED,
        stage=Stage.ACQUISITION,
        message="Automatic strategies exhausted — manual browser capture required.",
        hint={
            "url": url,
            "fix": (
                "Open the URL in a browser, scroll to load all exhibitors, "
                "Ctrl+S → 'Webpage, Complete', then use "
                "build_event_tier_list with source_kind=html_file directly."
            ),
        },
        retryable=False,
    )


# ---- pagination helpers ----

_PAGE_MARKERS = ("PAGE", "page", "pageNo", "pageNum", "page_no", "p", "offset")


def _looks_paginated(body: str) -> bool:
    """Heuristic: body suggests there are more pages."""
    lower = body.lower()
    return any(kw in lower for kw in (
        '"total":', '"totalcount":', '"totalpage":', '"totalpages":',
        '"nextpage":', '"hasmore":', '"has_more":', '"next":', '"nexturl":',
    ))


def _maybe_paginate(
    winner: Any, body: str, budget: AcquireBudget, max_pages: int
) -> tuple[str, int]:
    """Follow page-number pagination from the winning request, budget-bounded."""
    if not winner or not _looks_paginated(body):
        return body, 1

    from event_intel.acquisition import http_status_map as _status_map
    from event_intel.acquisition import raw_fetch as _raw_fetch

    pages = [body]
    for page_num in range(2, max_pages + 1):
        if not budget.may_fetch(reserved=0):
            break
        next_params = _next_page_params(winner, page_num)
        if next_params is None:
            break
        page_resp = _raw_fetch.fetch_raw(
            winner.url,
            method=winner.method,
            params=next_params if winner.method == "GET" else None,
            data=next_params if winner.method == "POST" else None,
        )
        budget.charge(page_resp)
        p_ok, _ = _status_map.map_http_response(page_resp, landing_url=winner.url)
        if p_ok and page_resp.body:
            pages.append(page_resp.body)
        else:
            break

    if len(pages) == 1:
        return body, 1
    http_pages = len(pages)
    wrapped = (
        f"<!-- acquire_exhibitor_source paginated: {http_pages} pages from "
        f"{winner.url} -->\n" + "\n\n".join(pages)
    )
    return wrapped, http_pages


def _next_page_params(winner: Any, page_num: int) -> dict[str, str] | None:
    """Try to construct next-page params from the winner's URL query."""
    params = dict(winner.url.split("?")[1].split("&") if "?" in winner.url else [])
    for key in _PAGE_MARKERS:
        if key.upper() in {k.upper() for k in params}:
            real_key = next(k for k in params if k.upper() == key.upper())
            new_params = dict(params)
            new_params[real_key] = str(page_num)
            return new_params
    return None
