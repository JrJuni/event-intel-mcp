"""Phase 18T T2 — probe_exhibitor_endpoint: probe core + tool handler tests.

All network calls are monkeypatched via module-reference pattern.
probe_endpoints and probe_embedded_json make 0 LLM calls — pure code paths.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from event_intel.acquisition import raw_fetch as _raw_fetch
from event_intel.acquisition.probe import (
    ProbeAttempt,
    ProbeResult,
    probe_embedded_json,
    probe_endpoints,
)
from event_intel.acquisition.raw_fetch import RawResponse
from event_intel.errors import ErrorCode, MCPError, Stage

# ---------- shared helpers ----------

def _raw_ok(body: str, *, url: str = "https://example.com/api") -> RawResponse:
    return RawResponse(
        status=200, headers={"content-type": "application/json"},
        body=body, content_type="application/json",
        final_url=url,
    )


def _raw_fail(status: int, *, url: str = "https://example.com/api") -> RawResponse:
    return RawResponse(
        status=status, headers={}, body="error",
        content_type="text/html", final_url=url,
    )


def _exhibitor_body(*, lang: str = "en") -> str:
    if lang == "ko":
        # Ensure all 8 KO keywords appear as literal strings (no json.dumps escaping).
        # This guarantees "kw in body" substring checks pass for all KO_KEYWORDS.
        keywords_line = "참가업체 참가사 회사명 부스 출품사 출품업체 전시업체 참가기업\n"
        return (
            keywords_line * 30
            + json.dumps(
                [{"회사명": "테스트 주식회사", "부스": "A-1"}] * 30,
                ensure_ascii=False,
            )
        )
    return (
        "Exhibitor company booth participant stand\n" * 30
        + json.dumps([{"company": "ACME Corp", "booth": "A1"}] * 30)
    )


def _hints_xhr(
    method: str = "GET",
    url: str = "https://example.com/api/exhibitors",
    params: dict | None = None,
) -> dict:
    return {
        "candidate_endpoints": [
            {
                "url": url,
                "method": method,
                "sample_params": params or {},
                "rationale": "found in script",
            }
        ],
        "embedded_json_selectors": [],
        "operator_action": None,
    }


def _patch_robots(allowed: bool = True):
    # Use string path so the patch always targets sys.modules current version,
    # even after cold-start tests purge and re-import event_intel.*.
    return patch("event_intel.acquisition.robots.is_allowed", return_value=allowed)


def _patch_fetch(*responses: RawResponse):
    """Patch fetch_raw to return responses in sequence."""
    iter_responses = iter(responses)
    return patch(
        "event_intel.acquisition.raw_fetch.fetch_raw",
        side_effect=lambda *a, **kw: next(iter_responses),
    )


# ---------- 1. Three candidates, one wins ----------

def test_probe_endpoints_returns_best_scoring_winner():
    bodies = [
        "nothing here",
        _exhibitor_body(),
        "also nothing",
    ]
    hints = {
        "candidate_endpoints": [
            {"url": f"https://example.com/api/{i}", "method": "GET", "sample_params": {}, "rationale": ""}
            for i in range(3)
        ],
        "embedded_json_selectors": [],
        "operator_action": None,
    }
    responses = [_raw_ok(b, url=f"https://example.com/api/{i}") for i, b in enumerate(bodies)]
    # probe_endpoints fetches candidates once to score, then fetches winner again.
    winner_resp = _raw_ok(bodies[1], url="https://example.com/api/1")
    all_responses = responses + [winner_resp]

    with _patch_robots(), _patch_fetch(*all_responses):
        result = probe_endpoints(url="https://example.com", hints=hints)

    assert result.winner is not None
    assert result.winner.url == "https://example.com/api/1"
    assert result.winner.score > 0.5


# ---------- 2. All below threshold → ACQUISITION_AMBIGUOUS ----------

def test_probe_endpoints_all_below_threshold_raises_ambiguous():
    hints = _hints_xhr()
    with _patch_robots(), _patch_fetch(_raw_ok("nothing relevant")):
        with pytest.raises(MCPError) as ei:
            probe_endpoints(url="https://example.com", hints=hints, min_score=0.9)
    assert ei.value.error_code == ErrorCode.ACQUISITION_AMBIGUOUS
    assert "attempts" in (ei.value.hint or {})


# ---------- 3. HTTP 4xx on a candidate → skip, continue ----------

def test_probe_endpoints_4xx_skips_and_continues():
    hints = {
        "candidate_endpoints": [
            {"url": "https://example.com/api/bad", "method": "GET", "sample_params": {}, "rationale": ""},
            {"url": "https://example.com/api/good", "method": "GET", "sample_params": {}, "rationale": ""},
        ],
        "embedded_json_selectors": [],
        "operator_action": None,
    }
    bad_resp = _raw_fail(404, url="https://example.com/api/bad")
    good_resp = _raw_ok(_exhibitor_body(), url="https://example.com/api/good")
    winner_resp = _raw_ok(_exhibitor_body(), url="https://example.com/api/good")

    with _patch_robots(), _patch_fetch(bad_resp, good_resp, winner_resp):
        result = probe_endpoints(url="https://example.com", hints=hints)

    assert result.winner is not None
    assert result.winner.url == "https://example.com/api/good"
    # Bad candidate should appear in attempts with an error.
    bad_attempts = [a for a in result.attempts if a.url == "https://example.com/api/bad"]
    assert bad_attempts and bad_attempts[0].error_code is not None


# ---------- 4. HTTP 5xx on all → ACQUISITION_AMBIGUOUS ----------

def test_probe_endpoints_5xx_on_all_raises_ambiguous():
    hints = {
        "candidate_endpoints": [
            {"url": "https://example.com/api/a", "method": "GET", "sample_params": {}, "rationale": ""},
            {"url": "https://example.com/api/b", "method": "GET", "sample_params": {}, "rationale": ""},
        ],
        "embedded_json_selectors": [],
        "operator_action": None,
    }
    with _patch_robots(), _patch_fetch(
        _raw_fail(503, url="https://example.com/api/a"),
        _raw_fail(500, url="https://example.com/api/b"),
    ):
        with pytest.raises(MCPError) as ei:
            probe_endpoints(url="https://example.com", hints=hints)
    assert ei.value.error_code == ErrorCode.ACQUISITION_AMBIGUOUS


# ---------- 5. Korean keyword scorer ----------

def test_probe_endpoints_korean_keyword_scorer():
    hints = {
        "candidate_endpoints": [
            {"url": "https://example.com/api/en", "method": "GET", "sample_params": {}, "rationale": ""},
            {"url": "https://example.com/api/ko", "method": "GET", "sample_params": {}, "rationale": ""},
        ],
        "embedded_json_selectors": [],
        "operator_action": None,
    }
    en_body = "generic page content without keywords"
    ko_body = _exhibitor_body(lang="ko")
    ko_winner_resp = _raw_ok(ko_body, url="https://example.com/api/ko")

    with _patch_robots(), _patch_fetch(
        _raw_ok(en_body, url="https://example.com/api/en"),
        _raw_ok(ko_body, url="https://example.com/api/ko"),
        ko_winner_resp,
    ):
        result = probe_endpoints(url="https://example.com", hints=hints, lang="ko")

    assert result.winner is not None
    assert result.winner.url == "https://example.com/api/ko"


# ---------- 6. Cross-origin skipped without allow_cross_origin ----------

def test_probe_endpoints_cross_origin_skipped_by_default():
    hints = {
        "candidate_endpoints": [
            # Cross-origin candidate.
            {"url": "https://other-domain.com/api/data", "method": "GET", "sample_params": {}, "rationale": ""},
        ],
        "embedded_json_selectors": [],
        "operator_action": None,
    }
    with _patch_robots():
        with pytest.raises(MCPError) as ei:
            probe_endpoints(url="https://example.com", hints=hints)
    # All candidates skipped → ACQUISITION_AMBIGUOUS.
    assert ei.value.error_code == ErrorCode.ACQUISITION_AMBIGUOUS
    attempts = ei.value.hint.get("attempts", [])
    assert any("cross_origin_skipped" in (a.get("warning") or "") for a in attempts)


# ---------- 7. Embedded JSON script_var_name regex + key_path walk ----------

def test_probe_embedded_json_script_var_name_and_key_path():
    data = {"props": {"pageProps": {"exhibitors": [{"name": "ACME", "booth": "A1"}]}}}
    html = f"<html><body><script>var __NEXT_DATA__ = {json.dumps(data)};</script></body></html>"

    hints = {
        "candidate_endpoints": [],
        "embedded_json_selectors": [
            {"script_id": None, "script_var_name": "__NEXT_DATA__", "key_path": "props.pageProps.exhibitors"},
        ],
        "operator_action": None,
    }
    page_resp = RawResponse(
        status=200, headers={"content-type": "text/html"},
        body=html, content_type="text/html",
        final_url="https://example.com/",
    )
    with _patch_robots(), _patch_fetch(page_resp):
        result = probe_embedded_json(url="https://example.com/", hints=hints)

    assert result.winner is not None
    assert result.body is not None
    parsed = json.loads(result.body)
    assert isinstance(parsed, list)
    assert parsed[0]["name"] == "ACME"


# ---------- 8. Max-5 cap enforced ----------

def test_probe_endpoints_max_5_cap():
    hints = {
        "candidate_endpoints": [
            {"url": f"https://example.com/api/{i}", "method": "GET", "sample_params": {}, "rationale": ""}
            for i in range(8)  # 8 candidates, only 5 should be tried
        ],
        "embedded_json_selectors": [],
        "operator_action": None,
    }
    # Return low-score responses for all.
    responses = [_raw_ok("no keywords here", url=f"https://example.com/api/{i}") for i in range(5)]
    fetch_calls = []

    def counting_fetch(url, **kwargs):
        fetch_calls.append(url)
        idx = len(fetch_calls) - 1
        if idx < len(responses):
            return responses[idx]
        return _raw_ok("no keywords")

    with _patch_robots():
        with patch.object(_raw_fetch, "fetch_raw", side_effect=counting_fetch):
            with pytest.raises(MCPError) as ei:
                probe_endpoints(url="https://example.com", hints=hints, min_score=0.9)

    assert ei.value.error_code == ErrorCode.ACQUISITION_AMBIGUOUS
    # Should have tried exactly 5 candidates (cap enforced).
    assert len(fetch_calls) <= 5


# ===== v2 added test cases =====

# ---------- 9. Malformed hints → INVALID_INPUT (review #2) ----------

def test_probe_rejects_malformed_hints():
    """hints.candidate_endpoints must be a list; a string triggers INVALID_INPUT."""
    bad_hints = {"candidate_endpoints": "not-a-list"}
    with _patch_robots():
        with pytest.raises(MCPError) as ei:
            probe_endpoints(url="https://example.com", hints=bad_hints)
    assert ei.value.error_code == ErrorCode.INVALID_INPUT
    assert "validation_error" in (ei.value.hint or {})


# ---------- 10. Non-{GET,POST} method → skip + attempt log (review #3) ----------

def test_probe_skips_non_get_post_methods():
    """A PUT candidate must be skipped (attempt log entry) rather than raising."""
    hints = {
        "candidate_endpoints": [
            # PUT candidate — should be skipped.
            {"url": "https://example.com/api/bad", "method": "PUT", "sample_params": {}, "rationale": ""},
            # GET candidate — should win.
            {"url": "https://example.com/api/good", "method": "GET", "sample_params": {}, "rationale": ""},
        ],
        "embedded_json_selectors": [],
        "operator_action": None,
    }
    good_body = _exhibitor_body()
    winner_resp = _raw_ok(good_body, url="https://example.com/api/good")

    with _patch_robots(), _patch_fetch(_raw_ok(good_body, url="https://example.com/api/good"), winner_resp):
        result = probe_endpoints(url="https://example.com", hints=hints)

    assert result.winner is not None
    assert result.winner.url == "https://example.com/api/good"
    # PUT candidate must appear in attempts with INVALID_INPUT.
    put_attempts = [a for a in result.attempts if a.url == "https://example.com/api/bad"]
    assert put_attempts
    assert put_attempts[0].error_code == ErrorCode.INVALID_INPUT
    assert "PUT" in (put_attempts[0].error_message or "")


# ---------- 11. Advisory warning → proceed + carry in attempt log (review #4) ----------

def test_probe_carries_short_body_warning():
    """When map_http_response returns (True, advisory_warning), candidate proceeds
    and the warning is stored in the attempt log — not treated as failure."""

    advisory_warn = MCPError(
        error_code=ErrorCode.INTERNAL,
        stage=Stage.ACQUISITION,
        message="short_body_with_scripts",
        hint={},
        retryable=False,
    )

    exhibitor_body = _exhibitor_body()
    short_spa_resp = RawResponse(
        status=200, headers={"content-type": "text/html"},
        body=exhibitor_body, content_type="text/html",
        final_url="https://example.com/api/exhibitors",
    )

    with _patch_robots():
        with patch("event_intel.acquisition.raw_fetch.fetch_raw", return_value=short_spa_resp):
            with patch(
                "event_intel.acquisition.http_status_map.map_http_response",
                return_value=(True, advisory_warn),
            ):
                result = probe_endpoints(
                    url="https://example.com",
                    hints=_hints_xhr(),
                )

    assert result.winner is not None
    assert result.winner.score > 0.0
    assert result.winner.warning == "short_body_with_scripts"


# ---------- 12. Tool wrapper: envelope shape + module-ref monkeypatch (review #5) ----------

def test_probe_exhibitor_endpoint_tool_wrapper_happy_path(monkeypatch):
    """tools/probe_exhibitor_endpoint delegates to probe_endpoints via module-ref.

    Uses string-path monkeypatch so the patch targets sys.modules current version
    of probe, matching what the freshly imported _tool_mod uses.
    """
    from event_intel.tools import probe_exhibitor_endpoint as _tool_mod

    fake_result = ProbeResult(
        winner=ProbeAttempt(
            url="https://example.com/api/exhibitors",
            method="GET",
            status=200,
            score=0.85,
        ),
        attempts=[],
        body='[{"company": "ACME"}]',
        content_type="application/json",
    )
    monkeypatch.setattr("event_intel.acquisition.probe.probe_endpoints", lambda **kw: fake_result)

    result = _tool_mod.probe_exhibitor_endpoint(
        url="https://example.com",
        hints={"candidate_endpoints": [
            {"url": "https://example.com/api/exhibitors", "method": "GET",
             "sample_params": {}, "rationale": ""}
        ], "embedded_json_selectors": [], "operator_action": None},
        lang="en",
    )

    assert result["ok"] is True
    assert result["winner"]["url"] == "https://example.com/api/exhibitors"
    assert result["winner"]["score"] == 0.85


def test_probe_exhibitor_endpoint_tool_wrapper_failure_envelope(monkeypatch):
    """When probe_endpoints raises MCPError, tool returns ok=false envelope.

    Import MCPError/ErrorCode inside the function so we always get the
    current sys.modules version — avoiding class-identity mismatches that arise
    after cold-start tests purge and re-import event_intel.*.
    """
    from event_intel.errors import ErrorCode as _EC
    from event_intel.errors import MCPError as _MCPError
    from event_intel.errors import Stage as _Stage
    from event_intel.tools import probe_exhibitor_endpoint as _tool_mod

    def _raise(**kw):
        raise _MCPError(
            error_code=_EC.ACQUISITION_AMBIGUOUS,
            stage=_Stage.ACQUISITION,
            message="no candidates",
            hint={"attempts": []},
            retryable=False,
        )

    monkeypatch.setattr("event_intel.acquisition.probe.probe_endpoints", _raise)

    result = _tool_mod.probe_exhibitor_endpoint(
        url="https://example.com",
        hints={"candidate_endpoints": [], "embedded_json_selectors": [], "operator_action": None},
    )

    assert result["ok"] is False
    assert result["error_code"] == "ACQUISITION_AMBIGUOUS"
    assert result["stage"] == "acquisition"


def test_probe_exhibitor_endpoint_tool_wrapper_empty_url():
    """Empty url → INVALID_INPUT before any probe logic."""
    from event_intel.tools import probe_exhibitor_endpoint as _tool_mod

    result = _tool_mod.probe_exhibitor_endpoint(url="")
    assert result["ok"] is False
    assert result["error_code"] == "INVALID_INPUT"
