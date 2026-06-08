"""Phase 18T T3 — acquire_exhibitor_source: orchestrator + tool handler tests.

All network calls, LLM calls, and robots checks are monkeypatched.
Uses string-path patching throughout (cold-start isolation safety).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from event_intel.acquisition.acquire import acquire_source
from event_intel.acquisition.raw_fetch import RawResponse
from event_intel.errors import ErrorCode, MCPError
from event_intel.providers import llm as _llm

# ---------- shared fakes ----------

class _FakeAnalyzeLLM:
    """Returns a canned analyze_page verdict."""

    def __init__(self, verdict: str, endpoints=None, selectors=None):
        self._verdict = verdict
        self._endpoints = endpoints or []
        self._selectors = selectors or []

    def chat_once(self, *, system, user, **kwargs):
        payload = {
            "verdict": self._verdict,
            "confidence": 0.9,
            "hints": {
                "candidate_endpoints": self._endpoints,
                "embedded_json_selectors": self._selectors,
                "operator_action": None,
            },
            "page_meta": {
                "has_exhibitor_keywords": True,
                "detected_framework": "unknown",
            },
        }
        return _llm.LLMResponse(
            text=json.dumps(payload),
            usage={"input_tokens": 100, "output_tokens": 50},
            model="fake-sonnet",
        )

    def ping(self):
        return {"status": "ok", "model": "fake-sonnet"}

    def chat_cached(self, **_):  # pragma: no cover
        raise NotImplementedError


def _html_body(n: int = 500) -> str:
    return "<html><body>" + "<p>Exhibitor Company Booth A-1</p>" * n + "</body></html>"


def _bare_body() -> str:
    """A non-roster page: no exhibitor keywords, no scripts/endpoints — scores
    below the roster floor so the static rung rejects it."""
    return "<html><body><h1>Welcome</h1><p>Nothing to see here.</p></body></html>"


def _ok_resp(body: str, url: str = "https://example.com/exhibitors") -> RawResponse:
    return RawResponse(
        status=200, headers={"content-type": "text/html"},
        body=body, content_type="text/html",
        final_url=url,
    )


def _minimal_config():
    return {
        "schema_version": 1,
        "llm": {"extract_exhibitors_model": "fake-sonnet", "extract_max_tokens": 512},
        "paths": {"chroma_dir": "~/.event-intel/chroma"},
    }


def _patch_robots(allowed: bool = True):
    return patch("event_intel.acquisition.robots.is_allowed", return_value=allowed)


def _patch_config(cfg=None):
    return patch(
        "event_intel.runtime.preflight.load_config",
        return_value=cfg or _minimal_config(),
    )


def _patch_llm(verdict: str, *, endpoints=None, selectors=None):
    fake = _FakeAnalyzeLLM(verdict, endpoints=endpoints, selectors=selectors)
    return patch("event_intel.providers.llm.AnthropicProvider", return_value=fake)


def _patch_analyze(verdict: str, *, endpoints=None, selectors=None):
    """Patch analyze_response (the landing classifier the ladder calls) so the
    test controls the verdict/hints without the LLM. The landing fetch still
    happens via fetch_raw — patch that too in each test."""
    hints = {
        "candidate_endpoints": endpoints or [],
        "embedded_json_selectors": selectors or [],
        "operator_action": None,
    }
    analysis_result = {
        "ok": True,
        "verdict": verdict,
        "confidence": 0.9,
        "hints": hints,
        "page_meta": {"has_exhibitor_keywords": True, "detected_framework": "unknown",
                      "url": "https://example.com", "status": 200,
                      "content_type": "text/html", "bytes": 5000, "warnings": []},
        "url": "https://example.com",
        "lang": "en",
        "usage": {},
    }
    return patch("event_intel.acquisition.analyzer.analyze_response", return_value=analysis_result)


# ---------- 1. static_html verdict → html_file artifact ----------

def test_acquire_static_html_writes_html_file_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    html = _html_body()
    with (
        _patch_robots(),
        _patch_config(),
        _patch_analyze("static_html"),
        patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=_ok_resp(html)),
    ):
        result = acquire_source(
            url="https://example.com/exhibitors",
            workspace_id="ws1",
            event_slug="evt1",
        )

    assert result.source_kind == "html_file"
    assert result.source_ref.endswith("source.html")
    assert Path(result.source_ref).is_file()
    assert Path(result.source_ref).read_text(encoding="utf-8") == html
    assert result.manifest_path is not None and Path(result.manifest_path).is_file()
    assert result.analysis["verdict"] == "static_html"


# ---------- 2. xhr_endpoint verdict → probe → content-type aware artifact ----------

def test_acquire_xhr_endpoint_json_writes_text_file(tmp_path, monkeypatch):
    """A JSON XHR winner is persisted as source.json/text_file (review #7)."""
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    from event_intel.acquisition.probe import ProbeAttempt, ProbeResult
    endpoints = [{"url": "https://example.com/api/exhibitors", "method": "GET", "sample_params": {}, "rationale": ""}]
    body = json.dumps({"company_data": [{"company_title": f"Co {i}"} for i in range(10)]})

    fake_probe_result = ProbeResult(
        winner=ProbeAttempt(url="https://example.com/api/exhibitors", method="GET", status=200, score=0.8),
        attempts=[],
        body=body,
        content_type="application/json",
    )
    with (
        _patch_robots(),
        _patch_config(),
        _patch_analyze("xhr_endpoint", endpoints=endpoints),
        patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=_ok_resp(_html_body())),
        patch("event_intel.acquisition.probe.probe_endpoints", return_value=fake_probe_result),
    ):
        result = acquire_source(
            url="https://example.com",
            workspace_id="ws1",
            event_slug="evt2",
        )

    assert result.source_kind == "text_file"
    assert result.source_ref.endswith("source.json")
    assert Path(result.source_ref).read_text(encoding="utf-8") == body
    assert result.analysis["verdict"] == "xhr_endpoint"
    assert result.probe is not None
    assert result.selected_rung == "xhr"


def test_acquire_xhr_html_shell_writes_html_file(tmp_path, monkeypatch):
    """A non-JSON body served with a json content-type still lands as html_file
    (the body sniff guards against mislabeling a paginated wrapper / SPA shell)."""
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    from event_intel.acquisition.probe import ProbeAttempt, ProbeResult
    endpoints = [{"url": "https://example.com/api/exhibitors", "method": "GET", "sample_params": {}, "rationale": ""}]
    body = "exhibitor company booth participant\n" * 100

    fake_probe_result = ProbeResult(
        winner=ProbeAttempt(url="https://example.com/api/exhibitors", method="GET", status=200, score=0.8),
        attempts=[],
        body=body,
        content_type="application/json",
    )
    with (
        _patch_robots(),
        _patch_config(),
        _patch_analyze("xhr_endpoint", endpoints=endpoints),
        patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=_ok_resp(_html_body())),
        patch("event_intel.acquisition.probe.probe_endpoints", return_value=fake_probe_result),
    ):
        result = acquire_source(
            url="https://example.com", workspace_id="ws1", event_slug="evt2b",
        )

    assert result.source_kind == "html_file"
    assert result.source_ref.endswith("source.html")


# ---------- 3. embedded_json verdict → text_file (NOT "text") — R1-3 fix ----------

def test_acquire_embedded_json_returns_text_file_not_text(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    from event_intel.acquisition.probe import ProbeAttempt, ProbeResult
    selectors = [{"script_id": None, "script_var_name": "__NEXT_DATA__", "key_path": "props.pageProps.exhibitors"}]
    json_body = json.dumps([{"name": "ACME", "booth": "A1"}] * 30, ensure_ascii=False)

    fake_probe_result = ProbeResult(
        winner=ProbeAttempt(url="https://example.com/", method="GET", status=200, score=0.7),
        attempts=[],
        body=json_body,
        content_type="application/json",
    )
    with (
        _patch_robots(),
        _patch_config(),
        _patch_analyze("embedded_json", selectors=selectors),
        patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=_ok_resp(_html_body())),
        patch("event_intel.acquisition.probe.probe_embedded_json", return_value=fake_probe_result),
    ):
        result = acquire_source(
            url="https://example.com/",
            workspace_id="ws1",
            event_slug="evt3",
        )

    assert result.source_kind == "text_file", "embedded_json must return text_file, not 'text'"
    assert result.source_ref.endswith("source.json")
    assert Path(result.source_ref).is_file()
    assert result.selected_rung == "embedded"


# ---------- 4. operator prior + no recoverable evidence → OPERATOR_CAPTURE_REQUIRED ----------

def test_acquire_operator_capture_required_raises(tmp_path, monkeypatch):
    """operator_capture_required is now a prior, not an immediate raise: every
    rung is still tried; with no roster / endpoints / bundles the ladder
    exhausts and surfaces OPERATOR_CAPTURE_REQUIRED."""
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    with (
        _patch_robots(),
        _patch_config(),
        _patch_analyze("operator_capture_required"),
        patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=_ok_resp(_bare_body())),
    ):
        with pytest.raises(MCPError) as ei:
            acquire_source(
                url="https://example.com",
                workspace_id="ws1",
                event_slug="evt4",
            )
    assert ei.value.error_code == ErrorCode.OPERATOR_CAPTURE_REQUIRED


# ---------- 5. login prior + nothing recoverable → LOGIN_REQUIRED ----------

def test_acquire_login_required_raises(tmp_path, monkeypatch):
    """login_required prior + no public roster recoverable → LOGIN_REQUIRED
    (the actionable terminal for the login prior)."""
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    with (
        _patch_robots(),
        _patch_config(),
        _patch_analyze("login_required"),
        patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=_ok_resp(_bare_body())),
    ):
        with pytest.raises(MCPError) as ei:
            acquire_source(
                url="https://example.com",
                workspace_id="ws1",
                event_slug="evt5",
            )
    assert ei.value.error_code == ErrorCode.LOGIN_REQUIRED


def test_acquire_login_landing_401_raises_login_required(tmp_path, monkeypatch):
    """A genuine HTTP 401 at the landing fetch raises LOGIN_REQUIRED before the
    LLM (analyze_response maps the status). Uses the real analyzer + fake LLM."""
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    resp_401 = RawResponse(
        status=401, headers={}, body="Unauthorized",
        content_type="text/html", final_url="https://example.com",
    )
    with (
        _patch_robots(),
        _patch_config(),
        _patch_llm("static_html"),
        patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=resp_401),
    ):
        with pytest.raises(MCPError) as ei:
            acquire_source(
                url="https://example.com", workspace_id="ws1", event_slug="evt5b",
            )
    assert ei.value.error_code == ErrorCode.LOGIN_REQUIRED


# ---------- 6. refetch=False + valid manifest → cache hit (0 fetches, 0 LLM) ----------

def test_acquire_cache_hit_returns_manifest_data(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))

    # Pre-create an artifact + manifest.
    from event_intel.storage.artifacts import (
        artifact_dir,
        make_manifest,
        write_artifact,
        write_manifest,
    )
    art_dir = artifact_dir(workspace_id="ws1", event_slug="evt6")
    body = _html_body()
    path = write_artifact(art_dir, "source.html", body)
    manifest = make_manifest(
        verdict="static_html",
        source_kind="html_file",
        source_ref=str(path),
        url="https://example.com/exhibitors",
        content_type="text/html",
        status=200,
        http_pages=1,
        artifact_path=path,
    )
    write_manifest(art_dir, manifest)

    fetch_calls = []
    with (
        _patch_robots(),
        _patch_config(),
        patch("event_intel.acquisition.raw_fetch.fetch_raw", side_effect=lambda *a, **kw: fetch_calls.append(1) or _ok_resp("")),
        patch("event_intel.providers.llm.AnthropicProvider") as mock_llm,
    ):
        result = acquire_source(
            url="https://example.com/exhibitors",
            workspace_id="ws1",
            event_slug="evt6",
            refetch=False,
        )

    assert len(fetch_calls) == 0, "Cache hit should make 0 fetch calls"
    assert mock_llm.call_count == 0, "Cache hit should make 0 LLM calls"
    assert result.source_kind == "html_file"
    assert result.analysis.get("cached") is True


# ---------- 7. refetch=False + sha256 mismatch → refetches ----------

def test_acquire_sha256_mismatch_triggers_refetch(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))

    from event_intel.storage.artifacts import (
        artifact_dir,
        write_artifact,
        write_manifest,
    )
    art_dir = artifact_dir(workspace_id="ws1", event_slug="evt7")
    path = write_artifact(art_dir, "source.html", _html_body())
    # Write manifest with WRONG sha256.
    manifest = {
        "verdict": "static_html",
        "source_kind": "html_file",
        "source_ref": str(path),
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "sha256": "0" * 64,   # deliberately wrong
        "url": "https://example.com",
        "content_type": "text/html",
        "status": 200,
        "http_pages": 1,
    }
    write_manifest(art_dir, manifest)

    html = _html_body()
    with (
        _patch_robots(),
        _patch_config(),
        _patch_llm("static_html"),
        patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=_ok_resp(html)),
    ):
        result = acquire_source(
            url="https://example.com",
            workspace_id="ws1",
            event_slug="evt7",
            refetch=False,
        )

    # Should have re-fetched and written a fresh artifact.
    assert result.source_kind == "html_file"
    assert result.analysis.get("cached") is not True


# ---------- 8. refetch=True → ignores cache ----------

def test_acquire_refetch_true_ignores_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))

    from event_intel.storage.artifacts import (
        artifact_dir,
        make_manifest,
        write_artifact,
        write_manifest,
    )
    art_dir = artifact_dir(workspace_id="ws1", event_slug="evt8")
    path = write_artifact(art_dir, "source.html", _html_body())
    manifest = make_manifest(
        verdict="static_html", source_kind="html_file", source_ref=str(path),
        url="https://example.com", content_type="text/html",
        status=200, http_pages=1, artifact_path=path,
    )
    write_manifest(art_dir, manifest)

    analyze_call_count = [0]
    html = _html_body()

    def _counting_analyze(*args, **kwargs):
        analyze_call_count[0] += 1
        return {
            "ok": True, "verdict": "static_html", "confidence": 0.9,
            "hints": {"candidate_endpoints": [], "embedded_json_selectors": [], "operator_action": None},
            "page_meta": {"url": "https://example.com", "status": 200,
                          "content_type": "text/html", "bytes": len(html), "warnings": [],
                          "has_exhibitor_keywords": True, "detected_framework": "unknown"},
            "url": "https://example.com", "lang": "en", "usage": {},
        }

    with (
        _patch_robots(),
        _patch_config(),
        patch("event_intel.acquisition.analyzer.analyze_response", side_effect=_counting_analyze),
        patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=_ok_resp(html)),
    ):
        acquire_source(
            url="https://example.com",
            workspace_id="ws1",
            event_slug="evt8",
            refetch=True,
        )

    assert analyze_call_count[0] == 1, "refetch=True must call analyze_response (ignore cache)"


# ---------- 9. Korean event_slug → INVALID_INPUT with suggested_slug ----------

def test_acquire_korean_slug_raises_invalid_input_with_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    with (
        _patch_robots(),
        _patch_config(),
        patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=_ok_resp("")),
    ):
        with pytest.raises(MCPError) as ei:
            acquire_source(
                url="https://example.com",
                workspace_id="default",
                event_slug="서울 ITS 2026",
            )
    assert ei.value.error_code == ErrorCode.INVALID_INPUT
    hint = ei.value.hint or {}
    assert "suggested_slug" in hint


# ---------- 10. Artifact path isolation per workspace ----------

def test_acquire_artifact_path_isolation_by_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    html = _html_body()

    for ws in ("ws_alpha", "ws_beta"):
        with (
            _patch_robots(),
            _patch_config(),
            _patch_analyze("static_html"),
            patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=_ok_resp(html)),
        ):
            result = acquire_source(
                url="https://example.com",
                workspace_id=ws,
                event_slug="same_slug",
            )
        assert ws in result.source_ref, f"artifact for {ws} should be under {ws}/ subdir"

    # Both should have separate directories.
    assert (tmp_path / "ws_alpha" / "same_slug" / "source.html").is_file()
    assert (tmp_path / "ws_beta" / "same_slug" / "source.html").is_file()


# ---------- 11. Tool wrapper: ok=True shape ----------

def test_acquire_tool_wrapper_happy_path(tmp_path, monkeypatch):
    from event_intel.tools import acquire_exhibitor_source as _tool_mod
    html = _html_body()

    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    with (
        _patch_robots(),
        _patch_config(),
        _patch_analyze("static_html"),
        patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=_ok_resp(html)),
    ):
        result = _tool_mod.acquire_exhibitor_source(
            url="https://example.com/exhibitors",
            workspace_id="ws1",
            event_slug="tool_evt",
        )

    assert result["ok"] is True
    assert result["source_kind"] == "html_file"
    assert "source_ref" in result
    assert "verdict" in result
    assert "artifact_path" in result
    assert "manifest_path" in result


# ---------- 12. Tool wrapper: empty url → INVALID_INPUT ----------

def test_acquire_tool_wrapper_empty_url():
    from event_intel.tools import acquire_exhibitor_source as _tool_mod

    result = _tool_mod.acquire_exhibitor_source(
        url="",
        workspace_id="default",
        event_slug="evt",
    )
    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
    assert result["stage"] == "acquisition"


# ====================================================================
# C7 — agentic acquisition ladder
# ====================================================================

_HCR_LANDING = (
    "<html><head>"
    '<base href="https://expo.example.com/">'
    '<script src="assets/app.bundle.js"></script>'
    "</head><body><div id=\"app\"></div></body></html>"
)
_HCR_BUNDLE = "axios.get('_ajax/exhibitor/get_exhibitor_data/').then(r => r.data);"
# Real HCR roster JSON is ~577 KB; keep the fixture > 1 KB so it doesn't trip the
# short-body operator heuristic in http_status_map (which targets inert shells).
_HCR_JP_JSON = json.dumps(
    {"company_data": [
        {"company_name": f"出展会社_{i:03d}", "booth": f"A-{i:03d}",
         "category": "ヘルスケア・ロボティクス"}
        for i in range(40)
    ]},
    ensure_ascii=False,
)


def _hcr_router(landing_url="https://expo.example.com/exhibitor/"):
    """fetch_raw router for the HCR bundle chain. Returns (router, counters)."""
    counters = {"landing": 0, "bundle": 0, "endpoint": 0}

    def router(u, **kw):
        if "get_exhibitor_data" in u:
            counters["endpoint"] += 1
            return RawResponse(
                status=200, headers={}, body=_HCR_JP_JSON,
                content_type="application/json", final_url=u,
            )
        if "app.bundle.js" in u:
            counters["bundle"] += 1
            return RawResponse(
                status=200, headers={}, body=_HCR_BUNDLE,
                content_type="application/javascript", final_url=u,
            )
        if u == landing_url:
            counters["landing"] += 1
            return RawResponse(
                status=200, headers={"content-type": "text/html"}, body=_HCR_LANDING,
                content_type="text/html", final_url=u,
            )
        return RawResponse(
            status=404, headers={}, body="not found",
            content_type="text/plain", final_url=u,
        )

    return router, counters


def test_ladder_hcr_e2e_operator_prior_bundle_to_json(tmp_path, monkeypatch):
    """DoD: an operator-prior verdict still recovers — the bundle rung discovers
    the endpoint inside an external <script src>, the JP JSON roster is accepted
    by the structural validator, and it is saved as source.json/text_file with
    zero operator intervention."""
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    landing_url = "https://expo.example.com/exhibitor/"
    router, counters = _hcr_router(landing_url)

    with (
        _patch_robots(),
        _patch_config(),
        _patch_analyze("operator_capture_required"),  # wrong prior — ladder recovers
        patch("event_intel.acquisition.raw_fetch.fetch_raw", side_effect=router),
    ):
        result = acquire_source(
            url=landing_url, workspace_id="ws1", event_slug="hcr",
        )

    assert result.selected_rung == "bundle"
    assert result.source_kind == "text_file"
    assert result.source_ref.endswith("source.json")
    saved = json.loads(Path(result.source_ref).read_text(encoding="utf-8"))
    assert len(saved["company_data"]) == 40
    # Landing fetched exactly once; bundle + endpoint each fetched.
    assert counters["landing"] == 1
    assert counters["bundle"] == 1
    assert counters["endpoint"] >= 1
    # Manifest records ladder provenance: selected rung, winning request, fps.
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["selected_rung"] == "bundle"
    assert manifest["winning_request"]["url"].endswith("/get_exhibitor_data/")
    assert manifest["analysis_fp"] and manifest["config_fp"]


def test_ladder_spa_shell_not_accepted_as_static(tmp_path, monkeypatch):
    """An SPA shell (keywords/scripts but no real roster, no external bundle) is
    rejected by the static rung and, with nothing else to try, surfaces operator
    capture — it must NOT be saved as a static html_file."""
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    shell = (
        "<html><body><div id=\"exhibitor-app\"></div>"
        "<script>window.__BOOT__ = true;</script></body></html>"
    )
    with (
        _patch_robots(),
        _patch_config(),
        _patch_analyze("static_html"),  # LLM wrongly said static
        patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=_ok_resp(shell)),
    ):
        with pytest.raises(MCPError) as ei:
            acquire_source(url="https://example.com", workspace_id="ws1", event_slug="shell")
    assert ei.value.error_code == ErrorCode.OPERATOR_CAPTURE_REQUIRED
    assert not (tmp_path / "ws1" / "shell" / "source.html").exists()


def test_ladder_budget_deadline_blocks_bundle_to_operator(tmp_path, monkeypatch):
    """Budget enforcement: with the overall deadline already spent, the bundle
    rung that would otherwise succeed (see e2e) is blocked → OPERATOR_CAPTURE."""
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    landing_url = "https://expo.example.com/exhibitor/"
    router, counters = _hcr_router(landing_url)
    cfg = {**_minimal_config(), "acquisition": {"overall_deadline_seconds": 0}}

    with (
        _patch_robots(),
        _patch_config(cfg),
        _patch_analyze("operator_capture_required"),
        patch("event_intel.acquisition.raw_fetch.fetch_raw", side_effect=router),
    ):
        with pytest.raises(MCPError) as ei:
            acquire_source(url=landing_url, workspace_id="ws1", event_slug="budget")
    assert ei.value.error_code == ErrorCode.OPERATOR_CAPTURE_REQUIRED
    # Landing was fetched, but the budget stopped the bundle rung before fetching it.
    assert counters["landing"] == 1
    assert counters["bundle"] == 0


def test_ladder_operator_prior_does_not_block_static_success(tmp_path, monkeypatch):
    """A wrong operator prior must not short-circuit a perfectly static roster:
    rung order tries static and wins (selected_rung=static, no operator raise)."""
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    html = _html_body()
    with (
        _patch_robots(),
        _patch_config(),
        _patch_analyze("operator_capture_required"),
        patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=_ok_resp(html)),
    ):
        result = acquire_source(url="https://example.com", workspace_id="ws1", event_slug="orderp")
    assert result.selected_rung == "static"
    assert result.source_kind == "html_file"
    assert Path(result.source_ref).read_text(encoding="utf-8") == html


def test_manifest_backward_compat_missing_ladder_fields(tmp_path, monkeypatch):
    """A pre-ladder manifest (no selected_rung/winning_request/fp fields) still
    loads via .get() defaults (M9) and serves a cache hit."""
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    from event_intel.storage.artifacts import (
        ManifestModel,
        artifact_dir,
        read_manifest,
        write_artifact,
        write_manifest,
    )

    # Direct from_dict: old shape parses, new fields default.
    old = {
        "verdict": "static_html", "source_kind": "html_file", "source_ref": "/x/source.html",
        "fetched_at": "2026-01-01T00:00:00+00:00", "sha256": "0" * 64,
        "url": "https://example.com", "content_type": "text/html",
        "status": 200, "http_pages": 1,
    }
    m = ManifestModel.from_dict(old)
    assert m.selected_rung is None and m.winning_request is None
    assert m.analysis_fp == "" and m.config_fp == ""

    # And a cache hit off an old manifest works (0 fetch / 0 LLM).
    art_dir = artifact_dir(workspace_id="ws1", event_slug="oldcache")
    body = _html_body()
    path = write_artifact(art_dir, "source.html", body)
    old_manifest = make_manifest_old(path, body)
    write_manifest(art_dir, old_manifest)
    assert read_manifest(art_dir) is not None

    fetch_calls = []
    with (
        _patch_robots(),
        _patch_config(),
        patch("event_intel.acquisition.raw_fetch.fetch_raw",
              side_effect=lambda *a, **kw: fetch_calls.append(1) or _ok_resp("")),
        patch("event_intel.providers.llm.AnthropicProvider") as mock_llm,
    ):
        result = acquire_source(
            url="https://example.com", workspace_id="ws1", event_slug="oldcache", refetch=False,
        )
    assert len(fetch_calls) == 0
    assert mock_llm.call_count == 0
    assert result.analysis.get("cached") is True


def make_manifest_old(path, body):
    """A manifest dict in the pre-C7 shape (no ladder provenance fields)."""
    import hashlib
    return {
        "verdict": "static_html", "source_kind": "html_file", "source_ref": str(path),
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "url": "https://example.com", "content_type": "text/html",
        "status": 200, "http_pages": 1,
    }
