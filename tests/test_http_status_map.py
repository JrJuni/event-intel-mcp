"""Phase 18T T0.5 — http_status_map: Contract #9 binding tests.

Covers all rows of the HTTP status → error_code mapping table from plan v1.2.
Special focus on the R2-2 SPA-shell short-body rule.
"""
from __future__ import annotations

from event_intel.acquisition.http_status_map import map_http_response
from event_intel.acquisition.raw_fetch import RawResponse
from event_intel.errors import ErrorCode


def _resp(*, status: int, body: str = "", network_error: str | None = None) -> RawResponse:
    return RawResponse(
        status=status,
        headers={},
        body=body,
        content_type="text/html",
        final_url="https://example.com/exhibitors",
        network_error=network_error,
    )


# ---------- network failure (status=0) ----------


def test_network_error_returns_upstream_error_retryable():
    ok, err = map_http_response(
        _resp(status=0, network_error="ConnectError: Name or service not known"),
        landing_url="https://example.com/exhibitors",
    )
    assert ok is False
    assert err is not None
    assert err.error_code == ErrorCode.UPSTREAM_ERROR
    assert err.retryable is True


# ---------- 2xx — healthy ----------


def test_200_healthy_body_returns_success():
    body = "<html><ul>" + "<li>Exhibitor A — AI company</li>" * 50 + "</ul></html>"
    ok, err = map_http_response(_resp(status=200, body=body))
    assert ok is True
    assert err is None


# ---------- 2xx — CAPTCHA ----------


def test_200_captcha_keyword_returns_operator_capture_required():
    body = "Please complete the CAPTCHA to continue."
    ok, err = map_http_response(_resp(status=200, body=body))
    assert ok is False
    assert err is not None
    assert err.error_code == ErrorCode.OPERATOR_CAPTURE_REQUIRED


def test_200_cloudflare_keyword_triggers_captcha_path():
    body = "Checking your browser... cloudflare protection"
    ok, err = map_http_response(_resp(status=200, body=body))
    assert ok is False
    assert err.error_code == ErrorCode.OPERATOR_CAPTURE_REQUIRED


# ---------- 2xx — short body without script (inert shell) ----------


def test_200_short_body_no_script_returns_operator_capture_required():
    body = "<html><body>Loading...</body></html>"  # < 1KB, no <script>, no hints
    assert len(body) < 1024
    ok, err = map_http_response(_resp(status=200, body=body))
    assert ok is False
    assert err is not None
    assert err.error_code == ErrorCode.OPERATOR_CAPTURE_REQUIRED


# ---------- 2xx — short body WITH script (SPA shell — R2-2 fix) ----------


def test_200_short_body_with_script_tag_returns_success_with_warning():
    """SPA shell: short body but contains a <script> tag. Analyzer should proceed."""
    body = '<html><head><script src="/app.js"></script></head><body></body></html>'
    assert len(body) < 1024
    ok, err = map_http_response(_resp(status=200, body=body))
    assert ok is True  # proceed — analyzer will classify
    # err is advisory (the warning); check it's present and has our sentinel message.
    assert err is not None
    assert err.message == "short_body_with_scripts"


def test_200_short_body_with_endpoint_hint_proceeds():
    """Short body containing 'fetch(' counts as an endpoint hint."""
    body = "<html><script>fetch('/api/exhibitors')</script></html>"
    assert len(body) < 1024
    ok, err = map_http_response(_resp(status=200, body=body))
    assert ok is True
    assert err is not None and err.message == "short_body_with_scripts"


# ---------- auth ----------


def test_401_returns_login_required():
    ok, err = map_http_response(_resp(status=401))
    assert ok is False
    assert err.error_code == ErrorCode.LOGIN_REQUIRED
    assert err.retryable is False


def test_403_returns_login_required():
    ok, err = map_http_response(_resp(status=403))
    assert ok is False
    assert err.error_code == ErrorCode.LOGIN_REQUIRED


# ---------- 404 ----------


def test_404_returns_invalid_input():
    ok, err = map_http_response(_resp(status=404))
    assert ok is False
    assert err.error_code == ErrorCode.INVALID_INPUT
    assert err.retryable is False


# ---------- 5xx ----------


def test_500_returns_upstream_error_retryable():
    ok, err = map_http_response(_resp(status=500))
    assert ok is False
    assert err.error_code == ErrorCode.UPSTREAM_ERROR
    assert err.retryable is True


def test_503_returns_upstream_error_retryable():
    ok, err = map_http_response(_resp(status=503))
    assert ok is False
    assert err.error_code == ErrorCode.UPSTREAM_ERROR
    assert err.retryable is True
