"""ChatGPTOAuthProvider unit tests — plan v3 R1 / R3 / R7.

Covers:
- SSE parser: happy path, completed-with-no-deltas (legitimate empty), partial
  stream truncation (no completed), response.error event, response.failed event.
- Payload: max_output_tokens forwarded from caller's max_tokens, reasoning.effort
  applied per-instance, temperature absent.
- __init__ validates reasoning_effort and rejects typos with ValueError.
- make_llm_provider factory honors config.llm.chatgpt_oauth_reasoning_effort.

All tests bypass the actual OAuth flow by injecting a fake access_token whose
JWT payload carries the expected `chatgpt_account_id` claim. No network calls.
"""
from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from event_intel.providers.llm import (
    ChatGPTOAuthProvider,
    make_llm_provider,
)

# ---------- helpers ----------


def _make_jwt(account_id: str = "acct-test-123") -> str:
    """Build a minimal JWT with the chatgpt_account_id claim our provider extracts."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_dict = {
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    payload = base64.urlsafe_b64encode(
        json.dumps(payload_dict).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _sse_event(etype: str, **fields) -> str:
    """Format one SSE `data:` line for an event dict."""
    body = {"type": etype, **fields}
    return f"data: {json.dumps(body)}"


class _FakeStreamResponse:
    """Mimics the context-manager + iter_lines surface of httpx.stream()."""

    def __init__(self, lines: list[str], status_code: int = 200):
        self._lines = lines
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        yield from self._lines

    def read(self) -> bytes:
        return b""


@contextmanager
def _patch_stream(lines: list[str], status_code: int = 200):
    """Make `httpx.stream(...)` return the canned event sequence."""
    fake = _FakeStreamResponse(lines, status_code=status_code)
    with patch("httpx.stream", return_value=fake):
        yield fake


def _make_provider_with_token(*, reasoning_effort: str = "low") -> ChatGPTOAuthProvider:
    """Build a provider that thinks it's already authenticated."""
    p = ChatGPTOAuthProvider(reasoning_effort=reasoning_effort)
    fake_token = _make_jwt()
    # Bypass _ensure_token by populating the in-memory cache directly.
    p._tokens = {
        "access_token": fake_token,
        "refresh_token": "rt",
        "expires_at": 9_999_999_999,  # far future
    }
    return p


# ---------- __init__ validation (R7) ----------


def test_init_accepts_low_medium_high():
    for effort in ("low", "medium", "high"):
        p = ChatGPTOAuthProvider(reasoning_effort=effort)
        assert p._reasoning_effort == effort


def test_init_rejects_invalid_reasoning_effort():
    with pytest.raises(ValueError) as exc:
        ChatGPTOAuthProvider(reasoning_effort="loww")  # typo
    assert "reasoning_effort" in str(exc.value)
    assert "low" in str(exc.value)


def test_init_rejects_empty_string_effort():
    with pytest.raises(ValueError):
        ChatGPTOAuthProvider(reasoning_effort="")


# ---------- factory wiring (R7) ----------


def test_factory_forwards_reasoning_effort_from_config():
    config = {
        "llm": {
            "provider": "chatgpt_oauth",
            "chatgpt_oauth_model": "gpt-5.5",
            "chatgpt_oauth_reasoning_effort": "medium",
        }
    }
    p = make_llm_provider(config)
    assert isinstance(p, ChatGPTOAuthProvider)
    assert p._reasoning_effort == "medium"


def test_factory_defaults_to_low_when_effort_missing():
    config = {"llm": {"provider": "chatgpt_oauth"}}
    p = make_llm_provider(config)
    assert p._reasoning_effort == "low"


def test_factory_typo_in_config_raises_immediately():
    config = {
        "llm": {
            "provider": "chatgpt_oauth",
            "chatgpt_oauth_reasoning_effort": "extra-high",
        }
    }
    with pytest.raises(ValueError):
        make_llm_provider(config)


# ---------- SSE parser happy path ----------


def test_chat_once_returns_text_from_deltas():
    lines = [
        _sse_event("response.output_text.delta", delta="Hel"),
        _sse_event("response.output_text.delta", delta="lo"),
        _sse_event(
            "response.completed",
            response={
                "model": "gpt-5.5",
                "usage": {"input_tokens": 5, "output_tokens": 2},
                "status": "completed",
                "output": [],
            },
        ),
        "data: [DONE]",
    ]
    p = _make_provider_with_token()
    with _patch_stream(lines):
        r = p.chat_once(system="sys", user="hi", max_tokens=10)
    assert r.text == "Hello"
    assert r.usage == {"input_tokens": 5, "output_tokens": 2}
    assert r.model == "gpt-5.5"
    assert r.stop_reason == "completed"


def test_chat_once_falls_back_to_completed_output_when_no_deltas():
    """Some backends emit final text only inside response.completed.output."""
    lines = [
        _sse_event(
            "response.completed",
            response={
                "model": "gpt-5.5",
                "usage": {"input_tokens": 5, "output_tokens": 2},
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "fallback"}],
                    }
                ],
            },
        ),
        "data: [DONE]",
    ]
    p = _make_provider_with_token()
    with _patch_stream(lines):
        r = p.chat_once(system="sys", user="hi", max_tokens=10)
    assert r.text == "fallback"


def test_chat_once_empty_completed_returns_empty_text_without_raising():
    """Legitimate empty response: completed event arrives, just no content."""
    lines = [
        _sse_event(
            "response.completed",
            response={
                "model": "gpt-5.5",
                "usage": {"input_tokens": 1, "output_tokens": 0},
                "status": "completed",
                "output": [],
            },
        ),
        "data: [DONE]",
    ]
    p = _make_provider_with_token()
    with _patch_stream(lines):
        r = p.chat_once(system="sys", user="hi", max_tokens=10)
    assert r.text == ""
    assert r.stop_reason == "completed"


# ---------- SSE parser error / truncation (R1) ----------


def test_chat_once_raises_on_response_error_event():
    lines = [
        _sse_event("response.output_text.delta", delta="partial"),
        _sse_event("response.error", error={"message": "model overloaded"}),
    ]
    p = _make_provider_with_token()
    with _patch_stream(lines):
        with pytest.raises(RuntimeError) as exc:
            p.chat_once(system="sys", user="hi", max_tokens=10)
    assert "error" in str(exc.value).lower()


def test_chat_once_raises_on_response_failed_event():
    lines = [
        _sse_event(
            "response.failed",
            response={"error": {"message": "content policy violation"}},
        ),
    ]
    p = _make_provider_with_token()
    with _patch_stream(lines):
        with pytest.raises(RuntimeError) as exc:
            p.chat_once(system="sys", user="hi", max_tokens=10)
    assert "content policy" in str(exc.value)


def test_chat_once_raises_on_truncated_stream_with_deltas_but_no_completed():
    """plan v3 R1: deltas without completed = truncated stream = error.
    Previously this returned partial text silently."""
    lines = [
        _sse_event("response.output_text.delta", delta="partial answer"),
        # stream ends here — no response.completed
    ]
    p = _make_provider_with_token()
    with _patch_stream(lines):
        with pytest.raises(RuntimeError) as exc:
            p.chat_once(system="sys", user="hi", max_tokens=10)
    assert "incomplete" in str(exc.value).lower()


def test_chat_once_raises_on_completely_empty_stream():
    p = _make_provider_with_token()
    with _patch_stream([]):
        with pytest.raises(RuntimeError):
            p.chat_once(system="sys", user="hi", max_tokens=10)


def test_chat_once_raises_on_non_200_status():
    p = _make_provider_with_token()
    with _patch_stream([], status_code=500):
        with pytest.raises(RuntimeError) as exc:
            p.chat_once(system="sys", user="hi", max_tokens=10)
    assert "500" in str(exc.value)


# ---------- payload contents (R3, R7) ----------


def _capture_payload() -> dict:
    """Patch httpx.stream and capture the json= kwarg, returning the call's payload."""
    captured: dict = {}

    def _stream(method, url, *, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = json
        # Return a minimal-but-complete stream so the call succeeds.
        lines = [
            _sse_event(
                "response.completed",
                response={
                    "model": "gpt-5.5",
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                    "status": "completed",
                    "output": [],
                },
            ),
            "data: [DONE]",
        ]
        return _FakeStreamResponse(lines)

    return captured, _stream


def test_payload_omits_max_tokens_due_to_codex_backend_limitation():
    """Codex backend rejects max_output_tokens / max_tokens / max_completion_tokens
    with 400 "Unsupported parameter" (verified 2026-05-29 smoke). The caller's
    max_tokens is therefore informational only when this provider is selected.

    This test prevents reintroducing the field — if a future commit adds it,
    the next real-site smoke will 400 again."""
    captured, stream_fn = _capture_payload()
    p = _make_provider_with_token()
    with patch("httpx.stream", side_effect=stream_fn):
        p.chat_once(system="sys", user="hi", max_tokens=2048)
    payload = captured["payload"]
    assert "max_output_tokens" not in payload
    assert "max_tokens" not in payload
    assert "max_completion_tokens" not in payload


def test_payload_applies_per_instance_reasoning_effort():
    """plan v3 R7: each instance carries its own validated effort into payload."""
    captured, stream_fn = _capture_payload()
    p = _make_provider_with_token(reasoning_effort="high")
    with patch("httpx.stream", side_effect=stream_fn):
        p.chat_once(system="sys", user="hi", max_tokens=10)
    assert captured["payload"]["reasoning"]["effort"] == "high"


def test_payload_omits_temperature():
    """Codex backend rejects temperature — confirm we never send it."""
    captured, stream_fn = _capture_payload()
    p = _make_provider_with_token()
    with patch("httpx.stream", side_effect=stream_fn):
        p.chat_once(system="sys", user="hi", max_tokens=10, temperature=0.5)
    assert "temperature" not in captured["payload"]


def test_payload_carries_required_headers():
    captured, stream_fn = _capture_payload()
    p = _make_provider_with_token()
    with patch("httpx.stream", side_effect=stream_fn):
        p.chat_once(system="sys", user="hi", max_tokens=10)
    headers = captured["headers"]
    assert headers["Authorization"].startswith("Bearer ")
    assert headers["chatgpt-account-id"] == "acct-test-123"
    assert headers["OpenAI-Beta"] == "responses=experimental"
    assert headers["originator"] == "codex_cli_rs"
    assert headers["accept"] == "text/event-stream"


# ---------- public login() + login-aware ping() hint (Phase 18T.1) ----------


def test_login_calls_ensure_token_no_browser(monkeypatch):
    """Non-force login() defers to _ensure_token and must NOT trigger the PKCE flow."""
    p = ChatGPTOAuthProvider()
    called: dict = {}

    def _fake_ensure():
        called["ensure"] = True
        return "tok"

    def _no_browser():
        raise AssertionError("the PKCE browser flow must not run in the non-force path")

    monkeypatch.setattr(p, "_ensure_token", _fake_ensure)
    monkeypatch.setattr(p, "_pkce_login", _no_browser)

    result = p.login()
    assert called.get("ensure") is True
    assert result["status"] == "ok"
    assert result["model"] == p.model
    assert "token_path" in result


def test_login_force_runs_pkce_and_saves(monkeypatch):
    """force=True bypasses the cache: runs _pkce_login and persists the tokens."""
    p = ChatGPTOAuthProvider()
    fake_tokens = {"access_token": "x", "refresh_token": "y", "expires_at": 9_999_999_999}
    saved: dict = {}

    def _must_not_run():
        raise AssertionError("force=True must bypass _ensure_token")

    monkeypatch.setattr(p, "_pkce_login", lambda: fake_tokens)
    monkeypatch.setattr(p, "_save_tokens", lambda t: saved.update(t))
    monkeypatch.setattr(p, "_ensure_token", _must_not_run)

    result = p.login(force=True)
    assert result["status"] == "ok"
    assert saved == fake_tokens
    assert p._tokens == fake_tokens


def test_ping_not_logged_in_hint_points_to_cli(tmp_path, monkeypatch):
    """check_runtime surfaces ping()['fix']; it must point users at `login-chatgpt`."""
    p = ChatGPTOAuthProvider()
    # Point the token path at a non-existent file → not_logged_in branch.
    monkeypatch.setattr(p, "_TOKEN_PATH", tmp_path / "no_token.json")

    status = p.ping()
    assert status["status"] == "not_logged_in"
    assert "login-chatgpt" in status["fix"]
