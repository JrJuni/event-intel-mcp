from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    text: str
    usage: dict[str, int]
    model: str
    stop_reason: str | None = None


class LLMProvider(ABC):
    @abstractmethod
    def chat_cached(
        self,
        *,
        system: str,
        cached_context: str,
        volatile_context: str,
        task: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse: ...

    @abstractmethod
    def chat_once(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse: ...

    @abstractmethod
    def ping(self) -> dict: ...


class AnthropicProvider(LLMProvider):
    """Default LLMProvider using anthropic SDK. anthropic is imported lazily."""

    def __init__(self, *, model: str = "claude-sonnet-4-6", api_key: str | None = None):
        self.model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = None

    def _get_client(self):
        if self._client is None:
            from anthropic import Anthropic

            if not self._api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            self._client = Anthropic(api_key=self._api_key)
        return self._client

    def chat_cached(
        self,
        *,
        system: str,
        cached_context: str,
        volatile_context: str,
        task: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        client = self._get_client()
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": cached_context,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": volatile_context} if volatile_context else None,
                    {"type": "text", "text": task},
                ],
            }
        ]
        messages[0]["content"] = [c for c in messages[0]["content"] if c is not None]
        resp = client.messages.create(
            model=self.model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return LLMResponse(
            text=text,
            usage={
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                "cache_creation_input_tokens": getattr(
                    resp.usage, "cache_creation_input_tokens", 0
                )
                or 0,
            },
            model=resp.model,
            stop_reason=resp.stop_reason,
        )

    def chat_once(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        client = self._get_client()
        resp = client.messages.create(
            model=self.model,
            system=system,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return LLMResponse(
            text=text,
            usage={
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
            },
            model=resp.model,
            stop_reason=resp.stop_reason,
        )

    def ping(self) -> dict:
        if not self._api_key:
            return {"status": "missing_key"}
        return {"status": "ok", "model": self.model}
