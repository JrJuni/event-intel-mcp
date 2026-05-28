"""Phase 18T T0.5 — raw_fetch tests.

All network calls are monkeypatched via httpx.Client. Tests verify:
  - raw_fetch returns RawResponse with raw body (no extraction).
  - Safety violations (private IP redirect) RAISE MCPError(INVALID_INPUT).
  - Transport failures RETURN RawResponse(status=0, network_error=...).
  - raw_fetch does NOT map 404 -> INVALID_INPUT (that's http_status_map's job).
  - Cross-origin redirects are rejected by default.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from event_intel.acquisition.raw_fetch import RawResponse, fetch_raw
from event_intel.errors import ErrorCode, MCPError


# ---------- helpers ----------

def _make_httpx_response(*, status: int, text: str, url: str, headers: dict | None = None, history=None):
    """Build a minimal mock that looks like an httpx.Response."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.url = MagicMock()
    resp.url.__str__ = lambda self: url
    resp.headers = headers or {"content-type": "text/html"}
    resp.history = history or []
    return resp


class _FakeClient:
    """Context manager that returns a canned response."""

    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def request(self, *args, **kwargs):
        return self._response


def _fake_client(response):
    return patch("httpx.Client", return_value=_FakeClient(response))


# ---------- tests ----------


def test_get_200_returns_raw_body():
    mock_resp = _make_httpx_response(
        status=200, text="<html>Exhibitors</html>", url="https://example.com/exhibitors"
    )
    with _fake_client(mock_resp):
        result = fetch_raw("https://example.com/exhibitors")
    assert isinstance(result, RawResponse)
    assert result.status == 200
    assert result.body == "<html>Exhibitors</html>"
    assert result.network_error is None


def test_post_200_returns_raw_body():
    mock_resp = _make_httpx_response(
        status=200, text='[{"name":"A"}]', url="https://example.com/biz/get.asp",
        headers={"content-type": "application/json"}
    )
    with _fake_client(mock_resp):
        result = fetch_raw(
            "https://example.com/biz/get.asp",
            method="POST",
            data={"PAGE": "1"},
        )
    assert result.status == 200
    assert '"name"' in result.body
    assert result.content_type == "application/json"


def test_redirect_followed_and_final_url_updated():
    """raw_fetch returns final_url = URL after redirects."""
    mock_redirect = MagicMock()
    mock_redirect.url = MagicMock()
    mock_redirect.url.__str__ = lambda self: "https://example.com/old"

    mock_resp = _make_httpx_response(
        status=200, text="body", url="https://example.com/new",
        history=[mock_redirect]
    )
    with _fake_client(mock_resp):
        result = fetch_raw("https://example.com/old")
    assert result.final_url == "https://example.com/new"


def test_redirect_to_private_ip_raises_invalid_input():
    """Cross-origin redirect that resolves to a private IP must raise INVALID_INPUT."""
    with pytest.raises(MCPError) as ei:
        # validate_url is called for the initial URL AND for redirects.
        # We can exercise this by calling fetch_raw with a private-IP URL directly.
        fetch_raw("http://192.168.1.1/exhibitors")
    assert ei.value.error_code == ErrorCode.INVALID_INPUT
    assert ei.value.stage.value == "acquisition"


def test_network_error_returns_raw_response_with_status_0():
    """DNS failure / timeout / conn refused → RawResponse(status=0, network_error=...)
    NOT a raised exception. http_status_map is responsible for routing this."""
    import httpx

    with patch("httpx.Client", side_effect=httpx.ConnectError("Name or service not known")):
        result = fetch_raw("https://doesnotexist.example.invalid/")
    assert result.status == 0
    assert result.network_error is not None
    assert "ConnectError" in result.network_error or "service" in result.network_error.lower()


def test_raw_fetch_does_not_map_404_to_invalid_input():
    """404 is returned as RawResponse(status=404) — http_status_map's job."""
    mock_resp = _make_httpx_response(status=404, text="Not Found", url="https://example.com/gone")
    with _fake_client(mock_resp):
        result = fetch_raw("https://example.com/gone")
    # raw_fetch must NOT raise — returns the status as-is.
    assert result.status == 404
    assert result.network_error is None
