"""acquire_exhibitor_source orchestrator — Phase 18T T3.

Pipeline (≤ 1 LLM call):
  1. Sanitize workspace_id + event_slug.
  2. URL safety + robots gates.
  3. Resolve artifact dir. If manifest exists + refetch=False: verify sha256 →
     return cached (source_kind, source_ref). Corrupt artifact → refetch + warn.
  4. Call analyze_page (1 Sonnet call).
  5. Branch on verdict:
     - static_html        → raw_fetch GET → source.html → ("html_file", path)
     - xhr_endpoint       → probe_endpoints → paginate up to max_pages →
                            source.html → ("html_file", path)
     - embedded_json      → probe_embedded_json → source.json →
                            ("text_file", path)   [R2-4 fix: not "text"]
     - operator_capture_required → OPERATOR_CAPTURE_REQUIRED
     - login_required     → LOGIN_REQUIRED
  6. Write artifact + manifest.json.
  7. Return AcquireResult(source_kind, source_ref, analysis, probe, artifact_path,
     manifest_path).

No new LLM calls after step 4. Probe is 0 LLM. Paginate cap = max_pages (3, yaml).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from event_intel.errors import ErrorCode, MCPError, Stage

_log = logging.getLogger(__name__)


@dataclass
class AcquireResult:
    source_kind: str
    source_ref: str
    analysis: dict[str, Any] = field(default_factory=dict)
    probe: dict[str, Any] | None = None
    artifact_path: Path | None = None
    manifest_path: Path | None = None


def acquire_source(
    *,
    url: str,
    workspace_id: str,
    event_slug: str,
    lang: str = "en",
    refetch: bool = False,
    max_pages: int = 3,
) -> AcquireResult:
    """Orchestrate analyze → probe → fetch → artifact.

    Raises MCPError on any unrecoverable failure.
    The caller (tool handler) wraps in envelope_from_exception.
    """
    from event_intel.acquisition import analyzer as _analyzer
    from event_intel.acquisition import http_status_map as _status_map
    from event_intel.acquisition import probe as _probe
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

    # 2. URL safety + robots.
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
                    )
                else:
                    _log.warning(
                        "Artifact sha256 mismatch for %s/%s — refetching.",
                        workspace_id, event_slug,
                    )

    # 4. Analyze page (1 LLM call).
    config = _preflight.load_config()
    model = config.get("llm", {}).get("extract_exhibitors_model", "claude-sonnet-4-6")
    max_tokens = int(config.get("llm", {}).get("extract_max_tokens", 2048))
    llm_provider = _llm.make_llm_provider(config, model=model)

    analysis = _analyzer.analyze_page(
        url=url,
        lang=lang,
        llm_provider=llm_provider,
        max_tokens=max_tokens,
    )
    verdict = analysis["verdict"]
    hints = analysis.get("hints", {})

    # 5. Branch on verdict.
    probe_result_dict: dict[str, Any] | None = None

    if verdict == "operator_capture_required":
        raise MCPError(
            error_code=ErrorCode.OPERATOR_CAPTURE_REQUIRED,
            stage=Stage.ACQUISITION,
            message="Page requires manual browser capture.",
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

    if verdict == "login_required":
        raise MCPError(
            error_code=ErrorCode.LOGIN_REQUIRED,
            stage=Stage.ACQUISITION,
            message="Page requires authentication.",
            hint={
                "url": url,
                "fix": (
                    "This page requires authentication. Check for an official "
                    "exhibitor API or contact the organizer."
                ),
            },
            retryable=False,
        )

    if verdict == "static_html":
        resp = _raw_fetch.fetch_raw(url, method="GET")
        should_proceed, err = _status_map.map_http_response(resp, landing_url=url)
        if not should_proceed:
            raise err  # type: ignore[misc]

        basename = "source.html"
        artifact_path = _artifacts.write_artifact(art_dir, basename, resp.body)
        source_kind = "html_file"
        content_type = resp.content_type
        status = resp.status
        http_pages = 1

    elif verdict == "xhr_endpoint":
        probe_result = _probe.probe_endpoints(
            url=url,
            hints=hints,
            lang=lang,
        )
        probe_result_dict = {
            "winner_url": probe_result.winner.url if probe_result.winner else None,
            "attempts": len(probe_result.attempts),
        }
        body = probe_result.body or ""
        content_type = probe_result.content_type or "text/html"
        status = probe_result.winner.status if probe_result.winner else 200
        http_pages = 1

        # Paginate if the response suggests there are more pages.
        if probe_result.winner and _looks_paginated(body):
            pages = [body]
            for page_num in range(2, max_pages + 1):
                next_params = _next_page_params(probe_result.winner, page_num)
                if next_params is None:
                    break
                page_resp = _raw_fetch.fetch_raw(
                    probe_result.winner.url,
                    method=probe_result.winner.method,
                    params=next_params if probe_result.winner.method == "GET" else None,
                    data=next_params if probe_result.winner.method == "POST" else None,
                )
                p_ok, _ = _status_map.map_http_response(page_resp, landing_url=probe_result.winner.url)
                if p_ok and page_resp.body:
                    pages.append(page_resp.body)
                else:
                    break
            http_pages = len(pages)
            # Wrap pages in a single HTML artifact with a header.
            body = (
                f"<!-- acquire_exhibitor_source paginated: {http_pages} pages from "
                f"{probe_result.winner.url} -->\n"
                + "\n\n".join(pages)
            )

        basename = "source.html"
        artifact_path = _artifacts.write_artifact(art_dir, basename, body)
        source_kind = "html_file"

    elif verdict == "embedded_json":
        probe_result = _probe.probe_embedded_json(
            url=url,
            hints=hints,
        )
        probe_result_dict = {
            "winner_url": probe_result.winner.url if probe_result.winner else None,
        }
        body = probe_result.body or "{}"
        content_type = probe_result.content_type or "application/json"
        status = probe_result.winner.status if probe_result.winner else 200
        http_pages = 1

        # [v1.1 R1-3 fix] embedded_json returns text_file, NOT "text"
        basename = "source.json"
        artifact_path = _artifacts.write_artifact(art_dir, basename, body)
        source_kind = "text_file"

    else:
        # Fallback: treat as static_html for any unexpected verdict.
        resp = _raw_fetch.fetch_raw(url, method="GET")
        should_proceed, err = _status_map.map_http_response(resp, landing_url=url)
        if not should_proceed:
            raise err  # type: ignore[misc]
        basename = "source.html"
        artifact_path = _artifacts.write_artifact(art_dir, basename, resp.body)
        source_kind = "html_file"
        content_type = resp.content_type
        status = resp.status
        http_pages = 1

    # 6. Write manifest.
    manifest_dict = _artifacts.make_manifest(
        verdict=verdict,
        source_kind=source_kind,
        source_ref=str(artifact_path),
        url=url,
        content_type=content_type,
        status=status,
        http_pages=http_pages,
        artifact_path=artifact_path,
    )
    manifest_path = _artifacts.write_manifest(art_dir, manifest_dict)

    # 7. Return result.
    return AcquireResult(
        source_kind=source_kind,
        source_ref=str(artifact_path),
        analysis=analysis,
        probe=probe_result_dict,
        artifact_path=artifact_path,
        manifest_path=manifest_path,
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


def _next_page_params(winner: Any, page_num: int) -> dict[str, str] | None:
    """Try to construct next-page params from the winner's sample_params."""
    params = dict(winner.url.split("?")[1].split("&") if "?" in winner.url else [])
    # Also check if the winner attempt has sample_params stored.
    # For a simple page-number scheme, try common keys.
    for key in _PAGE_MARKERS:
        if key.upper() in {k.upper() for k in params}:
            real_key = next(k for k in params if k.upper() == key.upper())
            new_params = dict(params)
            new_params[real_key] = str(page_num)
            return new_params
    return None
