"""Phase 18T T3 — acquire_exhibitor_source: orchestrator + tool handler tests.

All network calls, LLM calls, and robots checks are monkeypatched.
Uses string-path patching throughout (cold-start isolation safety).
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from event_intel.acquisition import acquire as _acquire_mod
from event_intel.acquisition import analyzer as _analyzer
from event_intel.acquisition import probe as _probe_mod
from event_intel.acquisition import raw_fetch as _raw_fetch
from event_intel.acquisition import robots as _robots_mod
from event_intel.acquisition.acquire import AcquireResult, acquire_source
from event_intel.acquisition.raw_fetch import RawResponse
from event_intel.errors import ErrorCode, MCPError, Stage
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
    """Patch analyze_page at the module level to skip HTTP entirely."""
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
    return patch("event_intel.acquisition.analyzer.analyze_page", return_value=analysis_result)


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


# ---------- 2. xhr_endpoint verdict → probe → html_file ----------

def test_acquire_xhr_endpoint_writes_html_file(tmp_path, monkeypatch):
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
        patch("event_intel.acquisition.probe.probe_endpoints", return_value=fake_probe_result),
    ):
        result = acquire_source(
            url="https://example.com",
            workspace_id="ws1",
            event_slug="evt2",
        )

    assert result.source_kind == "html_file"
    assert Path(result.source_ref).is_file()
    assert result.analysis["verdict"] == "xhr_endpoint"
    assert result.probe is not None


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


# ---------- 4. operator_capture_required → OPERATOR_CAPTURE_REQUIRED ----------

def test_acquire_operator_capture_required_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    with (
        _patch_robots(),
        _patch_config(),
        _patch_analyze("operator_capture_required"),
    ):
        with pytest.raises(MCPError) as ei:
            acquire_source(
                url="https://example.com",
                workspace_id="ws1",
                event_slug="evt4",
            )
    assert ei.value.error_code == ErrorCode.OPERATOR_CAPTURE_REQUIRED


# ---------- 5. login_required → LOGIN_REQUIRED ----------

def test_acquire_login_required_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    with (
        _patch_robots(),
        _patch_config(),
        _patch_analyze("login_required"),
    ):
        with pytest.raises(MCPError) as ei:
            acquire_source(
                url="https://example.com",
                workspace_id="ws1",
                event_slug="evt5",
            )
    assert ei.value.error_code == ErrorCode.LOGIN_REQUIRED


# ---------- 6. refetch=False + valid manifest → cache hit (0 fetches, 0 LLM) ----------

def test_acquire_cache_hit_returns_manifest_data(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))

    # Pre-create an artifact + manifest.
    from event_intel.storage.artifacts import artifact_dir, make_manifest, write_artifact, write_manifest
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

    from event_intel.storage.artifacts import artifact_dir, make_manifest, write_artifact, write_manifest
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

    from event_intel.storage.artifacts import artifact_dir, make_manifest, write_artifact, write_manifest
    art_dir = artifact_dir(workspace_id="ws1", event_slug="evt8")
    path = write_artifact(art_dir, "source.html", _html_body())
    from event_intel.storage.artifacts import sha256_of
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
        patch("event_intel.acquisition.analyzer.analyze_page", side_effect=_counting_analyze),
        patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=_ok_resp(html)),
    ):
        acquire_source(
            url="https://example.com",
            workspace_id="ws1",
            event_slug="evt8",
            refetch=True,
        )

    assert analyze_call_count[0] == 1, "refetch=True must call analyze_page (ignore cache)"


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
