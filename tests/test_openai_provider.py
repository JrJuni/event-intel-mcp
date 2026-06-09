"""OpenAIProvider unit tests — Y2.2a (official key-based OpenAI API lane, D5).

Covers the chat/completions surface (httpx.post), error/corner paths, ping()
key gating, and make_llm_provider factory wiring. No network calls — httpx.post
is patched. Distinct from the ChatGPTOAuthProvider (Codex OAuth) tests.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from event_intel.providers.llm import (
    LLMResponse,
    OpenAIProvider,
    make_llm_provider,
)

# ---------- fakes ----------


class _FakeResponse:
    """Mimics the bits of httpx.Response that OpenAIProvider._post touches."""

    def __init__(self, payload: dict, status_code: int = 200, text: str = ""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or "error body"

    def json(self) -> dict:
        return self._payload


def _ok_payload(content: str = "hi there", *, model: str = "gpt-4.1") -> dict:
    return {
        "model": model,
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }


def _provider() -> OpenAIProvider:
    return OpenAIProvider(model="gpt-4.1", api_key="sk-test")


def _capture_post():
    """Patch target that records the httpx.post call and returns an OK response."""
    captured: dict = {}

    def _post(url, *, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = json
        captured["timeout"] = timeout
        return _FakeResponse(_ok_payload())

    return captured, _post


# ---------- happy path ----------


def test_chat_once_parses_content_usage_model_and_finish_reason():
    p = _provider()
    with patch("httpx.post", return_value=_FakeResponse(_ok_payload("Hello"))):
        r = p.chat_once(system="sys", user="hi", max_tokens=64)
    assert isinstance(r, LLMResponse)
    assert r.text == "Hello"
    assert r.usage == {"input_tokens": 11, "output_tokens": 3}
    assert r.model == "gpt-4.1"
    assert r.stop_reason == "stop"


def test_chat_cached_concatenates_contexts_into_single_user_message():
    captured, post_fn = _capture_post()
    p = _provider()
    with patch("httpx.post", side_effect=post_fn):
        p.chat_cached(
            system="SYS",
            cached_context="CACHED",
            volatile_context="VOL",
            task="TASK",
            max_tokens=32,
        )
    msgs = captured["payload"]["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "CACHED\n\nVOL\n\nTASK"


def test_chat_cached_skips_empty_context_parts():
    captured, post_fn = _capture_post()
    p = _provider()
    with patch("httpx.post", side_effect=post_fn):
        p.chat_cached(
            system="SYS", cached_context="CACHED", volatile_context="", task="TASK"
        )
    assert captured["payload"]["messages"][1]["content"] == "CACHED\n\nTASK"


def test_payload_and_headers_are_well_formed():
    captured, post_fn = _capture_post()
    p = _provider()
    with patch("httpx.post", side_effect=post_fn):
        p.chat_once(system="sys", user="hi", max_tokens=128, temperature=0.3)
    payload = captured["payload"]
    assert payload["model"] == "gpt-4.1"
    assert payload["max_tokens"] == 128
    assert payload["temperature"] == 0.3
    assert payload["messages"][0]["content"] == "sys"
    assert payload["messages"][1]["content"] == "hi"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["url"].endswith("/v1/chat/completions")


# ---------- corner cases ----------


def test_null_content_yields_empty_text_not_crash():
    """A choice with content=None (e.g. a tool-call response) must not crash."""
    payload = _ok_payload()
    payload["choices"][0]["message"]["content"] = None
    p = _provider()
    with patch("httpx.post", return_value=_FakeResponse(payload)):
        r = p.chat_once(system="s", user="u")
    assert r.text == ""


def test_missing_usage_defaults_to_zero():
    payload = _ok_payload()
    del payload["usage"]
    p = _provider()
    with patch("httpx.post", return_value=_FakeResponse(payload)):
        r = p.chat_once(system="s", user="u")
    assert r.usage == {"input_tokens": 0, "output_tokens": 0}


def test_non_200_status_raises_with_code():
    p = _provider()
    with patch("httpx.post", return_value=_FakeResponse({}, status_code=429, text="rate")):
        with pytest.raises(RuntimeError) as exc:
            p.chat_once(system="s", user="u")
    assert "429" in str(exc.value)


def test_empty_choices_raises():
    p = _provider()
    with patch("httpx.post", return_value=_FakeResponse({"choices": []})):
        with pytest.raises(RuntimeError) as exc:
            p.chat_once(system="s", user="u")
    assert "choices" in str(exc.value).lower()


def test_missing_key_raises_on_call_without_network():
    p = OpenAIProvider(model="gpt-4.1", api_key=None)
    # Must fail fast (no httpx.post call) when no key is configured.
    with patch("httpx.post", side_effect=AssertionError("must not call the API")):
        with pytest.raises(RuntimeError) as exc:
            p.chat_once(system="s", user="u")
    assert "OPENAI_API_KEY" in str(exc.value)


# ---------- ping() key gating ----------


def test_ping_ok_when_key_present():
    assert _provider().ping() == {"status": "ok", "model": "gpt-4.1"}


def test_ping_missing_key_surfaces_fix_hint():
    status = OpenAIProvider(api_key=None).ping()
    assert status["status"] == "missing_key"
    assert "OPENAI_API_KEY" in status["message"]
    assert "OPENAI_API_KEY" in status["fix"]


def test_env_key_picked_up_when_not_passed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    p = OpenAIProvider(model="gpt-4.1")
    assert p.ping()["status"] == "ok"


# ---------- factory wiring ----------


def test_factory_selects_openai_provider_with_config_model():
    config = {"llm": {"provider": "openai", "openai_model": "gpt-4.1-mini"}}
    p = make_llm_provider(config)
    assert isinstance(p, OpenAIProvider)
    assert p.model == "gpt-4.1-mini"


def test_factory_openai_defaults_model_when_missing():
    p = make_llm_provider({"llm": {"provider": "openai"}})
    assert isinstance(p, OpenAIProvider)
    assert p.model == "gpt-4.1"


def test_factory_model_param_overrides_config():
    config = {"llm": {"provider": "openai", "openai_model": "gpt-4.1"}}
    p = make_llm_provider(config, model="gpt-4.1-mini")
    assert p.model == "gpt-4.1-mini"
