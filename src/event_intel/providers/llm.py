from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


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
            return {
                "status": "missing_key",
                "message": "ANTHROPIC_API_KEY not set",
                "fix": "Set ANTHROPIC_API_KEY in .env (see .env.example)",
            }
        return {"status": "ok", "model": self.model}


class ChatGPTOAuthProvider(LLMProvider):
    """LLMProvider via ChatGPT Plus/Pro subscription — OAuth 2.0 PKCE flow.

    Uses the same auth.openai.com endpoint as the Codex CLI.
    On first use (or after token expiry), opens a browser for login.
    Tokens are cached at ~/.event-intel/chatgpt_auth.json and auto-refreshed.

    NOTE: This uses an unofficial path (Codex CLI client_id). OpenAI may change
    it. Intended for personal local use only — do not deploy as a shared server.
    Calls the OpenAI Responses API, not chat/completions.
    """

    _CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
    _AUTH_URL = "https://auth.openai.com/oauth/authorize"
    _TOKEN_URL = "https://auth.openai.com/oauth/token"
    _REDIRECT_URI = "http://localhost:1455/auth/callback"
    _RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
    _TOKEN_PATH = Path.home() / ".event-intel" / "chatgpt_auth.json"
    _ALLOWED_REASONING_EFFORTS = frozenset({"low", "medium", "high"})

    def __init__(self, *, model: str = "gpt-5.5", reasoning_effort: str = "low"):
        # plan v3 R7: validate at __init__ so typos surface as ValueError
        # immediately (preflight wraps as CONFIG_ERROR) rather than as opaque
        # backend errors at first chat_*() call.
        if reasoning_effort not in self._ALLOWED_REASONING_EFFORTS:
            raise ValueError(
                f"reasoning_effort must be one of {sorted(self._ALLOWED_REASONING_EFFORTS)}, "
                f"got {reasoning_effort!r}"
            )
        self.model = model
        self._reasoning_effort = reasoning_effort
        self._tokens: dict | None = None

    # ---- token persistence ----

    def _load_tokens(self) -> dict | None:
        if self._TOKEN_PATH.exists():
            try:
                import json
                return json.loads(self._TOKEN_PATH.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def _save_tokens(self, tokens: dict) -> None:
        import json
        self._TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._TOKEN_PATH.write_text(json.dumps(tokens, indent=2), encoding="utf-8")

    def _is_token_valid(self, tokens: dict) -> bool:
        import time
        return time.time() < tokens.get("expires_at", 0) - 60  # 60s buffer

    def _refresh(self, refresh_token: str) -> dict | None:
        import time
        import httpx
        try:
            resp = httpx.post(
                self._TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._CLIENT_ID,
                    "refresh_token": refresh_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                data["expires_at"] = time.time() + data.get("expires_in", 3600)
                return data
        except Exception:
            pass
        return None

    # ---- PKCE login ----

    def _pkce_login(self) -> dict:
        import base64
        import hashlib
        import json
        import secrets
        import threading
        import time
        import urllib.parse
        import webbrowser
        from http.server import BaseHTTPRequestHandler, HTTPServer

        code_verifier = secrets.token_urlsafe(96)[:128]
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )

        auth_code: dict[str, str] = {}

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if "code" in params:
                    auth_code["value"] = params["code"][0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    b"<html><body>Authenticated. You can close this tab.</body></html>"
                )

            def log_message(self, *args):
                pass

        server = HTTPServer(("localhost", 1455), _Handler)
        t = threading.Thread(target=server.handle_request, daemon=True)
        t.start()

        state = secrets.token_urlsafe(32)
        qs = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": self._CLIENT_ID,
            "redirect_uri": self._REDIRECT_URI,
            "scope": "openid profile email offline_access api.connectors.read api.connectors.invoke",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
            "originator": "codex_cli_rs",
        })
        auth_url = f"{self._AUTH_URL}?{qs}"
        print(f"\nOpening browser for ChatGPT login...\n{auth_url}\n")
        webbrowser.open(auth_url)

        t.join(timeout=120)
        server.server_close()

        if "value" not in auth_code:
            raise RuntimeError("ChatGPT OAuth login timed out or was cancelled")

        import httpx
        resp = httpx.post(
            self._TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": self._CLIENT_ID,
                "code": auth_code["value"],
                "code_verifier": code_verifier,
                "redirect_uri": self._REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        tokens = resp.json()
        tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
        return tokens

    # ---- token management ----

    def _ensure_token(self) -> str:
        if self._tokens is None:
            self._tokens = self._load_tokens()

        if self._tokens and self._is_token_valid(self._tokens):
            return self._tokens["access_token"]

        if self._tokens and self._tokens.get("refresh_token"):
            refreshed = self._refresh(self._tokens["refresh_token"])
            if refreshed:
                self._tokens = refreshed
                self._save_tokens(self._tokens)
                return self._tokens["access_token"]

        self._tokens = self._pkce_login()
        self._save_tokens(self._tokens)
        return self._tokens["access_token"]

    # ---- JWT account_id extraction ----

    @staticmethod
    def _decode_account_id(access_token: str) -> str:
        """Extract chatgpt_account_id from the OAuth access_token's JWT payload."""
        import base64
        import json
        try:
            payload_b64 = access_token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            return payload["https://api.openai.com/auth"]["chatgpt_account_id"]
        except Exception as exc:
            raise RuntimeError(
                f"Could not extract chatgpt_account_id from token: {exc}. "
                "Delete ~/.event-intel/chatgpt_auth.json and re-authenticate."
            ) from exc

    # ---- ChatGPT backend Responses API call (SSE) ----

    def _call(
        self,
        *,
        instructions: str,
        user_content: str,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        import json
        import httpx

        token = self._ensure_token()
        account_id = self._decode_account_id(token)

        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_content}],
                }
            ],
            "store": False,
            "stream": True,
            # plan v3 R7: per-instance effort, validated at __init__.
            "reasoning": {"effort": self._reasoning_effort, "summary": "auto"},
        }
        # Intentionally omitted, all rejected by the Codex backend with
        # "Unsupported parameter" (verified 2026-05-29 smoke test):
        #   - max_output_tokens / max_tokens / max_completion_tokens
        #   - temperature
        # Caller's `max_tokens` is therefore informational only when this
        # provider is selected. The backend enforces its own output cap based
        # on the model + subscription tier.
        _ = max_tokens  # explicit "we know this is dropped"
        _ = temperature
        headers = {
            "Authorization": f"Bearer {token}",
            "chatgpt-account-id": account_id,
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "OAI-Product-Sku": "codex",
            "Content-Type": "application/json",
            "accept": "text/event-stream",
        }

        text_parts: list[str] = []
        input_tokens = 0
        output_tokens = 0
        final_model = self.model
        stop_reason: str | None = None
        # plan v3 R1: track structural completion separately from text content.
        # A stream that yields deltas but never reaches `response.completed`
        # is a truncated/dropped stream and MUST surface as an error.
        seen_completed = False
        seen_error_payload: object | None = None

        with httpx.stream(
            "POST", self._RESPONSES_URL, headers=headers, json=payload, timeout=120
        ) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"ChatGPT backend returned {resp.status_code}: {body[:500]}"
                )
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].lstrip()
                if raw == "[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                etype = event.get("type", "")
                if etype == "response.output_text.delta":
                    text_parts.append(event.get("delta", ""))
                elif etype == "response.completed":
                    seen_completed = True
                    final = event.get("response", {})
                    final_model = final.get("model", final_model)
                    usage = final.get("usage", {}) or {}
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    stop_reason = final.get("status", "completed")
                    # Fallback: harvest text from full output if deltas missed any
                    if not text_parts:
                        for item in final.get("output", []):
                            if item.get("type") == "message":
                                for block in item.get("content", []):
                                    if block.get("type") == "output_text":
                                        text_parts.append(block.get("text", ""))
                elif etype in ("response.failed", "response.error", "response.incomplete"):
                    # plan v3 R1: backend error events surface as RuntimeError.
                    # The caller (tool boundary) wraps as UPSTREAM_ERROR envelope.
                    seen_error_payload = (
                        event.get("error")
                        or event.get("response", {}).get("error")
                        or event.get("response", {}).get("status")
                        or event
                    )
                    break

        if seen_error_payload is not None:
            raise RuntimeError(
                f"ChatGPT backend reported error event: {seen_error_payload!r}"
            )
        if not seen_completed:
            # Partial deltas + truncation, or no events at all — either way the
            # response is structurally incomplete and the caller cannot trust it.
            raise RuntimeError(
                "ChatGPT backend returned an incomplete stream "
                "(no response.completed event before close)."
            )

        return LLMResponse(
            text="".join(text_parts),
            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
            model=final_model,
            stop_reason=stop_reason,
        )

    # ---- LLMProvider interface ----

    def chat_once(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        return self._call(
            instructions=system,
            user_content=user,
            max_tokens=max_tokens,
            temperature=temperature,
        )

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
        # Responses API has no prompt caching — concatenate contexts
        parts = [p for p in (cached_context, volatile_context, task) if p]
        return self._call(
            instructions=system,
            user_content="\n\n".join(parts),
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def ping(self) -> dict:
        tokens = self._load_tokens()
        if tokens is None:
            return {
                "status": "not_logged_in",
                "message": "ChatGPT OAuth not authenticated",
                "fix": "First use will open a browser for ChatGPT login",
            }
        if self._is_token_valid(tokens) or tokens.get("refresh_token"):
            return {"status": "ok", "model": self.model}
        return {
            "status": "not_logged_in",
            "message": "ChatGPT OAuth token expired and no refresh token",
            "fix": "Delete ~/.event-intel/chatgpt_auth.json and retry",
        }


def make_llm_provider(config: dict, *, model: str | None = None) -> LLMProvider:
    """Factory: reads llm.provider from config and returns the right provider.

    For chatgpt_oauth: model param is ignored — always uses llm.chatgpt_oauth_model.
    For anthropic: model param (if given) overrides llm.draft_cards_model.
    """
    provider_name = config.get("llm", {}).get("provider", "anthropic")
    if provider_name == "chatgpt_oauth":
        oauth_model = config.get("llm", {}).get("chatgpt_oauth_model", "gpt-5.5")
        # plan v3 R7: forward optional reasoning_effort override. __init__
        # validates the value so a typo in config surfaces immediately.
        effort = config.get("llm", {}).get("chatgpt_oauth_reasoning_effort", "low")
        return ChatGPTOAuthProvider(model=oauth_model, reasoning_effort=effort)
    resolved = model or config.get("llm", {}).get("draft_cards_model", "claude-sonnet-4-6")
    return AnthropicProvider(model=resolved)
